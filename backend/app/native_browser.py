"""One bundled Chromium process and persistent data directory per account.

The browser-scoped CDP endpoint, process handle and generation are held together.
No global Electron session or shared debugging endpoint is used for automation.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
from subprocess import Popen as _spawn_browser_process
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from .browser_runtime import BrowserLaunchLog, browser_executable
from .errors import ConflictError, NotFoundError, UpstreamUnavailableError, ValidationError
from .service import isoformat


def initialize_native_schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS native_browser_profiles (
        id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL REFERENCES app_users(id),
        serial INTEGER NOT NULL UNIQUE, name TEXT NOT NULL, group_name TEXT NOT NULL DEFAULT '',
        proxy_server TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    c.execute('INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(22,?)',(isoformat(),))


def validate_proxy(value):
    value=str(value or '').strip()
    if not value:return ''
    parsed=urlparse(value)
    try:port=parsed.port
    except ValueError:raise ValidationError('代理端口不正确') from None
    if (parsed.scheme not in {'http','https','socks5'} or not parsed.hostname or not port
            or parsed.username or parsed.password or parsed.path not in {'','/'} or parsed.query or parsed.fragment):
        raise ValidationError('代理请填写 http://主机:端口 或 socks5://主机:端口；此版本支持无密码或 IP 白名单代理')
    return value.rstrip('/')


class NativeBrowser:
    def __init__(self,database,data_dir,*,executable=None,headless=False,startup_file_logging=False):
        self.db=database;self.root=Path(data_dir).resolve()/'browser-profiles'
        self.executable=executable;self.headless=headless
        # Only the disposable release fixture requests Chrome file logs. Normal
        # account windows retain bounded stderr logging without verbose files.
        self.startup_file_logging=startup_file_logging
        self.lock=threading.RLock();self.profile_locks={};self.processes={};self.generations={}
        self.connections={};self.actions={};self.action_leases={};self.closing=set();self.stopping=False

    def _profile_lock(self,ident):
        with self.lock:return self.profile_locks.setdefault(ident,threading.RLock())

    def get(self,ident):
        with self.db.read() as c:row=c.execute('SELECT * FROM native_browser_profiles WHERE id=?',(ident,)).fetchone()
        if row is None:raise NotFoundError('内置窗口不存在')
        return dict(row)

    def directory(self,ident):
        row=self.get(ident)
        # Only canonical UUIDs from the database can form directory segments.
        return self.root/str(uuid.UUID(row['owner_user_id']))/str(uuid.UUID(ident.removeprefix('native:')))

    def assert_owner(self,owner,ident):
        if self.get(ident)['owner_user_id']!=owner:raise NotFoundError('内置窗口不存在')

    def create(self,c,owner,name,group,proxy):
        ident='native:'+str(uuid.uuid4());now=isoformat()
        serial=c.execute('SELECT COALESCE(max(serial),0)+1 FROM native_browser_profiles').fetchone()[0]
        c.execute('INSERT INTO native_browser_profiles VALUES(?,?,?,?,?,?,?,?)',(ident,owner,serial,name,group,validate_proxy(proxy),now,now))
        return ident

    def update(self,c,owner,ident,name,group,proxy):
        with self.lock:
            state=self.processes.get(ident)
            if state and state['process'].poll() is None and self.get(ident)['proxy_server']!=validate_proxy(proxy):raise ConflictError('请先关闭内置窗口再修改窗口配置')
        if c.execute('UPDATE native_browser_profiles SET name=?,group_name=?,proxy_server=?,updated_at=? WHERE id=? AND owner_user_id=?',(name,group,validate_proxy(proxy),isoformat(),ident,owner)).rowcount!=1:raise NotFoundError('内置窗口不存在')

    def _inventory_rows(self):
        # Deleted plans keep their history and on-disk login data, but no
        # longer offer an orphan window to task selectors. Legacy unbound
        # profiles remain visible until explicitly removed from a plan.
        with self.db.read() as c:
            return [dict(r) for r in c.execute('''SELECT n.* FROM native_browser_profiles n
                WHERE NOT EXISTS(SELECT 1 FROM account_window_plans p WHERE p.profile_id=n.id)
                   OR EXISTS(SELECT 1 FROM account_window_plans p WHERE p.profile_id=n.id AND p.archived=0)
                ORDER BY n.serial''')]

    def inventory(self):
        rows=self._inventory_rows()
        result=[]
        with self.lock:
            for r in rows:
                s=self.processes.get(r['id']);opened=bool(s and s['process'].poll() is None)
                result.append({'id':r['id'],'owner_user_id':r['owner_user_id'],'name':r['name'],'group':r['group_name'],'serial_number':r['serial'],'provider_order':r['serial'],'is_open':opened,'ready':opened and bool(s.get('ws')),'opening':opened and not bool(s.get('ws')),'window_state':'open' if opened else 'closed','generation':self.generations.get(r['id'],0),'provider':'native'})
        return result

    def begin_connection_attempt(self,ident):
        self.get(ident)
        with self.lock:
            self._check_ticket(None,ident)
            token='native-connection:'+uuid.uuid4().hex
            self.connections[token]={'profile':ident,'verified':False}
            return token

    def cancel_connection_attempt(self,token):
        with self.lock:return {'cancelled':self.connections.pop(token,None) is not None}

    def _check_ticket(self,token,ident):
        if self.stopping:raise ConflictError('软件正在退出')
        if ident in self.closing:raise ConflictError('窗口正在关闭，完成释放后才能重新使用')
        if token is not None and self.connections.get(token,{}).get('profile')!=ident:raise ConflictError('窗口连接已取消')

    def connection_endpoint(self,ident,*,open_if_needed=False,attempt_id=None):
        self.get(ident)
        with self._profile_lock(ident):
            with self.lock:
                self._check_ticket(attempt_id,ident)
                state=self.processes.get(ident)
                if state and state['process'].poll() is None and state.get('ws'):
                    return {'ws':state['ws'],'generation':state['generation']}
            if not open_if_needed:raise ConflictError('内置窗口尚未打开')
            directory=self.directory(ident);directory.mkdir(parents=True,exist_ok=True)
            port_file=directory/'DevToolsActivePort'
            port_file.unlink(missing_ok=True)
            exe=Path(self.executable or browser_executable(directory)).resolve()
            preferences=directory/'Default'/'Preferences'
            if not preferences.exists() and not (directory/'Local State').exists():
                preferences.parent.mkdir(parents=True,exist_ok=True)
                preferences.write_text(json.dumps({'intl':{'accept_languages':'zh-CN,zh','selected_languages':'zh-CN,zh'}}),encoding='utf-8')
            args=[str(exe),f'--user-data-dir={directory}','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--no-first-run','--no-default-browser-check','--disable-background-mode','--disable-background-timer-throttling','--disable-backgrounding-occluded-windows','--disable-renderer-backgrounding','--enable-logging=stderr']
            proxy=self.get(ident)['proxy_server']
            if proxy:args.append('--proxy-server='+validate_proxy(proxy))
            if self.headless:args.append('--headless=new')
            # Sandbox disabling is restricted to the root Linux test container.
            if self.headless and os.name!='nt' and os.geteuid()==0:args.append('--no-sandbox')
            args.append('about:blank')
            args.insert(1,'--lang=zh-CN')
            launch_options={}
            chrome_log=directory/'juxin-startup.chrome.log'
            if self.startup_file_logging:
                chrome_log.write_bytes(b'')
                args.remove('--enable-logging=stderr')
                args.extend(['--enable-logging','--log-file='+str(chrome_log)])
                # Never mutate the parent environment or inherit another
                # window's log path. Windows sandbox output can omit stderr.
                launch_options['env']={**os.environ,'CHROME_LOG_FILE':str(chrome_log)}
            diagnostics=BrowserLaunchLog(directory,exe,args,self.headless)
            if self.startup_file_logging:
                diagnostics.update('starting',chrome_log=str(chrome_log))
            try:
                with self.lock:self._check_ticket(attempt_id,ident)
                # Pass Unicode argv directly; no cmd.exe/shell quoting and no
                # dependence on the builder's current working directory.
                process=_spawn_browser_process(args,cwd=str(exe.parent),stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,bufsize=0,**launch_options)
            except OSError as error:
                diagnostics.update('launch_failed',error=str(error))
                raise UpstreamUnavailableError(f'无法启动内置 Chromium，诊断记录：{diagnostics.report}',
                    details={'reason':'native_browser_launch_failed','startup_log':str(diagnostics.report),
                             'winerror':getattr(error,'winerror',None),'errno':error.errno}) from error
            with self.lock:
                generation=self.generations.get(ident,0)+1;self.generations[ident]=generation
                state={'process':process,'generation':generation,'ws':None,'diagnostics':diagnostics};self.processes[ident]=state
            try:
                diagnostics.attach(process)
                deadline=time.monotonic()+35
                while time.monotonic()<deadline:
                    with self.lock:self._check_ticket(attempt_id,ident)
                    if process.poll() is not None:
                        diagnostics.update('startup_exited',exit_code=process.returncode)
                        raise UpstreamUnavailableError(f'内置 Chromium 启动后退出（退出码 {process.returncode}），诊断记录：{diagnostics.report}',
                            details={'reason':'native_browser_exited','exit_code':process.returncode,
                                     'startup_log':str(diagnostics.report),'stderr_log':str(diagnostics.output)})
                    if port_file.exists():
                        # Chromium may still hold/write this file on Windows.
                        try:
                            with port_file.open(encoding='utf-8') as stream:lines=stream.read(4096).splitlines()
                        except (OSError,UnicodeError):
                            lines=[]
                        if len(lines)>=2 and lines[0].isdigit() and 0<int(lines[0])<65536 and re.fullmatch(r'/devtools/browser/[A-Za-z0-9._:-]+',lines[1]):
                            with self.lock:
                                self._check_ticket(attempt_id,ident)
                                state['ws']=f'ws://127.0.0.1:{lines[0]}{lines[1]}'
                                if attempt_id:self.connections[attempt_id]['generation']=generation
                                diagnostics.update('ready')
                                return {'ws':state['ws'],'generation':generation}
                    time.sleep(.1)
                diagnostics.update('startup_timeout')
                raise UpstreamUnavailableError(f'内置窗口启动超时，诊断记录：{diagnostics.report}',
                    details={'reason':'native_browser_timeout','startup_log':str(diagnostics.report),
                             'stderr_log':str(diagnostics.output)})
            except BaseException:
                self._stop_process(state)
                with self.lock:
                    if self.processes.get(ident) is state:self.processes.pop(ident,None)
                raise

    def verify_connection_endpoint(self,ident,endpoint,generation=None,attempt_id=None):
        with self.lock:
            self._check_ticket(attempt_id,ident)
            state=self.processes.get(ident)
            if not state or state.get('closing') or self.generations.get(ident)!=state['generation'] or state['process'].poll() is not None or state['ws']!=endpoint or (generation is not None and generation!=state['generation']):raise ConflictError('内置窗口连接已变化，请重新连接')
            if attempt_id:self.connections[attempt_id].update(verified=True,generation=state['generation'])
            return {'verified':True,'profile_id':ident,'generation':state['generation']}

    def commit_connection_attempt(self,token):
        with self.lock:
            ticket=self.connections.get(token)
            if not ticket or not ticket['verified']:raise ConflictError('窗口连接尚未验证或已取消')
            state=self.processes.get(ticket['profile'])
            if not state:raise ConflictError('窗口已关闭')
            result=self.verify_connection_endpoint(ticket['profile'],state['ws'],ticket['generation'])
            self.connections.pop(token,None)
            return {**result,'committed':True}

    def begin_action_attempt(self,ident,endpoint,generation):
        with self.lock:
            token='native-action:'+uuid.uuid4().hex
            self.actions[token]={'profile':ident,'endpoint':endpoint,'generation':generation,'lease':None}
            return token

    def resolve_action_attempt(self,token):
        with self.lock:
            ticket=self.actions.get(token)
            if not ticket:raise ConflictError('操作已取消')
            self.verify_connection_endpoint(ticket['profile'],ticket['endpoint'],ticket['generation'])
            lease=ticket['lease'] or 'native-lease:'+uuid.uuid4().hex
            ticket['lease']=lease;self.action_leases[lease]=ticket['profile']
            return lease

    def adopt_action_attempt(self,token):
        with self.lock:
            ticket=self.actions.pop(token,None)
            if not ticket or not ticket['lease']:raise ConflictError('操作尚未就绪')
            return ticket['lease']

    def cancel_action_attempt(self,token):
        with self.lock:
            ticket=self.actions.pop(token,None)
            if ticket:self.action_leases.pop(ticket['lease'],None)
            return {'cancelled':bool(ticket)}

    def begin_action(self,ident,endpoint,generation):
        token=self.begin_action_attempt(ident,endpoint,generation)
        try:self.resolve_action_attempt(token);return self.adopt_action_attempt(token)
        except BaseException:self.cancel_action_attempt(token);raise

    def end_action(self,token):
        with self.lock:self.action_leases.pop(token,None)

    @staticmethod
    def _stop_process(state):
        process=state['process']
        if process.poll() is not None:
            if state.get('diagnostics'):state['diagnostics'].finish(process.returncode)
            return
        close_details={'close_requested':False,'forced_termination':False}
        if state.get('ws'):
            try:
                from websockets.sync.client import connect
                with connect(state['ws'],open_timeout=3,close_timeout=1,proxy=None) as ws:
                    ws.send(json.dumps({'id':1,'method':'Browser.close'}))
                    close_details['close_requested']=True
            except Exception as error:
                close_details['close_error_type']=type(error).__name__
        try:process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            close_details['forced_termination']=True
            process.terminate()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
        if state.get('diagnostics'):
            state['diagnostics'].update(state['diagnostics'].data['status'],**close_details)
            state['diagnostics'].finish(process.returncode)

    def close_profile(self,ident):
        self.get(ident)
        # Publish the close fence before waiting for an in-progress launch. This
        # prevents a delayed close from overtaking a later task's new browser.
        with self.lock:
            if ident in self.closing:raise ConflictError('窗口正在关闭，请等待释放')
            if ident in self.action_leases.values():raise ConflictError('窗口正在完成当前操作，请稍后关闭')
            self.closing.add(ident)
            self.connections={k:v for k,v in self.connections.items() if v['profile']!=ident}
            self.generations[ident]=self.generations.get(ident,0)+1
            state=self.processes.get(ident)
            if state:state['closing']=True
        try:
            with self._profile_lock(ident):
                with self.lock:state=self.processes.get(ident)
                if state:self._stop_process(state)
                with self.lock:self.processes.pop(ident,None)
            return {'profile_id':ident,'closed':True}
        finally:
            with self.lock:self.closing.discard(ident)

    def open_profile(self,ident):
        endpoint=self.connection_endpoint(ident,open_if_needed=True)
        return {'profile_id':ident,'opened':True,'generation':endpoint['generation']}

    def bring_profile_to_front(self,ident):return self.open_profile(ident)
    def profile_ports(self,ident):
        with self.lock:s=self.processes.get(ident)
        return {'profile_id':ident,'endpoint_available':bool(s and s['ws'] and s['process'].poll() is None),'provider_success':True}
    def shutdown(self):
        with self.lock:self.stopping=True;states=list(self.processes.values());self.connections.clear();self.actions.clear();self.action_leases.clear()
        for state in states:self._stop_process(state)
        with self.lock:self.processes.clear()


class BrowserHub:
    """Route profiles/tickets without sharing native and BitBrowser CDP targets."""
    PROFILE_METHODS={'connection_endpoint','verify_connection_endpoint','begin_connection_attempt','begin_action_attempt','begin_action','open_profile','close_profile','bring_profile_to_front','profile_ports','closed_profile_guard'}
    TICKET_METHODS={'cancel_connection_attempt','commit_connection_attempt','resolve_action_attempt','adopt_action_attempt','cancel_action_attempt','end_action'}
    def __init__(self,native,legacy):
        self.native=native;self.legacy=legacy;self.legacy_inventory={'windows':[]};self.legacy_lock=threading.Lock();self.legacy_stop=threading.Event();self.thread=None
    def __getattr__(self,name):
        if name in self.PROFILE_METHODS|self.TICKET_METHODS:
            def dispatch(ident,*args,**kwargs):
                provider=self.native if str(ident).startswith(('native:','native-')) else self.legacy
                if name=='closed_profile_guard' and not callable(getattr(provider,name,None)):
                    raise UpstreamUnavailableError('当前窗口服务不能安全核验关闭状态，清理占用已保留', details={'reason':'closed_profile_guard_unsupported'})
                return getattr(provider,name)(ident,*args,**kwargs)
            return dispatch
        return getattr(self.legacy,name)
    def resolve_cdp_endpoint(self,endpoint):
        owns=getattr(self.native,'owns_endpoint',None)
        if callable(owns) and owns(endpoint):return self.native.resolve_cdp_endpoint(endpoint)
        return self.legacy.resolve_cdp_endpoint(endpoint)
    def start(self):
        self.legacy.start()
        def poll():
            while not self.legacy_stop.is_set():
                try:
                    listing=self.legacy.list_all_windows()
                    with self.legacy_lock:self.legacy_inventory=listing
                except Exception:pass
                self.legacy_stop.wait(10)
        self.thread=threading.Thread(target=poll,daemon=True,name='optional-bitbrowser-inventory');self.thread.start()
    def shutdown(self):
        self.legacy_stop.set();self.native.shutdown();self.legacy.shutdown()
        if self.thread:self.thread.join(timeout=2)
    def status(self):return {'connected':True,'state':'ready','provider':'native','detail':'内置浏览器就绪；BitBrowser 为兼容选项','bitbrowser':self.legacy.status()}
    def health(self):return self.status()
    def list_all_windows(self,*,name='',force=False):
        native=self.native.inventory()
        with self.legacy_lock:legacy=list(self.legacy_inventory.get('windows',[]))
        rows=[*native,*legacy]
        if name:rows=[r for r in rows if name.casefold() in (r['name']+' '+str(r.get('serial_number',''))).casefold()]
        return {'windows':rows,'total':len(rows),'stale':False,'provider_success':True,'connection':self.status(),'legacy_stale':bool(self.legacy_inventory.get('stale',True))}
    def confirm_login(self):
        listing=self.legacy.confirm_login()
        with self.legacy_lock:self.legacy_inventory=listing
        return self.list_all_windows()
    def list_windows(self,*,page=0,page_size=100,name=''):
        listing=self.list_all_windows(name=name);return {**listing,'windows':listing['windows'][page*page_size:(page+1)*page_size],'page':page,'page_size':page_size}
