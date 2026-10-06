"""Desktop-owned account sessions exposed through a scoped CDP transport.

The existing task engine keeps its connection tickets, generation fences and
operation leases. A desktop bridge failure never launches external Chromium.
"""
from __future__ import annotations
import json
import os
import uuid
from contextlib import contextmanager
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from .native_browser import NativeBrowser, validate_proxy
from .errors import ConflictError, UpstreamUnavailableError, NotFoundError
from .service import isoformat

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

class DesktopBridge:
    def __init__(self,url,token):
        parsed=urlparse(url)
        if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or parsed.path not in ('','/') or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('Invalid embedded browser bridge')
        self.url=url.rstrip('/')+'/rpc';self.token=token
        self.opener=build_opener(ProxyHandler({}),NoRedirect())
    def call(self,method,**body):
        request=Request(self.url,data=json.dumps({'method':method,**body}).encode(),headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'},method='POST')
        try:
            # Closure verification is read-only background work. An unavailable
            # desktop must not hold the global presentation fence for 12s.
            timeout = 2 if method == 'confirm-closed' else 45 if method in {'profile-preview','manual-navigation','reset-whatsapp-storage','open-whatsapp'} else 12
            with self.opener.open(request,timeout=timeout) as response:return json.load(response)
        except HTTPError as error:
            try:message=json.loads(error.read(4096)).get('error','内置窗口操作失败')
            except Exception:message='内置窗口操作失败'
            raise ConflictError(message) from None
        except (URLError,TimeoutError,OSError,ValueError):
            raise UpstreamUnavailableError('软件内部网页服务未就绪，请重启软件后重试') from None

class EmbeddedBrowser(NativeBrowser):
    def __init__(self,database,data_dir,*,bridge=None):
        super().__init__(database,data_dir)
        self.bridge=bridge or DesktopBridge(os.environ['IGAC_EMBEDDED_BROWSER_URL'],os.environ['IGAC_EMBEDDED_BROWSER_TOKEN'])

    def update(self,c,owner,ident,name,group,proxy):
        current=self.get(ident)
        live=self.bridge.call('inventory')['profiles']
        if current['proxy_server']!=validate_proxy(proxy) and any(r['id']==ident for r in live):raise ConflictError('请先关闭内置窗口再修改窗口配置')
        if c.execute('UPDATE native_browser_profiles SET name=?,group_name=?,proxy_server=?,updated_at=? WHERE id=? AND owner_user_id=?',(name,group,validate_proxy(proxy),isoformat(),ident,owner)).rowcount!=1:raise NotFoundError('内置窗口不存在')

    def unread_snapshot(self,owner):return self.bridge.call('unread',owner=owner)

    def inventory(self):
        live={p['id']:p for p in self.bridge.call('inventory')['profiles']}
        rows=self._inventory_rows()
        return [{'id':r['id'],'owner_user_id':r['owner_user_id'],'name':r['name'],'group':r['group_name'],'serial_number':r['serial'],'provider_order':r['serial'],'is_open':r['id'] in live,'ready':r['id'] in live,'opening':False,'window_state':'open' if r['id'] in live else 'closed','generation':live.get(r['id'],{}).get('generation',0),'provider':'native'} for r in rows]

    @contextmanager
    def closed_profile_guard(self, ident, owner):
        """Affirmatively verify absence without closing or adopting any browser.

        Inventory hides retiring views and is not closure evidence. The desktop
        checks its complete profile/opening/retirement state instead. Keep the
        per-profile fence until the caller commits its historical reconciliation.
        """
        self.assert_owner(owner, ident)
        def local_idle():
            if self.stopping or ident in self.closing:
                raise ConflictError('窗口正在启动或关闭，暂时不能核验清理')
            if (any(ticket.get('profile') == ident for ticket in self.connections.values())
                    or any(ticket.get('profile') == ident for ticket in self.actions.values())
                    or ident in self.action_leases.values()):
                raise ConflictError('窗口仍有连接或操作正在退出，请稍后核验清理')
        with self._profile_lock(ident):
            with self.lock:local_idle()
            proof = self.bridge.call('confirm-closed', profile=ident, owner=owner)
            if (not isinstance(proof, dict) or proof.get('closed') is not True
                    or proof.get('profile_id') != ident or proof.get('owner_user_id') != owner
                    or proof.get('verification') != 'desktop-absence-v1'):
                raise UpstreamUnavailableError('未获得窗口完全关闭的确认，清理占用保留')
            # Connection tickets can be created while the desktop RPC waits.
            # Recheck and fence those too before the caller writes to SQLite.
            with self.lock:
                local_idle()
                yield proof

    def connection_endpoint(self,ident,*,open_if_needed=False,attempt_id=None):
        row=self.get(ident)
        with self._profile_lock(ident):
            with self.lock:self._check_ticket(attempt_id,ident)
            result=self.bridge.call('ensure',profile=ident,owner=row['owner_user_id'],proxy=row['proxy_server'],open=open_if_needed,name=row['name'])
            with self.lock:
                self._check_ticket(attempt_id,ident)
                state={'profile':ident,'ws':result['ws'],'generation':result['generation']}
                self.processes[ident]=state;self.generations[ident]=result['generation']
                if attempt_id:self.connections[attempt_id]['generation']=result['generation']
            return result

    def _verified_local_state(self,ident,endpoint,generation=None,attempt_id=None):
        # Caller holds self.lock. This check never performs desktop I/O.
        self._check_ticket(attempt_id,ident)
        state=self.processes.get(ident)
        if not state or state.get('closing') or self.generations.get(ident)!=state['generation'] or state['ws']!=endpoint or (generation is not None and generation!=state['generation']):raise ConflictError('内置窗口连接已变化，请重新连接')
        return state

    def verify_connection_endpoint(self,ident,endpoint,generation=None,attempt_id=None):
        # A stalled desktop verification must not hold every window's state lock
        # or prevent cancellation. The per-profile lock preserves open ordering;
        # close/cancel/shutdown still publish their fences while the RPC waits.
        with self._profile_lock(ident):
            with self.lock:
                state=self._verified_local_state(ident,endpoint,generation,attempt_id)
                expected_generation=state['generation']
            self.bridge.call('verify',profile=ident,ws=endpoint,generation=expected_generation)
            with self.lock:
                self._verified_local_state(ident,endpoint,expected_generation,attempt_id)
                if attempt_id:self.connections[attempt_id].update(verified=True,generation=expected_generation)
                return {'verified':True,'profile_id':ident,'generation':expected_generation}

    def commit_connection_attempt(self,token):
        # NativeBrowser's implementation holds self.lock around verification,
        # which is local there but a desktop RPC for this provider.
        with self.lock:
            ticket=self.connections.get(token)
            if not ticket or not ticket['verified']:raise ConflictError('窗口连接尚未验证或已取消')
            profile=ticket['profile'];generation=ticket['generation']
            state=self.processes.get(profile)
            if not state:raise ConflictError('窗口已关闭')
            endpoint=state['ws']
        result=self.verify_connection_endpoint(profile,endpoint,generation,token)
        with self.lock:
            self._verified_local_state(profile,endpoint,generation,token)
            self.connections.pop(token,None)
            return {**result,'committed':True}

    def resolve_action_attempt(self,token):
        with self.lock:
            ticket=self.actions.get(token)
            if not ticket:raise ConflictError('操作已取消')
            profile=ticket['profile'];endpoint=ticket['endpoint'];generation=ticket['generation']
        self.verify_connection_endpoint(profile,endpoint,generation)
        with self.lock:
            if self.actions.get(token) is not ticket:raise ConflictError('操作已取消')
            self._verified_local_state(profile,endpoint,generation)
            lease=ticket['lease'] or 'native-lease:'+uuid.uuid4().hex
            ticket['lease']=lease;self.action_leases[lease]=profile
            return lease

    def _stop_process(self,state):
        self.bridge.call('close',profile=state['profile'],generation=state['generation'])

    def close_profile(self,ident):
        row=self.get(ident)
        # Publish cancellation before waiting on an in-flight page creation.
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
                live=next((p for p in self.bridge.call('inventory')['profiles'] if p['id']==ident),None)
                if live:
                    # Owner validation remains necessary across Core restarts.
                    result=self.bridge.call('ensure',profile=ident,owner=row['owner_user_id'],proxy=row['proxy_server'],open=False)
                    self.bridge.call('close',profile=ident,generation=result['generation'])
                with self.lock:self.processes.pop(ident,None)
            return {'profile_id':ident,'closed':True}
        finally:
            with self.lock:self.closing.discard(ident)

    def owns_endpoint(self,endpoint):
        with self.lock:return any(s.get('ws')==endpoint for s in self.processes.values())

    def resolve_cdp_endpoint(self,endpoint):
        with self.lock:state=next((dict(s) for s in self.processes.values() if s.get('ws')==endpoint),None)
        if not state:raise ConflictError('内置窗口连接已失效')
        self.verify_connection_endpoint(state['profile'],endpoint,state['generation'])
        return endpoint

    def profile_ports(self,ident):
        self.get(ident)
        opened=any(r['id']==ident for r in self.bridge.call('inventory')['profiles'])
        return {'profile_id':ident,'endpoint_available':opened,'provider_success':True}

    def surface_show(self,profile,bounds,grant,target=None,read_only=False):return self.bridge.call('show',profile=profile,bounds=bounds,grant=grant,target=target,read_only=read_only)
    def surface_hide(self,profile=None):return self.bridge.call('hide',profile=profile)

    def preview_profile(self,profile,username,action):
        row=self.get(profile)
        with self._profile_lock(profile):
            state=self.connection_endpoint(profile,open_if_needed=True)
            return self.bridge.call('profile-preview',profile=profile,owner=row['owner_user_id'],generation=state['generation'],username=username,action=action)

    def whatsapp_diagnostics(self,owner,profile):
        self.assert_owner(owner,profile)
        endpoint=self.connection_endpoint(profile,open_if_needed=False)
        return self.bridge.call('whatsapp-diagnostics',owner=owner,profile=profile,generation=endpoint['generation'])

    def open_instagram(self,profile):
        row=self.get(profile)
        with self._profile_lock(profile):
            with self.lock:self._check_ticket(None,profile)
            result=self.bridge.call('open-instagram',profile=profile,owner=row['owner_user_id'],proxy=row['proxy_server'],name=row['name'])
            with self.lock:
                self._check_ticket(None,profile)
                self.processes[profile]={'profile':profile,'ws':result['ws'],'generation':result['generation']}
                self.generations[profile]=result['generation']
            return {k:v for k,v in result.items() if k not in {'ws','generation'}}

    def open_whatsapp(self,profile,url,cookies=None):
        row=self.get(profile)
        with self._profile_lock(profile):
            with self.lock:self._check_ticket(None,profile)
            result=self.bridge.call('open-whatsapp',profile=profile,owner=row['owner_user_id'],proxy=row['proxy_server'],name=row['name'],url=url,cookies=cookies)
            with self.lock:
                self._check_ticket(None,profile)
                self.processes[profile]={'profile':profile,'ws':result['ws'],'generation':result['generation']}
                self.generations[profile]=result['generation']
            return {k:v for k,v in result.items() if k not in {'ws','generation'}}

    def chat_translation(self,owner,profile,step):
        self.assert_owner(owner,profile)
        endpoint=self.connection_endpoint(profile,open_if_needed=False)
        return self.bridge.call('chat-translation',owner=owner,profile=profile,generation=endpoint['generation'],step=step)

    def reset_whatsapp_storage(self,owner,profile):
        self.assert_owner(owner,profile)
        self.close_profile(profile)
        return self.bridge.call('reset-whatsapp-storage',owner=owner,profile=profile)

    def navigate_profile(self,profile,platform,action):
        from .account_platforms import platform_config
        row=self.get(profile)
        with self._profile_lock(profile):
            state=self.connection_endpoint(profile,open_if_needed=False)
            return self.bridge.call('manual-navigation',profile=profile,owner=row['owner_user_id'],generation=state['generation'],action=action,homeUrl=platform_config(platform)['url'])

    def shutdown(self):
        # Core restarts must leave persistent login sessions alive in Electron.
        # Electron closes and flushes its own pages on application exit.
        with self.lock:self.stopping=True;self.connections.clear();self.actions.clear();self.action_leases.clear();self.processes.clear()
        self.surface_hide()
