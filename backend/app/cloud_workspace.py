"""Supabase authentication and owner-scoped, optimistic-concurrency backups.

Cloud access is disabled until the user supplies and enables their own project.
Publishable keys are never used as user JWTs. Passwords and Supabase session
credentials are kept in memory only.
"""
from __future__ import annotations
import base64
import hashlib
import json
import threading
import time
import urllib.request
import urllib.error
import zlib
import os
import tempfile
from pathlib import Path
from .config import validate_cloud_configuration
from .errors import AuthenticationError, ConflictError, ValidationError, UpstreamUnavailableError
from .service import isoformat

MAX_RAW=128*1024*1024
ROOT_TABLES=('report_review_decisions','split_admission_totals','split_completed_targets','tasks','task_list_dismissals','follow_monitor_accounts','follow_monitor_members','follow_monitor_latest_follow','follow_monitor_latest_unfollow','follow_monitor_latest_dm','follow_monitor_daily_counts','follow_monitor_runs','follow_monitor_seen','follow_monitor_rounds','follow_monitor_dm_accounts','action_campaigns','action_success_ledger','private_follow_completions','action_counter_resets','task_target_recovery_controls','split_candidates','split_candidate_history','workbench_candidates','workbench_review_decisions','workbench_candidate_dismissals','workbench_collection_exclusions','event_log','studio_templates','studio_assets','studio_jobs','studio_daily_actions','account_window_plans','account_window_events','native_browser_profiles','account_creation_batches','global_identity_owners','task_result_duplicate_archive','retired_account_profiles')
CHILD_TABLES={'task_targets':('task_id','tasks'),'task_windows':('task_id','tasks'),'task_checkpoints':('task_id','tasks'),'task_mode_candidates':('target_id','task_targets'),'task_mode_candidate_counters':('target_id','task_targets'),'task_results':('task_id','tasks'),'action_targets':('campaign_id','action_campaigns'),'action_attempts':('campaign_id','action_campaigns'),'split_candidate_window_affinity':('candidate_id','split_candidates')}
TABLES=(*ROOT_TABLES,'workbench_identity_claims',*CHILD_TABLES,'instagram_accounts','instagram_username_aliases','global_seen')


def initialize_cloud_schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS cloud_workspace_links(
        owner_user_id TEXT PRIMARY KEY REFERENCES app_users(id), cloud_user_id TEXT NOT NULL UNIQUE,
        email TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, digest TEXT NOT NULL DEFAULT '', last_sync_at TEXT)''')
    if 'project_url' not in {r[1] for r in c.execute('PRAGMA table_info(cloud_workspace_links)')}:
        # Old links have no trustworthy project identity. Keep them for local
        # history, but never reuse their remote revision with a new project.
        c.execute("ALTER TABLE cloud_workspace_links ADD COLUMN project_url TEXT NOT NULL DEFAULT ''")
    c.execute('INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(23,?)',(isoformat(),))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class SupabaseAPI:
    def __init__(self, *, enabled=False, project_url='', publishable_key=''):
        try:
            self.enabled,self.project_url,self._publishable_key=validate_cloud_configuration(enabled,project_url,publishable_key)
        except ValueError as exc:
            raise ValidationError(str(exc)) from None
    @property
    def configured(self):
        return bool(self.enabled and self.project_url and self._publishable_key)
    def request(self,path,*,method='GET',token=None,body=None):
        if not self.configured:raise ValidationError('云端未启用，请先配置并启用自己的 Supabase 项目')
        if not path.startswith(('/auth/v1/','/rest/v1/')):raise ValidationError('无效云端接口')
        headers={'apikey':self._publishable_key,'content-type':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None
        try:
            with urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(self.project_url+path,data=data,headers=headers,method=method),timeout=25) as r:
                raw=r.read(50*1024*1024+1)
                if len(raw)>50*1024*1024:raise ValidationError('云端数据超过单次接收上限')
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:detail=json.loads(e.read(2048))
            except Exception:detail={}
            code=detail.get('code') or detail.get('error_code')
            if code in {'PGRST205','PGRST202'}:raise ConflictError('云端数据表尚未初始化，请在 Supabase SQL Editor 执行安装包中的 cloud/supabase-init.sql') from None
            if code=='40001':raise ConflictError('云端已有其他电脑的新版本，已停止上传，原数据保持不变') from None
            if e.code in {401,403}:raise ValidationError('云端登录已过期、账号未验证或没有数据访问权限，请重新登录') from None
            if code in {'email_not_confirmed','invalid_credentials'}:raise ValidationError('邮箱未验证或密码不正确') from None
            raise UpstreamUnavailableError(f'云端请求失败（HTTP {e.code}），请检查邮箱验证、登录信息或稍后重试') from None
        except (OSError,ValueError):raise UpstreamUnavailableError('暂时无法连接 Supabase，数据仍保存在本机') from None


def _select_ids(c,table,column,ids):
    rows=[];ids=sorted(set(ids))
    for start in range(0,len(ids),500):
        part=ids[start:start+500]
        rows.extend(dict(r) for r in c.execute(f'SELECT * FROM "{table}" WHERE "{column}" IN ({",".join("?" for _ in part)})',part))
    return rows


def export_workspace(db,owner,data_dir):
    tables={}
    with db.read() as c:
        c.execute('BEGIN')
        for table in ROOT_TABLES:
            if table == 'studio_assets':
                tables[table] = []
            else:
                scope = " AND kind='nurture'" if table in {'studio_jobs','studio_templates'} else ''
                tables[table]=[dict(r) for r in c.execute(f'SELECT * FROM "{table}" WHERE owner_user_id=?'+scope,(owner,))]
        tables['workbench_identity_claims']=[dict(r) for r in c.execute('SELECT * FROM workbench_identity_claims WHERE claimed_by_user_id=?',(owner,))]
        for table,(fk,parent) in CHILD_TABLES.items():
            # Derived by candidate triggers on restore; never replay their totals.
            tables[table]=[] if table=='task_mode_candidate_counters' else _select_ids(c,table,fk,[r['id'] for r in tables[parent]])
        ids={r['account_id'] for rows in tables.values() for r in rows if r.get('account_id')}
        # Source/action queues historically stored usernames only. Resolve those
        # owner-scoped business references too, retaining stable IDs and every
        # known alias rather than recreating just the visible name on restore.
        usernames={r['username_norm'] for table in (
            'task_targets','action_targets','action_success_ledger',
            'task_target_recovery_controls','split_candidates','split_candidate_history'
        ) for r in tables[table] if r.get('username_norm')}
        ids.update(r['account_id'] for r in _select_ids(c,'instagram_username_aliases','username_norm',usernames))
        ids.update(r['id'] for r in _select_ids(c,'instagram_accounts','current_username_norm',usernames))
        tables['instagram_accounts']=_select_ids(c,'instagram_accounts','id',ids)
        for table in ('instagram_username_aliases','global_seen'):tables[table]=_select_ids(c,table,'account_id',ids)
    assets={};root=Path(data_dir).resolve()
    for rows in tables.values():rows.sort(key=lambda r:json.dumps(r,sort_keys=True,ensure_ascii=False))
    raw=json.dumps({'format':1,'schema':23,'owner':owner,'tables':tables,'assets':assets},sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    if len(raw)>MAX_RAW:raise ValidationError('工作区超过当前云端备份上限，尚未上传任何数据')
    compressed=zlib.compress(raw,6)
    if len(compressed)>32*1024*1024:raise ValidationError('工作区压缩后超过 32 MB，尚未上传任何数据')
    digest=hashlib.sha256(raw).hexdigest()
    return {'format':1,'encoding':'zlib+base64','sha256':digest,'data':base64.b64encode(compressed).decode()},digest


def decode_workspace(payload):
    try:
        if payload['format']!=1 or payload['encoding']!='zlib+base64':raise ValueError()
        packed=base64.b64decode(payload['data'],validate=True)
        decoder=zlib.decompressobj();raw=decoder.decompress(packed,MAX_RAW+1)
        if len(raw)>MAX_RAW or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:raise ValueError()
        if hashlib.sha256(raw).hexdigest()!=payload['sha256']:raise ValueError()
        data=json.loads(raw)
        if data['format']!=1 or data['schema']!=23:raise ValueError()
        # Older owner-scoped backups omit these additive registries. Missing
        # provenance is reconstructed only from that owner's business evidence.
        optional={'report_review_decisions','account_creation_batches','global_identity_owners','task_result_duplicate_archive','split_admission_totals','split_completed_targets','private_follow_completions','retired_account_profiles'}
        present=set(data['tables'])
        if present <= set(TABLES) and set(TABLES)-present <= optional:
            for table in optional-present:data['tables'][table]=[]
        if set(data['tables'])!=set(TABLES):raise ValueError()
        return data
    except (KeyError,TypeError,ValueError,zlib.error):raise ValidationError('云端备份格式或完整性校验失败，未修改本机数据') from None


def workspace_is_empty(c,owner):
    return not any(c.execute(f'SELECT 1 FROM "{t}" WHERE owner_user_id=? LIMIT 1',(owner,)).fetchone() for t in ROOT_TABLES if t != 'retired_account_profiles')


def _validate_workspace_relations(c, tables):
    """A backup must contain its own graph, never borrow destination records.

    SQLite foreign keys alone accept references to an unrelated local owner's
    existing task/identity. Validate against payload rows before any insertion,
    and check redundant task/target links agree, not just that each ID exists.
    """
    for rows in tables.values():
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValidationError('云端表记录格式不正确，未恢复数据')
    parent_keys = {}
    for table, rows in tables.items():
        foreign_keys = {}
        for key in c.execute(f'PRAGMA foreign_key_list("{table}")'):
            if key['table'] == 'app_users':
                # Owner fields are remapped and checked by import_workspace.
                continue
            foreign_keys.setdefault(key['id'], []).append(key)
        for group in foreign_keys.values():
            group.sort(key=lambda key: key['seq'])
            parent = group[0]['table']
            columns = tuple(key['to'] for key in group)
            if parent not in tables:
                raise ValidationError('备份关联表缺失，未恢复数据')
            cache_key = (parent, columns)
            try:
                if cache_key not in parent_keys:
                    parent_keys[cache_key] = {tuple(row.get(column) for column in columns) for row in tables[parent]}
                for row in rows:
                    values = tuple(row.get(key['from']) for key in group)
                    if all(value is not None for value in values) and values not in parent_keys[cache_key]:
                        raise ValidationError('备份包含工作区外的记录关联，未恢复数据')
            except TypeError:
                raise ValidationError('备份关联标识格式不正确，未恢复数据') from None
    for table, parent, scope in (
        ('task_checkpoints', 'task_targets', 'task_id'),
        ('task_results', 'task_targets', 'task_id'),
        ('action_attempts', 'action_targets', 'campaign_id'),
    ):
        owners = {row.get('id'): row.get(scope) for row in tables[parent]}
        for row in tables[table]:
            if owners.get(row.get('target_id')) != row.get(scope):
                raise ValidationError('备份中的任务与目标归属不一致，未恢复数据')


def _archive_retired_cloud_content(db, payload, data):
    """Keep the original verified download locally before excluding retired content.

    A digest-named file is immutable. Existing bytes are verified rather than
    overwritten; failure leaves the active workspace transaction untouched.
    The archive is never exported by the owner-scoped cloud table allowlist.
    """
    tables=data['tables']
    retired = bool(tables['studio_assets'] or data.get('assets') or any(
        row.get('kind') != 'nurture' for table in ('studio_jobs','studio_templates') for row in tables[table]))
    if not retired:return
    from .posting_retirement import _archive_location, _reject_symlinks, _sync_dir
    data_root,archive_root,_=_archive_location(db)
    root=archive_root/'cloud-snapshots'
    _reject_symlinks(root)
    root.mkdir(mode=0o700,exist_ok=True)
    raw=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    digest=hashlib.sha256(raw).hexdigest()
    path=root/(digest+'.json')
    _reject_symlinks(path)
    if not path.exists():
        fd,temporary=tempfile.mkstemp(prefix='.archive-',dir=root)
        try:
            with os.fdopen(fd,'wb') as out:
                out.write(raw);out.flush();os.fsync(out.fileno())
            if Path(temporary).read_bytes()!=raw:raise ValidationError('历史备份归档校验失败，未恢复数据')
            os.replace(temporary,path)
            _sync_dir(root)
            _sync_dir(archive_root)
            _sync_dir(data_root)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
    if path.read_bytes()!=raw or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
        raise ValidationError('历史备份归档校验失败，未恢复数据')
    # Decode the read-back file too, proving embedded media and original table
    # records can be recovered using the normal backup integrity contract.
    if decode_workspace(json.loads(path.read_bytes()))!=data:
        raise ValidationError('历史备份归档内容校验失败，未恢复数据')
    tables['studio_assets']=[]
    data['assets']={}
    for table in ('studio_jobs','studio_templates'):
        tables[table]=[row for row in tables[table] if row.get('kind')=='nurture']


def import_workspace(db,owner,data_dir,payload,*,check_active=lambda:None):
    check_active()
    data=decode_workspace(payload);tables=data['tables'];source=data['owner']
    with db.read() as c:
        _validate_workspace_relations(c,tables)
    for records in tables.values():
        for row in records:
            for key in ('owner_user_id','claimed_by_user_id'):
                if key in row and row[key]!=source:raise ValidationError('备份包含其他用户的数据，拒绝恢复')
    # All uploaded fields are validated against a local allowlist and local schema.
    with db.write() as c:
        if c.execute('SELECT 1 FROM browser_operation_leases WHERE owner_user_id=? LIMIT 1',(owner,)).fetchone():
            raise ConflictError('窗口仍由任务持有，不能恢复覆盖工作区')
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='posting_jobs'").fetchone() and c.execute('SELECT 1 FROM posting_jobs WHERE owner_user_id=? LIMIT 1',(owner,)).fetchone():
            raise ConflictError('历史窗口状态仍待核验，不能恢复覆盖工作区')
        if not workspace_is_empty(c,owner):raise ConflictError('本机已有业务数据；为保留原记录，不会用云端备份覆盖。请在新的本机账号中登录此云端账号恢复')
        from .posting_retirement import PostingRetirementError
        try:_archive_retired_cloud_content(db,payload,data)
        except (PostingRetirementError,OSError) as exc:
            raise ValidationError('历史备份归档校验失败，未恢复数据') from exc
        c.execute('PRAGMA defer_foreign_keys=ON')
        next_serial=c.execute('SELECT COALESCE(max(serial),0) FROM native_browser_profiles').fetchone()[0]
        for table in TABLES:
            if table=='task_mode_candidate_counters':continue
            columns={r[1] for r in c.execute(f'PRAGMA table_info("{table}")')}
            for original in tables[table]:
                check_active()
                row=dict(original)
                if table=='task_targets':row.setdefault('source_profile_json','{}')
                if table=='split_completed_targets':row.setdefault('completion_details_json','{}')
                if table=='workbench_candidates':
                    # Pre-r54 backups have no manual review layers.
                    row.setdefault('review_stage',1)
                    row.setdefault('review_transferred_at',None)
                if table=='studio_jobs':
                    for name,default in {'deleted_at':None,'source_draft_id':'','draft_target_profile_id':''}.items():
                        if name in columns:row.setdefault(name,default)
                if set(row)!=columns:raise ValidationError('云端表结构与当前版本不一致，未恢复数据')
                for key in ('owner_user_id','claimed_by_user_id'):
                    if key in row:
                        if row[key]!=source:raise ValidationError('备份包含其他用户的数据，拒绝恢复')
                        row[key]=owner
                if table in {'event_log','account_window_events'}:row.pop('seq')
                if table=='native_browser_profiles':
                    import uuid
                    if not row['id'].startswith('native:'):raise ValidationError('窗口标识不正确')
                    uuid.UUID(row['id'][7:]);next_serial+=1;row['serial']=next_serial
                    from .native_browser import validate_proxy
                    row['proxy_server']=validate_proxy(row['proxy_server'])
                # Nothing restored from another computer is permitted to auto-execute.
                if table=='studio_jobs' and row['status'] not in {'completed','cancelled','failed','needs_review'}:
                    row['status']='needs_review' if row['inflight'] else 'paused'
                    row['message']='从云端恢复，请核对窗口登录后手动继续'
                if table in {'tasks','action_campaigns'} and row['status'] in {'RUNNING','PAUSING','QUEUED','RECOVERING','running','queued','pausing','waiting_network','recoverable'}:row['status']='paused'
                if table=='task_targets' and row['status'] in {'running','waiting_network'}:
                    row['status']='recoverable';row['current_window_id']=None;row['last_error']='从云端恢复，请手动继续'
                if table=='action_targets' and row['status']=='running':row['status']='unknown'
                if table=='action_attempts' and row['status'] in {'RUNNING','running','started'}:row['status']='unknown'
                if table=='follow_monitor_runs' and row['status'] in {'running','paused','cancelling'}:row['status']='cancelled'
                keys=list(row)
                try:c.execute(f'INSERT {"OR IGNORE " if table=="retired_account_profiles" else ""}INTO "{table}" ({",".join(chr(34)+k+chr(34) for k in keys)}) VALUES({",".join("?" for _ in keys)})',[row[k] for k in keys])
                except Exception as e:raise ConflictError('本机存在相同记录或备份关联不完整，恢复已回滚，原数据未改变') from e
        # Repair legacy source/action identities and provenance after restoring
        # too: the destination may already have the latest migration marker.
        from .identity_registry import repair_global_registry
        repair_global_registry(c)
        from .split_admissions import backfill_split_admissions, backfill_split_completions
        if not tables['split_completed_targets']:
            # Legacy restore generated provisional rows through INSERT triggers.
            # The destination was empty; only these provisional rows belong here.
            # Rebuild using immutable history where it is stronger than updated_at.
            c.execute('DELETE FROM split_completed_targets WHERE owner_user_id=?', (owner,))
        backfill_split_completions(c, owner)
        backfill_split_admissions(c, owner)
        from .private_follow_reviews import capture_private_follow_completions
        capture_private_follow_completions(c, owner)
        # A destination can already have the startup migration marker. Reapply
        # the same narrowly classified removal to imported data before commit.
        from .database import purge_legacy_facebook_data
        purge_legacy_facebook_data(c, live_lease_tokens=getattr(db, 'live_browser_lease_tokens', ()))
        # Imported table order and deferred foreign keys can temporarily omit
        # provenance parents. Reconcile exact projections before exposing restore.
        from .workbench_aggregates import rebuild_workbench_aggregates
        from .workbench_progress_aggregates import rebuild_workbench_progress_aggregates
        rebuild_workbench_aggregates(c)
        rebuild_workbench_progress_aggregates(c)
        if c.execute('PRAGMA foreign_key_check').fetchone():raise ValidationError('备份关联校验失败，恢复已回滚')
        check_active()



class CloudWorkspace:
    def __init__(self,service,data_dir,api=None,*,enabled=False,project_url='',publishable_key=''):
        self.service=service;self.db=service.database;self.data_dir=data_dir
        self.api=api if api is not None else SupabaseAPI(enabled=enabled,project_url=project_url,publishable_key=publishable_key)
        self.lock=threading.RLock();self.sessions={};self.states={};self.auth_generations={};self.stop_event=threading.Event();self.wake=threading.Event();self.thread=None;self.sync_gate=threading.Lock()
    @property
    def configured(self):
        # Explicitly injected adapters support offline tests. Production always
        # uses SupabaseAPI, whose default is disabled and contains no endpoint.
        return bool(getattr(self.api,'configured',True))
    @property
    def project_url(self):
        return getattr(self.api,'project_url','')
    def configure(self,*,enabled,project_url,publishable_key):
        replacement=SupabaseAPI(enabled=enabled,project_url=project_url,publishable_key=publishable_key)
        # Never change destinations during an admitted authentication/backup
        # request or let an old session become credentials for a new project.
        if not self.sync_gate.acquire(blocking=False):raise ConflictError('云端请求正在完成，请稍后重试配置')
        try:
            with self.lock:
                if self.stop_event.is_set():raise ConflictError('应用正在退出')
                self.sessions.clear();self.states.clear();self.api=replacement
            self.start()
            return {'configured':self.configured,'enabled':self.api.enabled,'project_url':self.project_url,'activated':True}
        finally:self.sync_gate.release()
    def start(self):
        if not self.configured or self.stop_event.is_set() or (self.thread and self.thread.is_alive()):return
        def run():
            while not self.stop_event.is_set():
                self.wake.wait(30);self.wake.clear()
                if self.stop_event.is_set():break
                with self.lock:owners=list(self.sessions)
                for owner in owners:
                    try:self.sync(owner)
                    except Exception as e:
                        with self.lock:self.states[owner]=str(e)
        self.thread=threading.Thread(target=run,daemon=True,name='workspace-cloud-backup');self.thread.start()
    def shutdown(self):
        self.stop_event.set();self.wake.set()
        if self.thread:self.thread.join(timeout=2)
        with self.lock:self.sessions.clear()
    def _token(self,owner):
        with self.lock:session=self.sessions.get(owner)
        if not session:raise ValidationError('请先登录云端邮箱账号')
        if session['expires_at']<time.time()+60:
            updated=self.api.request('/auth/v1/token?grant_type=refresh_token',method='POST',body={'refresh_token':session['refresh_token']})
            with self.lock:
                if self.stop_event.is_set() or self.sessions.get(owner) is not session:raise InterruptedError('云端连接已结束')
                if updated.get('user',{}).get('id')!=session['user']['id']:raise ValidationError('云端登录身份发生变化')
                session.update(updated);session['expires_at']=time.time()+updated.get('expires_in',3600)
        return session['access_token']
    def status(self,owner):
        with self.db.read() as c:row=c.execute('SELECT email,revision,last_sync_at FROM cloud_workspace_links WHERE owner_user_id=? AND project_url=?',(owner,self.project_url)).fetchone()
        with self.lock:
            signed_in=self.configured and owner in self.sessions
            message=self.states.get(owner,'请登录云端邮箱账号，开启自动备份') if self.configured else '云端未启用；配置并启用自己的 Supabase 项目后可登录备份，本机数据继续保存在本机'
            return {'project_url':self.project_url,'enabled':bool(getattr(self.api,'enabled',self.configured)),'configured':self.configured,'signed_in':signed_in,'email':row['email'] if row else '', 'revision':row['revision'] if row else 0,'last_sync_at':row['last_sync_at'] if row else None,'message':message,'auto_sync':signed_in}
    def command(self,owner,body):
        if body.get('action')=='logout':
            with self.lock:
                self.auth_generations[owner]=self.auth_generations.get(owner,0)+1
                self.sessions.pop(owner,None);self.states[owner]='已断开云端，本机数据保留'
            return self.status(owner)
        if not self.configured:raise ValidationError('云端未启用，请先配置并启用自己的 Supabase 项目')
        if not self.sync_gate.acquire(blocking=False):raise ConflictError('云端请求正在完成，请稍后重试')
        try:
            if not self.configured:raise ValidationError('云端未启用，请先配置并启用自己的 Supabase 项目')
            return self._command(owner,body)
        finally:self.sync_gate.release()
    def _command(self,owner,body):
        action=body.get('action')
        if action=='probe':
            self.api.request('/auth/v1/settings')
            try:self.api.request('/rest/v1/juxin_workspaces?select=revision&limit=0')
            except ValidationError:
                with self.lock:self.states[owner]='云端可连接，请登录后验证数据表权限'
            else:
                with self.lock:self.states[owner]='云端可连接，请登录后同步'
            return self.status(owner)
        if action=='signup':
            email=str(body.get('email','')).strip();password=str(body.get('password',''))
            if '@' not in email or len(email)>254 or len(password)<8:raise ValidationError('请输入有效邮箱和至少 8 位密码')
            self.api.request('/auth/v1/signup',method='POST',body={'email':email,'password':password})
            with self.lock:self.states[owner]='注册请求已提交，请查看邮箱完成验证，再登录'
            return self.status(owner)
        if action=='login':
            with self.lock:
                if self.stop_event.is_set():raise InterruptedError('应用正在退出')
                generation=self.auth_generations.get(owner,0)
            session=self.api.request('/auth/v1/token?grant_type=password',method='POST',body={'email':str(body.get('email','')).strip(),'password':str(body.get('password',''))})
            def check_login_active():
                if self.stop_event.is_set():raise InterruptedError('应用正在退出')
                if self.auth_generations.get(owner,0)!=generation:raise InterruptedError('云端登录已取消')
            with self.lock:check_login_active()
            user=session.get('user',{});ident=user.get('id')
            if not ident or not session.get('access_token'):raise ValidationError('云端登录未完成')
            # Recovery checks session liveness while holding the DB write lock.
            # Never hold self.lock while waiting for that lock: login concurrent
            # with restore would deadlock every subsequent database writer.
            # Acquire the cloud lock only after the DB writer is acquired, then
            # retain it through commit and publication. Logout can cancel a
            # request waiting for HTTP/the writer; it cannot acknowledge and
            # then be undone by a late mapping commit or credential publish.
            locked=False
            try:
                with self.db.write() as c:
                    self.lock.acquire();locked=True
                    check_login_active()
                    old=c.execute('SELECT cloud_user_id FROM cloud_workspace_links WHERE owner_user_id=? AND project_url=?',(owner,self.project_url)).fetchone()
                    if old and old['cloud_user_id']!=ident:raise ConflictError('此本机账号已关联其他云端账号，请使用对应邮箱登录')
                    other=c.execute('SELECT owner_user_id FROM cloud_workspace_links WHERE cloud_user_id=?',(ident,)).fetchone()
                    if other and other['owner_user_id']!=owner:raise ConflictError('此云端账号已关联本机另一个账号，请切换到该本机账号')
                    if not old:
                        c.execute('''INSERT INTO cloud_workspace_links(owner_user_id,cloud_user_id,email,project_url) VALUES(?,?,?,?)
                            ON CONFLICT(owner_user_id) DO UPDATE SET cloud_user_id=excluded.cloud_user_id,email=excluded.email,
                            project_url=excluded.project_url,revision=0,digest='',last_sync_at=NULL''',(owner,ident,user.get('email',''),self.project_url))
                check_login_active()
                session['expires_at']=time.time()+session.get('expires_in',3600);self.sessions[owner]=session
                self.states[owner]='已登录，正在检查云端工作区';self.wake.set()
            finally:
                if locked:self.lock.release()
            return self.status(owner)
        if action=='sync':
            if owner not in self.sessions:raise ValidationError('请先登录云端邮箱账号')
            self.states[owner]='已安排同步';self.wake.set();return self.status(owner)
        raise ValidationError('无效云端操作')
    def sync(self,owner):
        if not self.configured:return
        # A separate operation gate never blocks logout or application shutdown.
        if not self.sync_gate.acquire(blocking=False):return
        try:
            if self.stop_event.is_set():return
            token=self._token(owner)
            with self.lock:session=self.sessions.get(owner)
            def alive():
                with self.lock:return not self.stop_event.is_set() and self.sessions.get(owner) is session
            def check_active():
                if not alive():raise InterruptedError('云端连接已结束')
            def request(*args,**kwargs):
                if not alive():raise InterruptedError('云端连接已结束')
                value=self.api.request(*args,**kwargs)
                if not alive():raise InterruptedError('云端连接已结束')
                return value
            if not alive():return
            with self.db.read() as c:
                link=dict(c.execute('SELECT * FROM cloud_workspace_links WHERE owner_user_id=?',(owner,)).fetchone())
                empty=workspace_is_empty(c,owner)
            remote=request('/rest/v1/juxin_workspaces?select=revision&owner_id=eq.'+session['user']['id'],token=token)
            if len(remote)>1:raise ValidationError('云端返回了异常工作区')
            if remote and empty and link['revision']==0:
                remote=request('/rest/v1/juxin_workspaces?select=revision,payload&owner_id=eq.'+session['user']['id'],token=token)
                if len(remote)!=1:raise ConflictError('云端工作区已变化，请重试')
                import_workspace(self.db,owner,self.data_dir,remote[0]['payload'],check_active=check_active)
                # Re-encode after local owner/path mapping before recording the baseline digest.
                _,digest=export_workspace(self.db,owner,self.data_dir)
                if not alive():return
                with self.db.write() as c:c.execute('UPDATE cloud_workspace_links SET revision=?,digest=?,last_sync_at=? WHERE owner_user_id=?',(remote[0]['revision'],digest,isoformat(),owner))
                self.states[owner]='已从云端恢复；浏览器需在本机重新登录，任务等待手动继续';return
            version=remote[0]['revision'] if remote else 0
            payload,digest=export_workspace(self.db,owner,self.data_dir)
            if version!=link['revision']:
                # A successful upload can lose its HTTP acknowledgement. Reconcile
                # only an exact content match; never overwrite another version.
                actual=request('/rest/v1/juxin_workspaces?select=revision,payload&owner_id=eq.'+session['user']['id'],token=token)
                if len(actual)==1 and actual[0]['payload']==payload:
                    if not alive():return
                    with self.db.write() as c:c.execute('UPDATE cloud_workspace_links SET revision=?,digest=?,last_sync_at=? WHERE owner_user_id=?',(actual[0]['revision'],digest,isoformat(),owner))
                    self.states[owner]='云端备份已确认';return
                raise ConflictError('云端已有其他电脑的新版本。已停止上传以保留两边的数据；请在新电脑的空工作区恢复')
            if digest==link['digest'] and remote:self.states[owner]='云端备份已是最新';return
            revision=request('/rest/v1/rpc/juxin_save_workspace',method='POST',token=token,body={'expected_revision':version,'new_payload':payload})
            if not isinstance(revision,int) or revision!=version+1:raise ValidationError('云端未确认保存版本')
            if not alive():return
            with self.db.write() as c:c.execute('UPDATE cloud_workspace_links SET revision=?,digest=?,last_sync_at=? WHERE owner_user_id=?',(revision,digest,isoformat(),owner))
            self.states[owner]='云端备份成功'
        finally:
            self.sync_gate.release()
