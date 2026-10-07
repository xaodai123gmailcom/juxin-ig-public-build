from __future__ import annotations
import asyncio
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError as ModelError
from .errors import ConflictError, NotFoundError, ValidationError
from .bitbrowser_v2 import validate_profile_id
from .service import isoformat
from .account_platforms import (platform_config, platform_from_environment, platform_for_profile,
                               parse_cookies, open_platform_window)


def initialize_account_schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS account_window_plans (
        id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL REFERENCES app_users(id),
        serial INTEGER NOT NULL, name TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
        group_name TEXT NOT NULL DEFAULT '', profile_id TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '', environment_json TEXT NOT NULL DEFAULT '{}',
        revision INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(owner_user_id,serial))''')
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS account_window_unique_binding ON account_window_plans(profile_id) WHERE profile_id<>'' AND archived=0")
    # Inventory checks both archived and active bindings. The active-only unique
    # index cannot serve its first NOT EXISTS probe and caused a full plan scan
    # for every profile, including when all windows were already open.
    c.execute('CREATE INDEX IF NOT EXISTS account_window_profile_archive ON account_window_plans(profile_id,archived)')
    c.execute('''CREATE TABLE IF NOT EXISTS account_window_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, owner_user_id TEXT NOT NULL REFERENCES app_users(id),
        plan_id TEXT NOT NULL, name TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL)''')
    c.execute('CREATE TABLE IF NOT EXISTS account_window_order (owner_user_id TEXT PRIMARY KEY REFERENCES app_users(id), ids_json TEXT NOT NULL)')
    c.execute('INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(27,?)',(isoformat(),))
    from .account_restore import initialize_restore_schema
    initialize_restore_schema(c)
    c.execute('CREATE INDEX IF NOT EXISTS account_events_owner ON account_window_events(owner_user_id,seq DESC)')
    c.execute('INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(21,?)',(isoformat(),))


class WindowPlan(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id: str=''
    revision: int=Field(default=0,ge=0)
    name: str=Field(min_length=1,max_length=80)
    username: str=Field(default='',max_length=160)
    platform: str=Field(default='instagram',max_length=40)
    custom_url: str=Field(default='',max_length=2048)
    group: str=Field(default='',max_length=80)
    profile_id: str=Field(default='',max_length=128)
    notes: str=Field(default='',max_length=1000)
    language: str=Field(default='简体中文',max_length=40)
    timezone: str=Field(default='跟随窗口',max_length=80)
    proxy_note: str=Field(default='',max_length=160)
    native: bool=False
    proxy_server: str=Field(default='',max_length=240)


class AccountWorkspace:
    def __init__(self,service,bitbrowser,*,opener=None):
        self.service=service;self.db=service.database;self.bitbrowser=bitbrowser
        self.opener=opener
        from .account_restore import AccountWindowRestore
        self.restore=AccountWindowRestore(self)
        from .account_surface import AccountSurface
        self.surface=AccountSurface(service,getattr(self.bitbrowser,'native',None))
        self.service.before_browser_operation=self.surface.suspend

    def _open(self,profile,platform,cookies=None,*,owner=None):
        platform_config(platform)
        if owner:
            with self.db.read() as c:
                if c.execute('SELECT 1 FROM retired_account_profiles WHERE owner_user_id=? AND profile_id=?',(owner,profile)).fetchone():
                    raise ValidationError('此窗口已停用，请先明确配置为 Instagram 窗口')
        provider=self.bitbrowser
        if self.opener:result=self.opener(provider,profile,platform,cookies)
        else:result=asyncio.run(open_platform_window(provider,profile,platform,cookies,inspect_instagram=True))
        if owner and isinstance(result,dict) and 'instagram_stats' in result:
            from .account_profile_stats import save_account_snapshot
            save_account_snapshot(self.db,owner,profile,result['instagram_stats'])
        return result

    def recover(self):
        with self.db.write() as c:c.execute("DELETE FROM browser_operation_leases WHERE operation_type='account'")

    def get(self,owner,ident):
        with self.db.read() as c:
            row=c.execute('SELECT * FROM account_window_plans WHERE id=? AND owner_user_id=? AND archived=0',(ident,owner)).fetchone()
        if row is None or platform_from_environment(row['environment_json']) == 'unknown':raise NotFoundError('账号窗口方案不存在或平台已停用')
        return dict(row)

    @contextmanager
    def profile_guard(self,owner,profiles):
        leases=[]
        try:
            for profile in sorted(set(p for p in profiles if p)):
                profile=validate_profile_id(profile)
                token=self.service.acquire_browser_lease(owner,profile,operation_type='account',entity_id=str(uuid.uuid4()),ttl_seconds=600)
                leases.append((profile,token))
            yield
        finally:
            for profile,token in reversed(leases):self.service.release_browser_lease(profile,token)

    def control_profile(self,owner,profile,operation):
        if operation not in {'open','close'}:raise ValidationError('无效窗口操作')
        with self.profile_guard(owner,[profile]):
            if operation=='close':result=self.bitbrowser.close_profile(profile)
            else:
                with self.db.read() as c:platform=platform_for_profile(c,profile,owner_user_id=owner)
                if platform=='custom':
                    from .account_platforms import destination_from_environment
                    with self.db.read() as c:row=c.execute('SELECT environment_json FROM account_window_plans WHERE profile_id=? AND owner_user_id=? ORDER BY archived,updated_at DESC LIMIT 1',(profile,owner)).fetchone()
                    platform=destination_from_environment(row[0])
                result=self._open(profile,platform,owner=owner)
            # Older window controls are explicit manual intent too. Persist it
            # under the same lease instead of guessing from task-opened pages at
            # shutdown. Unbound profiles have no account plan to restore.
            if profile.startswith('native:'):
                with self.db.write() as c:
                    row=c.execute('SELECT id FROM account_window_plans WHERE profile_id=? AND owner_user_id=? AND archived=0',(profile,owner)).fetchone()
                    if row:self.restore.remember(c,owner,row['id'],operation=='open')
            return result

    def _event(self,c,owner,ident,name,action):
        c.execute('INSERT INTO account_window_events(owner_user_id,plan_id,name,action,created_at) VALUES(?,?,?,?,?)',(owner,ident,name,action,isoformat()))

    def save(self,owner,payload):
        try:config=WindowPlan.model_validate(payload)
        except ModelError as e:raise ValidationError('; '.join(v['msg'] for v in e.errors())) from None
        config.name=config.name.strip()
        if not config.name:raise ValidationError('请输入窗口名称')
        old=self.get(owner,config.id) if config.id else None
        if old and 'platform' not in config.model_fields_set:
            config.platform=platform_from_environment(old['environment_json'])
        from .account_platforms import custom_url
        if config.platform=='custom':config.custom_url=custom_url(config.custom_url)
        else:platform_config(config.platform);config.custom_url=''
        config.username=config.username.strip()
        if config.platform=='instagram' and not re.fullmatch(r'@?[A-Za-z0-9._]{0,30}',config.username):
            raise ValidationError('Instagram 用户名只能包含字母、数字、点和下划线，最多 30 个字符')
        if any(ord(c)<32 for c in config.username):raise ValidationError('账号名称不能包含控制字符')
        native=getattr(self.bitbrowser,'native',None)
        using_native=config.native or bool(old and old['profile_id'].startswith('native:'))
        if using_native and native is None:raise ValidationError('当前运行版本不支持内置窗口')
        if using_native:
            from .native_browser import validate_proxy
            config.proxy_server=validate_proxy(config.proxy_server)
            config.profile_id=old['profile_id'] if old and old['profile_id'].startswith('native:') else ''
        if config.profile_id and not using_native:
            listing=self.bitbrowser.list_all_windows()
            if listing.get('stale') or listing.get('legacy_stale',False):raise ConflictError('窗口列表未更新，请连接 BitBrowser 并刷新后再绑定')
            if config.profile_id not in {p['id'] for p in listing.get('windows',[])}:raise ValidationError('未找到可绑定的窗口，请刷新窗口列表')
        ident=config.id or str(uuid.uuid4());now=isoformat()
        environment=json.dumps({'platform':config.platform,'language':config.language,'timezone':config.timezone,'proxy_note':config.proxy_note,'custom_url':config.custom_url},ensure_ascii=False)
        with self.profile_guard(owner,[config.profile_id,old['profile_id'] if old else '']):
            if old and old['profile_id'] and (config.platform!=platform_from_environment(old['environment_json']) or config.custom_url!=json.loads(old['environment_json']).get('custom_url','')):
                listing=self.bitbrowser.list_all_windows()
                if listing.get('stale'):raise ConflictError('请刷新窗口状态后再更改平台')
                window=next((r for r in listing.get('windows',[]) if r['id']==old['profile_id']),None)
                if window is None or window.get('is_open',window.get('opened',False)):
                    raise ConflictError('请先关闭该窗口，再更改软件平台')
            try:
                with self.db.write() as c:
                    retired=c.execute('SELECT 1 FROM retired_account_profiles WHERE owner_user_id=? AND profile_id=?',(owner,config.profile_id)).fetchone()
                    if retired and (config.platform!='instagram' or 'platform' not in config.model_fields_set):
                        raise ValidationError('此窗口已停用，请先明确配置为 Instagram 窗口')
                    if using_native:
                        if config.profile_id:native.update(c,owner,config.profile_id,config.name,config.group.strip(),config.proxy_server)
                        else:config.profile_id=native.create(c,owner,config.name,config.group.strip(),config.proxy_server)
                    if old:
                        changed=c.execute('''UPDATE account_window_plans SET name=?,username=?,group_name=?,profile_id=?,notes=?,environment_json=?,revision=revision+1,updated_at=?
                            WHERE id=? AND owner_user_id=? AND revision=? AND archived=0''',
                            (config.name,config.username.lstrip('@'),config.group.strip(),config.profile_id,config.notes,environment,now,ident,owner,config.revision)).rowcount
                        if not changed:raise ConflictError('这个方案已被更新，请刷新后再编辑')
                    else:
                        serial=c.execute('SELECT COALESCE(max(serial),0)+1 FROM account_window_plans WHERE owner_user_id=?',(owner,)).fetchone()[0]
                        c.execute('''INSERT INTO account_window_plans(id,owner_user_id,serial,name,username,group_name,profile_id,notes,environment_json,created_at,updated_at)
                                     VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(ident,owner,serial,config.name,config.username.lstrip('@'),config.group.strip(),config.profile_id,config.notes,environment,now,now))
                    if retired:
                        c.execute('DELETE FROM retired_account_profiles WHERE owner_user_id=? AND profile_id=?',(owner,config.profile_id))
                    self._event(c,owner,ident,config.name,'更新方案' if old else '新建方案')
            except sqlite3.IntegrityError:raise ConflictError('这个浏览器窗口已经绑定其他账号方案，请使用独立窗口') from None
        return {'id':ident,'saved':True,'revision':self.get(owner,ident)['revision']}

    def command(self,owner,body):
        action=body.get('action');restoring=action=='restore'
        if restoring:action='open'
        if action=='reorder':
            ids=body.get('ids')
            if not isinstance(ids,list) or not all(isinstance(i,str) for i in ids) or len(ids)!=len(set(ids)):raise ValidationError('窗口排序无效')
            with self.db.write() as c:
                current={r['id'] for r in c.execute('SELECT id,environment_json FROM account_window_plans WHERE owner_user_id=? AND archived=0',(owner,)) if platform_from_environment(r['environment_json']) != 'unknown'}
                if set(ids)!=current:raise ConflictError('窗口列表已变化，请刷新后重新排序')
                c.execute('INSERT INTO account_window_order VALUES(?,?) ON CONFLICT(owner_user_id) DO UPDATE SET ids_json=excluded.ids_json',(owner,json.dumps(ids)))
            return {'sorted':True}
        if action=='save_with_cookies':
            payload=body.get('plan',{})
            if not isinstance(payload,dict):raise ValidationError('窗口配置无效')
            platform={'id':'custom','url':payload.get('custom_url','')} if payload.get('platform')=='custom' else payload.get('platform','instagram')
            parse_cookies(body.get('cookie_text'),platform)  # validate before creating a plan
            saved=self.save(owner,payload)
            try:
                result=self.command(owner,{'action':'import_cookies','id':saved['id'],'revision':saved['revision'],'cookie_text':body.get('cookie_text')})
                return {**saved,**result,'cookie_failed':result.get('page_loaded') is False}
            except Exception:
                return {**saved,'cookie_failed':True,'login_verified':False,'message':'窗口已保存，Cookie 导入或打开未完成。可重试导入，不会重复创建窗口。'}
        if action=='save':return self.save(owner,body.get('plan',{}))
        if action=='batch_create':
            from .account_batch import create_batch
            return create_batch(self,owner,body)
        row=self.get(owner,str(body.get('id','')))
        if action=='whatsapp_diagnostics':
            from .account_platforms import destination_from_environment, is_whatsapp_destination
            native=getattr(self.bitbrowser,'native',None)
            if not is_whatsapp_destination(destination_from_environment(row['environment_json'])) or not row['profile_id'].startswith('native:') or not callable(getattr(native,'whatsapp_diagnostics',None)):
                raise ValidationError('请选择内置 WhatsApp 窗口')
            with self.profile_guard(owner,[row['profile_id']]):
                return native.whatsapp_diagnostics(owner,row['profile_id'])
        if action=='chat_translation':
            from .account_platforms import destination_from_environment, is_whatsapp_destination
            native=getattr(self.bitbrowser,'native',None)
            step=body.get('step',{})
            if not isinstance(step,dict) or step.get('kind') not in {'check','poll','apply','export'} or not row['profile_id'].startswith('native:') or (destination_from_environment(row['environment_json'])!='instagram' and not is_whatsapp_destination(destination_from_environment(row['environment_json']))):
                raise ValidationError('请选择内置 Instagram 或 WhatsApp 聊天窗口')
            if step['kind']=='check':return {'profile':row['profile_id']}
            if not callable(getattr(native,'chat_translation',None)):raise ValidationError('请更新桌面程序')
            # Keep the same surface -> SQLite lock order as task acquisition
            # and AccountSurface.update. Close the read connection before slow
            # desktop IPC: collection writes and lease heartbeats must continue.
            # Plan edits also acquire an account lease through this fence. Do
            # not take one here: doing so would hide the chat on every poll.
            with self.db.browser_surface_lock:
                with self.db.read() as c:
                    current=c.execute('SELECT profile_id,revision FROM account_window_plans WHERE id=? AND owner_user_id=? AND archived=0',(row['id'],owner)).fetchone()
                    if not current or current['profile_id']!=row['profile_id'] or current['revision']!=row['revision']:
                        raise ConflictError('窗口方案已变化，请刷新后再操作')
                    lease=c.execute('SELECT 1 FROM browser_operation_leases WHERE profile_id=? ',(row['profile_id'],)).fetchone()
                    from .browser_admission import assert_no_durable_window_hold
                    assert_no_durable_window_hold(c,owner,row['profile_id'])
                    if lease:raise ConflictError('任务占用，同步翻译已暂停')
                return native.chat_translation(owner,row['profile_id'],step)
        if action=='notes':
            notes=body.get('notes')
            if not isinstance(notes,str) or len(notes)>1000:raise ValidationError('备注最多 1000 字')
            with self.profile_guard(owner,[row['profile_id']]):
                with self.db.write() as c:
                    if c.execute('UPDATE account_window_plans SET notes=?,revision=revision+1,updated_at=? WHERE id=? AND owner_user_id=? AND revision=? AND archived=0',(notes,isoformat(),row['id'],owner,body.get('revision'))).rowcount!=1:raise ConflictError('窗口已变化，请刷新后再更新备注')
                    self._event(c,owner,row['id'],row['name'],'更新备注')
            return {'saved':True}
        if action in {'archive','delete'}:
            with self.profile_guard(owner,[row['profile_id']]):
                current=self.get(owner,row['id'])
                if current['revision']!=body.get('revision'):raise ConflictError('方案已变化，请刷新后重试')
                if action=='delete' and row['profile_id']:
                    self.bitbrowser.close_profile(row['profile_id'])
                with self.db.write() as c:
                    if c.execute('UPDATE account_window_plans SET archived=1,revision=revision+1,updated_at=? WHERE id=? AND owner_user_id=? AND revision=? AND archived=0',(isoformat(),row['id'],owner,body.get('revision'))).rowcount!=1:
                        raise ConflictError('方案已变化，请刷新后重试')
                    self._event(c,owner,row['id'],row['name'],'删除窗口（保留历史和登录资料）' if action=='delete' else '移出账号列表（保留历史）')
            return {'archived':True}
        if action not in {'open','close','import_cookies','home','refresh','back','forward','inbox','profile_preview','reset_whatsapp_storage'}:raise ValidationError('无效账号窗口操作')
        if not row['profile_id']:raise ValidationError('这是窗口方案，请先绑定已有 BitBrowser 窗口')
        # Keep the binding and its window protected together through the action.
        with self.profile_guard(owner,[row['profile_id']]):
            current=self.get(owner,row['id'])
            if current['revision']!=row['revision']:raise ConflictError('窗口绑定已变化，请刷新后再操作')
            if restoring:
                with self.db.read() as c:remembered=c.execute('SELECT opened FROM account_window_open_state WHERE plan_id=? AND owner_user_id=?',(row['id'],owner)).fetchone()
                if not remembered or not remembered[0]:return {'skipped':True}
            from .account_platforms import destination_from_environment, is_whatsapp_destination
            platform=destination_from_environment(current['environment_json'])
            if action=='reset_whatsapp_storage':
                native=getattr(self.bitbrowser,'native',None)
                if not is_whatsapp_destination(platform) or body.get('confirm') is not True or body.get('revision')!=current['revision'] or not callable(getattr(native,'reset_whatsapp_storage',None)):raise ValidationError('请确认重置当前 WhatsApp 窗口的登录资料')
                result=native.reset_whatsapp_storage(owner,row['profile_id'])
            elif action=='profile_preview':
                native=getattr(self.bitbrowser,'native',None)
                username=body.get('username','');preview_action=body.get('preview_action','target')
                if platform!='instagram' or not row['profile_id'].startswith('native:') or not callable(getattr(native,'preview_profile',None)):
                    raise ValidationError('请选择一个内置 Instagram 账号窗口查看目标主页')
                if not isinstance(username,str) or not re.fullmatch(r'[A-Za-z0-9._]{1,30}',username) or preview_action not in ('target','login'):
                    raise ValidationError('目标账号或预览操作无效')
                result=native.preview_profile(row['profile_id'],username,preview_action)
            elif action=='close':result=self.bitbrowser.close_profile(row['profile_id'])
            elif action in {'home','refresh','back','forward','inbox'}:
                native=getattr(self.bitbrowser,'native',None)
                if action in {'home','inbox'} and row['profile_id'].startswith('native:') and is_whatsapp_destination(platform):
                    from .account_platforms import platform_config
                    result=native.open_whatsapp(row['profile_id'],platform_config(platform)['url'])
                elif action in {'refresh','back','forward'} and row['profile_id'].startswith('native:') and callable(getattr(native,'navigate_profile',None)):
                    result=native.navigate_profile(row['profile_id'],platform,action)
                else:
                    from .account_navigation import navigate_account
                    result=asyncio.run(navigate_account(self.bitbrowser,row['profile_id'],platform,action))
            else:
                if action=='import_cookies' and body.get('revision')!=current['revision']:
                    raise ConflictError('窗口方案已变化，请刷新后重新导入')
                cookies=parse_cookies(body.get('cookie_text'),platform) if action=='import_cookies' else None
                result=self._open(row['profile_id'],platform,cookies,owner=owner)
            message={'reset_whatsapp_storage':'重置本窗口 WhatsApp 登录资料','open':'打开平台窗口','close':'关闭窗口','import_cookies':'导入 Cookie（登录状态待平台确认）','home':'打开主页','refresh':'刷新网页','back':'上一页','forward':'下一页','inbox':'打开私信','profile_preview':'查看目标主页'}[action]
            if result.get('page_loaded') is False:
                message+='失败：'+str(result.get('message') or '页面未加载')[:300]
            with self.db.write() as c:
                self._event(c,owner,row['id'],row['name'],message)
                if row['profile_id'].startswith('native:'):
                    self.restore.remember(c,owner,row['id'],action not in {'close','reset_whatsapp_storage'})
        return result

    def watch_task(self,owner,ident,target=None):
        row=self.get(owner,ident);native=getattr(self.bitbrowser,'native',None)
        if not row['profile_id'].startswith('native:') or not hasattr(native,'bridge'):raise ValidationError('此窗口不支持任务画面')
        native.assert_owner(owner,row['profile_id'])
        with self.db.read() as c:
            lease=c.execute('SELECT lease_token,operation_type,entity_id FROM browser_operation_leases WHERE profile_id=? ',(row['profile_id'],)).fetchone()
            from .posting_retirement import legacy_studio_lease_entities
            retired=bool(lease and (lease['operation_type']=='posting' or
                lease['operation_type']=='studio' and lease['entity_id'] in legacy_studio_lease_entities(c)))
            task=c.execute('SELECT settings_json FROM tasks WHERE id=? AND owner_user_id=?',
                (lease['entity_id'],owner)).fetchone() if lease and lease['operation_type']=='collection' else None
        slot_count=0
        if task:
            configured=json.loads(task['settings_json']).get('parallel_screening_workers',1)
            slot_count=max(1,min(configured,3)) if type(configured) is int else 1
        result=native.bridge.call('watch-profile',profile=row['profile_id'],owner=owner,target=target,prefer_ready=not bool(target))
        return {**result,'task_key':self.surface.task_key(lease),'operation':('account' if retired else lease['operation_type']) if lease else None,
                'screening_slots':slot_count,
                'manual_control':self.surface.manual_info(owner,row)}

    def unread_snapshot(self,owner):
        from .account_unread import account_unread_snapshot
        return account_unread_snapshot(self.db,getattr(self.bitbrowser,'native',None),owner)

    def task_page_close_info(self,owner,ident,task_key,target,*,operation_label='关闭'):
        if not isinstance(target,str) or not target or len(target)>128:raise ValidationError(f'请选择要{operation_label}的任务页面')
        row=self.get(owner,ident);native=getattr(self.bitbrowser,'native',None)
        if not row['profile_id'].startswith('native:') or not hasattr(native,'bridge'):raise ValidationError('仅支持内置任务页面')
        native.assert_owner(owner,row['profile_id'])
        with self.db.read() as c:
            lease=c.execute('SELECT * FROM browser_operation_leases WHERE profile_id=? ',(row['profile_id'],)).fetchone()
        if not lease or self.surface.task_key(lease)!=task_key:raise ConflictError('任务状态已变化，请重新查看')
        if lease['operation_type']!='collection':raise ValidationError(f'当前仅支持{operation_label}采集和筛选任务页面')
        pages=native.bridge.call('watch-profile',profile=row['profile_id'],owner=owner,target=target)
        if not any(p['id']==target for p in pages['pages']):raise ConflictError('该任务页面已经关闭')
        return {'row':row,'target':target,'generation':pages['generation'],'lease_token':lease['lease_token'],'task_key':task_key}

    def close_task_page(self,owner,info):
        # Pause was acknowledged by the execution manager first. Keep the lease
        # and binding stable through native destruction; no profile-wide close.
        row=info['row'];native=self.bitbrowser.native
        with self.db.write() as c:
            current=c.execute('SELECT * FROM account_window_plans WHERE id=? AND owner_user_id=? AND archived=0',(row['id'],owner)).fetchone()
            lease=c.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=? ',(row['profile_id'],)).fetchone()
            if not current or current['revision']!=row['revision'] or current['profile_id']!=row['profile_id'] or not lease or lease['lease_token']!=info['lease_token']:raise ConflictError('任务或窗口已变化，未关闭新页面')
            result=native.bridge.call('close-task-page',profile=row['profile_id'],owner=owner,generation=info['generation'],target=info['target'])
            self._event(c,owner,row['id'],row['name'],'关闭一个任务子页面；此账号任务已暂停，可从采集任务继续')
        return {**result,'message':'已完全关闭此页；该账号任务已暂停，可在采集任务中继续。'}

    def snapshot(self,owner,*,window_listing=None):
        native=getattr(self.bitbrowser,'native',None)
        with self.db.read() as c:
            # One coherent read replaces a new SQLite connection per profile.
            # Release it before desktop I/O; live task leases remain uncached.
            c.execute('BEGIN')
            plans=[dict(r) for r in c.execute('''SELECT p.*,n.proxy_server AS native_proxy_server
                FROM account_window_plans p LEFT JOIN native_browser_profiles n
                  ON n.id=p.profile_id AND n.owner_user_id=p.owner_user_id
                WHERE p.owner_user_id=? AND p.archived=0 ORDER BY p.serial''',(owner,))]
            excluded_profiles={p['profile_id'] for p in plans if platform_from_environment(p['environment_json']) == 'unknown'}
            excluded_profiles.update(r[0] for r in c.execute('SELECT profile_id FROM retired_account_profiles WHERE owner_user_id=?',(owner,)))
            plans=[p for p in plans if platform_from_environment(p['environment_json']) != 'unknown' and p['profile_id'] not in excluded_profiles]
            order=c.execute('SELECT ids_json FROM account_window_order WHERE owner_user_id=?',(owner,)).fetchone()
            positions={ident:i for i,ident in enumerate(json.loads(order[0]))} if order else {}
            plans.sort(key=lambda r:(positions.get(r['id'],len(positions)),r['serial']))
            events=[dict(r) for r in c.execute('SELECT name,action,created_at FROM account_window_events WHERE owner_user_id=? ORDER BY seq DESC LIMIT 100',(owner,))]
            dm={r['profile_id']:{'count':r['last_dm_count'],'checked_at':r['checked_at']} for r in c.execute('SELECT profile_id,last_dm_count,checked_at FROM follow_monitor_dm_accounts WHERE owner_user_id=?',(owner,))}
            following={r['profile_id']:{'username':r['username'],'count':r['following_count'],'checked_at':r['checked_at']} for r in c.execute('SELECT profile_id,username,following_count,checked_at FROM follow_monitor_accounts WHERE owner_user_id=?',(owner,))}
            from .posting_retirement import legacy_window_state
            lease_rows=list(c.execute('SELECT profile_id,operation_type,owner_user_id,entity_id FROM browser_operation_leases'))
            retired_studio_ids,retired_holds,cleanup_rows=legacy_window_state(c,leases=lease_rows,include_nurture=True)
            leases={}
            actual_profiles=set()
            for r in lease_rows:
                actual_profiles.add(r['profile_id'])
                retired=(r['operation_type']=='posting' or r['operation_type']=='studio' and r['entity_id'] in retired_studio_ids)
                leases[r['profile_id']]={'profile_id':r['profile_id'],'operation_type':'account' if retired else r['operation_type']}
                if retired:
                    leases[r['profile_id']].update(state='occupied',entity_id=None)
                    if r['owner_user_id']==owner:leases[r['profile_id']]['can_reconcile_window_state']=True
            for r in retired_holds:
                if r['profile_id'] in actual_profiles:continue
                leases[r['profile_id']]={'profile_id':r['profile_id'],'operation_type':'account','state':'occupied','entity_id':None}
                if r['owner_user_id']==owner and not r['_legacy_owner_ambiguous']:leases[r['profile_id']]['can_reconcile_window_state']=True
            # Durable cleanup receipts also block admission when their lease is
            # missing. Show the same occupancy the command path enforces.
            for r in cleanup_rows:
                leases.setdefault(r['profile_id'], {'profile_id':r['profile_id'],'operation_type':'studio','state':'cleanup_pending'})
                leases[r['profile_id']]['cleanup_required']=True
            opened_times={r['plan_id']:r['opened_at'] for r in c.execute("SELECT plan_id,max(created_at) AS opened_at FROM account_window_events WHERE owner_user_id=? AND (action LIKE '打开平台窗口%' OR action LIKE '导入 Cookie%') GROUP BY plan_id",(owner,))}
        for p in plans:
            p['last_opened_at']=opened_times.get(p['id'])
            p['native']=p['profile_id'].startswith('native:')
            p['proxy_server']=''
            native_proxy=p.pop('native_proxy_server')
            if p['native'] and native:
                if native_proxy is None:raise NotFoundError('内置窗口不存在')
                p['proxy_server']=native_proxy
            p['group']=p.pop('group_name');p['platform']=platform_from_environment(p['environment_json'])
            p['environment']=json.loads(p.pop('environment_json'));p['environment'].pop('platform',None)
            p['custom_url']=p['environment'].get('custom_url','')
            p.pop('owner_user_id');p.pop('archived')
        from .account_platforms import PLATFORMS,UNAVAILABLE_PLATFORMS
        # HTTP readers share the bounded provider read with the workbench.
        # Never launch another desktop RPC to refresh this independent view.
        rows = window_listing['windows'] if window_listing is not None else (
            native.inventory() if callable(getattr(native,'inventory',None)) else [])
        windows=[{'id':w['id'], 'name':w.get('name',w['id']),
                  'opened':bool(w.get('is_open')), 'group':w.get('group'),
                  'serial_number':w.get('serial_number'), 'provider_order':w.get('provider_order'),
                  'ready':w.get('ready',False), 'opening':w.get('opening',False),
                  'window_state':w.get('window_state'), 'generation':w.get('generation',0)}
                 for w in rows if w.get('owner_user_id',owner)==owner and w['id'] not in excluded_profiles]
        if window_listing is None:
            # Preserve the compact contract for existing direct service callers;
            # the independent HTTP route explicitly requests enriched inventory.
            windows=[{'id':w['id'],'opened':w['opened']} for w in windows]
        return {'restore':self.restore.status(owner),'windows':windows,
                'inventory_stale':bool(window_listing and window_listing.get('stale')),
                'connection':window_listing.get('connection',{}) if window_listing is not None else {},
                'platforms':[{'id':k,**v} for k,v in PLATFORMS.items()],'unavailable_platforms':UNAVAILABLE_PLATFORMS,'plans':plans,'events':events,'last_dm':dm,'last_following':following,'locks':leases,'cloud_sync':'not_connected','mode':'native_browser'}
