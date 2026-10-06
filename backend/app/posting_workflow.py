"""Persistent stock-photo queue; shared leases and irreversible-submit fence."""
from __future__ import annotations
import asyncio,base64,json,re,sqlite3,uuid
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from .errors import ValidationError,ConflictError,NotFoundError
from .service import isoformat
from .posting_schema import initialize_posting_schema
from .posting_pexels import PexelsProvider
from .posting_executor import PostingExecutor
from .studio_worker import PostingRejected
from .async_cleanup import finish_owned
from .browser_admission import assert_no_durable_window_hold
WAITING={'preparing','ready','queued'}
def posting_failure(exc,phase):
    """Stable, actionable diagnostics only: never return upstream text, URLs or secrets."""
    code=getattr(exc,'code','');message=str(exc);reason=getattr(exc,'details',{}).get('reason','')
    if isinstance(exc,asyncio.CancelledError):return 'interrupted','运行已停止；尚未提交，可在窗口清理后重新检查并重试'
    if phase=='preparation':
        if '安全配置' in message or '未配置' in message:return 'provider_unconfigured','Pexels 未配置；请由本人在安全配置中填写凭据，然后重试准备素材'
        if '限流' in message or '配额' in message:return 'provider_rate_limited','素材服务配额或访问受限；请查看连接验证结果后重试，不能据此判断密钥无效'
        if '重复' in message:return 'asset_duplicate','素材已命中永久去重记录，请移除此任务并创建使用其他素材的新任务'
        if '没有足够' in message:return 'asset_exhausted','未找到足够未用素材；可更换主题，或稍后重试准备素材'
        if '磁盘' in message:return 'disk_space','磁盘空间不足；释放空间后重试，程序不会删除原始素材'
        return 'material_prepare_failed','素材准备失败；请检查网络、素材来源和连接验证结果后重试，尚未发布'
    if code=='instagram_login_required' or any(s in message for s in ('需要登录','完成登录','登录或验证','login is required')):
        return 'instagram_login_required','目标窗口需要登录或验证；请在该窗口处理后重新检查并重试，尚未发布'
    if reason in {'posting_account_mismatch','posting_identity_unverified'}:
        return reason,'未能核实目标账号身份；请先检查该窗口的真实账号，再重新检查并重试，尚未发布'
    if any(s in message for s in ('身份','账号与任务目标','主页')) and phase=='identity':
        return 'identity_unverified','未能核实目标账号身份；请先检查该窗口的真实账号，再重新检查并重试，尚未发布'
    if reason=='posting_asset_changed' or isinstance(exc,(FileNotFoundError,PermissionError)) or any(s in message for s in ('素材文件','素材路径','素材尚未')):
        return 'material_unavailable','本地素材缺失、不可读或与审阅版本不一致；请检查素材文件，尚未发布'
    startup_reasons={
        'playwright_start_failed':'本地自动化驱动启动失败；请检查完整安装与驱动组件，尚未发布',
        'cdp_attach_failed':'任务窗口连接未建立；请检查窗口服务与本地连接后重试，尚未发布',
        'cdp_profile_verification_failed':'任务窗口连接身份未能核实；请关闭旧任务连接后重试，尚未发布',
        'browser_page_create_timeout':'任务窗口未能及时创建网页；请检查浏览器响应后重试，尚未发布',
    }
    if phase=='browser_open' and reason in startup_reasons:return reason,startup_reasons[reason]
    if phase=='browser_open':return 'browser_open_failed','任务浏览器未能打开或连接；请检查浏览器安装与窗口连接后重试，尚未发布'
    if isinstance(exc,ConflictError):return 'window_ownership_changed','窗口占用状态已变化；先确认原任务已结束且窗口可用，再重试'
    return 'composer_failed','发帖页面准备未完成；请检查目标窗口登录状态与页面加载后重试，尚未提交'

class PostingManager:
    def __init__(self,service,bitbrowser,key_supplier=None,executor_factory=None,provider=None):
        self.service=service;self.db=service.database;self.bitbrowser=bitbrowser
        self.provider=provider or PexelsProvider(self.db,key_supplier)
        self.executor_factory=executor_factory or PostingExecutor
        self.tasks={};self.executors={};self.retry_after={};self.scheduler=None;self.scheduler_error='';self.stopping=False;self.control=asyncio.Lock()
    def recover(self):
        with self.db.write() as c:
            initialize_posting_schema(c)
            c.execute("UPDATE posting_jobs SET status='needs_review',message='提交期间中断，需核验；不会自动重发' WHERE status='submitting'")
            c.execute("UPDATE posting_jobs SET status='failed',failure_stage='pre_submit',failure_code='interrupted',message='运行中断，尚未提交；先完成窗口清理，再重新检查并重试' WHERE status='running' AND attempt_id='' AND submitted_at IS NULL")
            c.execute("UPDATE posting_jobs SET status='needs_review',failure_stage='unknown',failure_code='interrupted_submission',message='提交记录存在，保留资源等待核验；禁止重发' WHERE status='running'")
            # Recover the acquire/persist crash seam only for the exact posting entity.
            for lease in c.execute("SELECT * FROM browser_operation_leases WHERE operation_type='posting'").fetchall():
                c.execute("UPDATE posting_jobs SET lease_token=?,status='needs_review',message='窗口申请期间中断，保留占用等待核验' WHERE id=? AND owner_user_id=? AND profile_id=? AND lease_token='' AND hidden_at IS NULL",(lease['lease_token'],lease['entity_id'],lease['owner_user_id'],lease['profile_id']))
            for job in c.execute("SELECT * FROM posting_jobs WHERE lease_token<>''").fetchall():
                lease=c.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?',(job['profile_id'],)).fetchone()
                if lease is None:
                    # Never overwrite a different generation or reuse a token on another profile.
                    columns={r[1] for r in c.execute('PRAGMA table_info(browser_operation_leases)')}
                    values={'profile_id':job['profile_id'],'owner_user_id':job['owner_user_id'],'operation_type':'posting','entity_id':job['id'],'lease_token':job['lease_token'],'acquired_at':isoformat(),'heartbeat_at':isoformat(),'expires_at':isoformat(datetime.now(timezone.utc)+timedelta(seconds=600))}
                    values={k:v for k,v in values.items() if k in columns}
                    if not c.execute('SELECT 1 FROM browser_operation_leases WHERE lease_token=?',(job['lease_token'],)).fetchone():
                        c.execute('INSERT INTO browser_operation_leases('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
                        lease=c.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?',(job['profile_id'],)).fetchone()
                    c.execute("UPDATE posting_jobs SET status='needs_review',message='恢复窗口占用，停止并等待核对' WHERE id=?",(job['id'],))
                if self._lease_matches(lease,job):self.db.live_browser_lease_tokens.add(job['lease_token'])
                else:c.execute("UPDATE posting_jobs SET status='needs_review',message='窗口占用记录不一致，停止并等待核对' WHERE id=?",(job['id'],))
    @staticmethod
    def _lease_matches(lease,job):
        return bool(lease and lease['profile_id']==job['profile_id'] and lease['lease_token']==job['lease_token'] and lease['owner_user_id']==job['owner_user_id'] and lease['entity_id']==job['id'] and lease['operation_type']=='posting')
    def assert_lease(self,job,connection=None):
        if connection is None:
            with self.db.read() as c:return self.assert_lease(job,c)
        lease=connection.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?',(job['profile_id'],)).fetchone()
        if not self._lease_matches(lease,job):raise ConflictError('发帖窗口占用身份不匹配，保留资源等待核对')
    def active_ids(self):
        with self.db.read() as c:
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='posting_jobs'").fetchone()
            held={r[0] for r in c.execute("SELECT id FROM posting_jobs WHERE lease_token<>''")} if exists else set()
        return held|{i for i,t in tuple(self.tasks.items()) if not t.done()}
    def get(self,owner,ident):
        with self.db.read() as c:r=c.execute('SELECT * FROM posting_jobs WHERE id=? AND owner_user_id=?',(ident,owner)).fetchone()
        if not r:raise NotFoundError('发帖任务不存在')
        return dict(r)
    def _asset(self,ident):
        with self.db.read() as c:r=c.execute('SELECT * FROM posting_assets WHERE id=?',(ident,)).fetchone()
        if not r:raise ValidationError('素材尚未准备')
        return dict(r)
    def _retry_state(self,job,connection):
        if job['status']!='failed' or job.get('hidden_at'):return None,'仅明确失败且未移除的任务可以重试'
        if job['lease_token']:return None,'请先重试关闭任务窗口；关闭确认前不能重发或换窗口'
        if connection.execute('SELECT 1 FROM posting_receipts WHERE job_id=?',(job['id'],)).fetchone():return None,'已确认发布成功，只能完成清理，禁止重发'
        if (job['attempt_id'] or job['submitted_at']) and job.get('failure_stage')!='rejected':return None,'存在提交记录但未确认失败，须先核验发布结果，禁止重发'
        if job['profile_id'] and connection.execute('SELECT 1 FROM browser_operation_leases WHERE profile_id=?',(job['profile_id'],)).fetchone():return None,'目标窗口正在被其他任务占用，请等待释放'
        asset=connection.execute('SELECT state FROM posting_assets WHERE id=? AND job_id=?',(job['asset_id'],job['id'])).fetchone() if job['asset_id'] else None
        if job['asset_id'] and not asset:return None,'素材登记缺失；请保留任务并检查数据，不能重发'
        if asset and asset['state'] not in {'ready','reserved'}:return None,'素材已使用、重复或已归档；请使用新素材创建任务'
        return ('review' if asset and asset['state']=='ready' else 'prepare'),' '
    def _can_withdraw(self,job,connection):
        # Withdrawing changes only an unclaimed queue entry. It must never
        # release another operation's window, erase a submit fence, or race a
        # worker already admitted by this manager (including acquire/persist).
        task=self.tasks.get(job['id'])
        if (job['status']!='queued' or job['hidden_at'] or job['lease_token']
                or job['attempt_id'] or job['submitted_at'] or job['id'] in self.executors
                or (task and not task.done())):
            return False
        if connection.execute('SELECT 1 FROM posting_receipts WHERE job_id=?',(job['id'],)).fetchone():return False
        return not connection.execute("SELECT 1 FROM browser_operation_leases WHERE operation_type='posting' AND entity_id=?",(job['id'],)).fetchone()
    async def command(self,owner,body):
        action=body.get('action')
        async with self.control:
            if action=='generate':return self.generate(owner,body)
            if action=='validate_provider':return await asyncio.to_thread(self.provider.validate)
            if action=='start':
                ids=body.get('job_ids',[])
                if not isinstance(ids,list) or not 1<=len(ids)<=20 or any(not isinstance(i,str) or not i for i in ids) or len(set(ids))!=len(ids):raise ValidationError('请选择1–20条不同任务')
                reviewed=body.get('reviewed')
                if not isinstance(reviewed,list) or len(reviewed)!=len(ids) or any(not isinstance(r,dict) or not isinstance(r.get("id"),str) or not r["id"] for r in reviewed):raise ValidationError('缺少已审阅内容，请重新检查账号、素材和文案')
                approved={r.get('id'):r for r in reviewed}
                if set(approved)!=set(ids):raise ValidationError('审阅记录与所选任务不一致')
                with self.db.write() as c:
                    jobs=[]
                    for ident in ids:
                        r=c.execute('SELECT * FROM posting_jobs WHERE id=? AND owner_user_id=?',(ident,owner)).fetchone()
                        if r and any(approved[ident].get(k)!=r[k] for k in ('caption','asset_id','profile_id','expected_username')):raise ConflictError('任务内容在审阅后发生变化，请重新检查')
                        # An identical account/content tuple after withdrawal
                        # must not revive an old confirmation or replayed start.
                        revision=approved[ident].get('queue_revision',0)
                        if r and (type(revision) is not int or revision!=r['queue_revision']):raise ConflictError('任务排队记录在审阅后发生变化，请重新检查并确认')
                        if not r or r['status']!='ready' or not r['profile_id'] or not r['expected_username'] or r['attempt_id'] or r['submitted_at'] or r['lease_token'] or c.execute('SELECT 1 FROM posting_receipts WHERE job_id=?',(ident,)).fetchone():raise ConflictError('任务状态已变化，或尚未选择已核验账号')
                        assert_no_durable_window_hold(c,owner,r['profile_id'])
                        if c.execute('SELECT 1 FROM browser_operation_leases WHERE profile_id=?',(r['profile_id'],)).fetchone():raise ConflictError('窗口正在被其他任务占用')
                        a=c.execute("SELECT 1 FROM posting_assets WHERE id=? AND state='ready'",(r['asset_id'],)).fetchone()
                        if not a:raise ValidationError('素材尚未准备就绪')
                        jobs.append(ident)
                    for ident in jobs:c.execute("UPDATE posting_jobs SET status='queued',queue_revision=queue_revision+1,updated_at=? WHERE id=?",(isoformat(),ident))
                return {'queued':jobs}
            ident=str(body.get('job_id',''));job=self.get(owner,ident)
            if action=='withdraw':
                with self.db.write() as c:
                    job=dict(c.execute('SELECT * FROM posting_jobs WHERE id=? AND owner_user_id=?',(ident,owner)).fetchone())
                    if not self._can_withdraw(job,c):raise ConflictError('仅可撤回尚未领取窗口、未执行且未提交的排队任务；请刷新后检查当前状态')
                    now=isoformat()
                    c.execute('INSERT INTO posting_withdraw_history VALUES(?,?,?,?,?,?,?)',
                        (str(uuid.uuid4()),ident,owner,job['profile_id'],job['expected_username'],job['expected_actor_id'],now))
                    # An assigned ready draft also fences orphan cleanup. Clear
                    # only this job's assignment so the original hold can be
                    # verified normally; preserve material, text and all audit.
                    c.execute("UPDATE posting_jobs SET status='ready',profile_id='',expected_username='',expected_actor_id='',queue_revision=queue_revision+1,message='已撤回排队，素材和文案保留；请重新选择窗口、检查并确认启动',updated_at=? WHERE id=? AND owner_user_id=?",(now,ident,owner))
                self.retry_after.pop(ident,None)
                return {'withdrawn':ident,'status':'ready','requires_assignment':True,'requires_review':True,'will_repost':False}
            if action=='retry':
                if self.stopping:raise ConflictError('程序正在停止，请重新启动后重试')
                if ident in self.tasks and not self.tasks[ident].done():raise ConflictError('任务仍在处理，请等待当前操作结束')
                with self.db.write() as c:
                    job=dict(c.execute('SELECT * FROM posting_jobs WHERE id=? AND owner_user_id=?',(ident,owner)).fetchone())
                    retry_action,reason=self._retry_state(job,c)
                    if not retry_action:raise ConflictError(reason)
                    if retry_action=='prepare' and not job['asset_id'] and not self.provider.configured():raise ValidationError('请先由本人在安全配置中填写 Pexels 凭据，再重试准备素材')
                    c.execute('INSERT INTO posting_retry_history VALUES(?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),ident,owner,job['attempt_id'],job['submitted_at'],job['failure_stage'],job['failure_code'],isoformat()))
                    status='ready' if retry_action=='review' else 'preparing'
                    message='已恢复待启动；保留原素材和文案，请重新检查并确认发布' if status=='ready' else '已重新加入素材准备队列；保留原素材预留，不会自动发布'
                    c.execute("UPDATE posting_jobs SET status=?,attempt_id='',submitted_at=NULL,failure_stage='',failure_code='',message=?,updated_at=? WHERE id=?",(status,message,isoformat(),ident))
                self.retry_after.pop(ident,None)
                return {'retried':ident,'status':status,'requires_review':True,'will_repost':False}
            if action=='close_unknown':
                if job['status']!='needs_review' or body.get('confirm_no_repost') is not True:raise ConflictError('请先确认结果未知且仅关闭窗口，不会重发')
                if not job['lease_token']:raise ConflictError('占用记录异常，不能关闭未经授权的窗口')
                if not self._spawn(ident,self._cleanup(job,self.executors.get(ident),resolve_unknown=True)):raise ConflictError('该任务仍在处理，请等待当前操作结束')
                return {'accepted':True,'will_repost':False}
            if action=='retry_cleanup':
                if job['status'] not in {'cleanup_pending','failed','cancelled'} or not job['lease_token']:raise ConflictError('该任务不能重试清理')
                if not self._spawn(ident,self._cleanup(job,self.executors.get(ident))):raise ConflictError('清理正在处理，请勿重复操作')
                return {'accepted':True}
            if (job['attempt_id'] and action in {'assign','edit'}) or job['lease_token'] or job['status'] not in {'preparing','ready','failed'}:raise ConflictError('执行中或结果待核验任务不可修改')
            if action=='assign':
                profile=str(body.get('profile_id',''));username=str(body.get('expected_username','')).lstrip('@').casefold()
                if not re.fullmatch(r'[a-z0-9._]{1,30}',username):raise ValidationError('请先检查该窗口的真实 Instagram 账号')
                with self.db.write() as c:
                    account=c.execute('SELECT username FROM posting_account_snapshots WHERE owner_user_id=? AND profile_id=?',(owner,profile)).fetchone()
                    if not account or str(account[0]).casefold()!=username:raise ValidationError('目标账号与最近核验记录不符，请先检查账号')
                    assert_no_durable_window_hold(c,owner,profile)
                    if c.execute('SELECT 1 FROM browser_operation_leases WHERE profile_id=?',(profile,)).fetchone():raise ConflictError('窗口被占用，不能分配')
                    c.execute("UPDATE posting_jobs SET profile_id=?,expected_username=?,expected_actor_id='',updated_at=? WHERE id=?",(profile,username,isoformat(),ident))
                return {'assigned':ident}
            if action=='edit':
                caption=body.get('caption')
                if not isinstance(caption,str) or not caption.strip() or len(caption)>2200:raise ValidationError('请输入1–2200字文案')
                with self.db.write() as c:c.execute('UPDATE posting_jobs SET caption=?,updated_at=? WHERE id=?',(caption,isoformat(),ident))
                return {'updated':ident}
            if action=='cancel':
                if ident in self.tasks and not self.tasks[ident].done():raise ConflictError('素材正在准备，请等待完成后再删除')
                with self.db.write() as c:c.execute("UPDATE posting_jobs SET status='cancelled',hidden_at=?,updated_at=? WHERE id=?",(isoformat(),isoformat(),ident))
                if job['asset_id']:
                    await asyncio.to_thread(self.provider.cleanup,self._asset(job['asset_id']))
                return {'cancelled':ident,'history_retained':True}
            raise ValidationError('不支持的发帖操作')
    def generate(self,owner,body):
        key=body.get('request_id','');theme=body.get('theme','');caption=body.get('caption','');count=body.get('count',1)
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}',key):raise ValidationError('缺少生成请求标识')
        if not isinstance(theme,str) or not theme.strip() or len(theme)>300:raise ValidationError('主题长度须为1–300字')
        if not isinstance(caption,str) or not caption.strip() or len(caption)>2200:raise ValidationError('文案长度须为1–2200字')
        if type(count)!=int or not 1<=count<=20:raise ValidationError('每批生成1–20条')
        provider_name=body.get('provider','pexels')
        if provider_name!='pexels':raise ValidationError('不支持的素材服务')
        provider=self.provider
        if not provider.configured():raise ValidationError('素材服务未配置；不会调用素材服务或创建假任务')
        ids=[]
        with self.db.write() as c:
            existing=c.execute('SELECT id,theme,caption,provider_name FROM posting_jobs WHERE owner_user_id=? AND request_key>=? AND request_key<? ORDER BY request_key',(owner,key+':',key+';')).fetchall()
            if existing:
                if len(existing)!=count or any(r['theme']!=theme.strip() or r['caption']!=caption or r['provider_name']!=provider_name for r in existing):raise ConflictError('同一请求标识不能更改任务内容')
                return {'job_ids':[r['id'] for r in existing]}
            waiting=c.execute("SELECT COUNT(*) FROM posting_jobs WHERE owner_user_id=? AND status IN ('preparing','ready','queued')",(owner,)).fetchone()[0]
            if waiting+count>200:raise ValidationError('等待任务已达200条上限，请先处理现有任务')
            for i in range(count):
                ident=str(uuid.uuid4());ids.append(ident)
                c.execute('INSERT INTO posting_jobs(id,owner_user_id,request_key,provider_name,theme,caption,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(ident,owner,key+':'+str(i).zfill(3),provider_name,theme.strip(),caption,isoformat(),isoformat()))
        return {'job_ids':ids}
    def start_scheduler(self):
        self.stopping=False
        if not self.scheduler or self.scheduler.done():self.scheduler=asyncio.create_task(self._schedule())
    def _spawn(self,ident,coro):
        if ident in self.tasks and not self.tasks[ident].done():coro.close();return False
        self.tasks[ident]=asyncio.create_task(coro);return True
    def _runnable_jobs(self):
        # Scan bounded pages, applying in-memory backoff before selecting work.
        rows=[];cursor=None;now=asyncio.get_running_loop().time()
        with self.db.read() as c:
            while len(rows)<2:
                clause='';params=[]
                if cursor:clause=' AND (created_at,id)>(?,?)';params=list(cursor)
                page=c.execute("SELECT * FROM posting_jobs WHERE (status IN ('preparing','cleanup_pending') OR (status='queued' AND NOT EXISTS(SELECT 1 FROM browser_operation_leases l WHERE l.profile_id=posting_jobs.profile_id)))"+clause+" ORDER BY created_at,id LIMIT 100",params).fetchall()
                if not page:break
                for row in page:
                    if row['id'] not in self.tasks and self.retry_after.get(row['id'],0)<=now:rows.append(dict(row))
                    if len(rows)>=2:break
                cursor=(page[-1]['created_at'],page[-1]['id'])
        return rows
    async def _schedule(self):
        while not self.stopping:
            for task in self.tasks.values():
                if task.done() and not task.cancelled():task.exception()
            self.tasks={i:t for i,t in self.tasks.items() if not t.done()}
            try:
                if len(self.tasks)<2:
                    rows=self._runnable_jobs()
                    for job in rows:
                        if len(self.tasks)>=2:break
                        if job['id'] in self.tasks or self.retry_after.get(job['id'],0)>asyncio.get_running_loop().time():continue
                        self._spawn(job['id'],self._prepare(job) if job['status']=='preparing' else self._cleanup(job) if job['status']=='cleanup_pending' else self._execute(job))
                self.scheduler_error=''
            except Exception:self.scheduler_error='发帖调度暂不可用，已保留持久任务并等待重试'
            await asyncio.sleep(1)
    async def _prepare(self,job):
        from .studio import finish_preparation
        try:
            provider=self.provider
            ident=await finish_preparation(provider.prepare,job)
            with self.db.write() as c:c.execute("UPDATE posting_jobs SET asset_id=?,status='ready',message='素材已就绪',updated_at=? WHERE id=? AND status='preparing'",(ident,isoformat(),job['id']))
        except Exception as exc:
            code,message=posting_failure(exc,'preparation')
            with self.db.write() as c:c.execute("UPDATE posting_jobs SET status='failed',failure_stage='preparation',failure_code=?,message=?,updated_at=? WHERE id=? AND status='preparing'",(code,message,isoformat(),job['id']))
    async def _execute(self,job):
        ident=job['id'];owner=job['owner_user_id'];profile=job['profile_id'];token='';executor=None;admission_started=False
        try:
            # Commands and scheduler admission share one lock through the
            # acquire/persist seam. A withdrawn or superseded queue snapshot
            # returns before any surface or lease operation can occur.
            async with self.control:
                current=self.get(owner,ident)
                if (current['status']!='queued' or current['hidden_at'] or current['attempt_id']
                        or current['submitted_at'] or current['lease_token']
                        or any(current[k]!=job[k] for k in ('profile_id','expected_username','asset_id','caption','queue_revision'))):return
                with self.db.read() as c:
                    if c.execute('SELECT 1 FROM posting_receipts WHERE job_id=?',(ident,)).fetchone():return
                admission_started=True
                token=await self.service.acquire_browser_lease_async(owner,profile,operation_type='posting',entity_id=ident,ttl_seconds=600)
                with self.db.write() as c:
                    changed=c.execute("UPDATE posting_jobs SET status='running',lease_token=?,message='核验目标账号',updated_at=? WHERE id=? AND owner_user_id=? AND status='queued' AND attempt_id='' AND submitted_at IS NULL AND lease_token=''",(token,isoformat(),ident,owner)).rowcount
            if not changed:
                with self.db.write() as c:
                    self.assert_lease(dict(job,lease_token=token),c)
                    c.execute("DELETE FROM browser_operation_leases WHERE profile_id=? AND lease_token=? AND owner_user_id=? AND entity_id=? AND operation_type='posting'",(profile,token,owner,ident))
                self.db.live_browser_lease_tokens.discard(token)
                token=''
                return
            job=self.get(owner,ident);executor=self.executor_factory(self);self.executors[ident]=executor
            async def checkpoint():
                if self.stopping:raise asyncio.CancelledError()
                current=self.get(owner,ident)
                self.assert_lease(current)
                if current['lease_token']!=token:raise ConflictError('发帖占用凭证失效')
                self.service.renew_browser_lease(profile,token,ttl_seconds=600)
            async def before_submit():
                await checkpoint()
                with self.db.write() as c:
                    changed=c.execute("UPDATE posting_jobs SET status='submitting',attempt_id=?,submitted_at=?,updated_at=? WHERE id=? AND status='running' AND attempt_id=''",(str(uuid.uuid4()),isoformat(),isoformat(),ident)).rowcount
                    if changed!=1:raise ConflictError('任务已进入提交阶段，禁止再次分享')
            async def confirmed(result):
                if result.get('published')!=1 or result.get('verification')!='instagram_dialog':raise ValidationError('缺少原生分享成功证据')
                at=result.get('confirmed_at')
                try:
                    dt=datetime.fromisoformat(at.replace('Z','+00:00'))
                    if dt.tzinfo is None or dt.utcoffset() is None:raise ValueError('timezone required')
                    dt=dt.astimezone(timezone.utc)
                except (ValueError,AttributeError,TypeError):raise ValidationError('成功回执时间无效') from None
                with self.db.write() as c:
                    r=c.execute('SELECT status,attempt_id FROM posting_jobs WHERE id=?',(ident,)).fetchone()
                    if r['status'] not in {'submitting','cleanup_pending'} or not r['attempt_id']:raise ConflictError('成功回执不匹配提交状态')
                    c.execute('INSERT OR IGNORE INTO posting_receipts VALUES(?,?,?,?,?,?,?,?)',(ident,owner,profile,job['expected_username'],result.get('post_url',''),isoformat(dt),dt.date().isoformat(),json.dumps({'verification':'instagram_dialog','attempt_id':r['attempt_id']})))
                    c.execute("UPDATE posting_assets SET state='used' WHERE id=?",(job['asset_id'],))
                    c.execute("UPDATE posting_jobs SET status='cleanup_pending',message='成功已确认，正在清理并关闭窗口',updated_at=? WHERE id=?",(isoformat(),ident))
            await executor.publish(job,self._asset(job['asset_id']),checkpoint,before_submit,confirmed)
            if self.get(owner,ident)['status']!='cleanup_pending':raise ValidationError('发布器没有返回可信成功回执')
        except ConflictError as exc:
            if not token:
                with self.db.write() as c:c.execute("UPDATE posting_jobs SET message='等待窗口释放' WHERE id=? AND status='queued'",(ident,))
                return
            self._record_failure(owner,ident,False,exc,job.get('_posting_phase','pre_submit'))
        except BaseException as exc:
            # Cancelling a stale worker while it waits for admission must not
            # turn a withdrawn/newer draft into a failure or alter its audit.
            if admission_started:self._record_failure(owner,ident,isinstance(exc,PostingRejected),exc,job.get('_posting_phase','pre_submit'))
            if isinstance(exc,(KeyboardInterrupt,SystemExit)):raise
        finally:
            if token:
                current=self.get(owner,ident)
                if current['status'] in {'cleanup_pending','failed','cancelled'}:await finish_owned(self._cleanup(current,executor))
                elif executor:
                    try:await finish_owned(executor.disconnect())
                    except Exception:
                        with self.db.write() as c:c.execute("UPDATE posting_jobs SET message='结果待核验，执行器清理未完成；保留资源占用' WHERE id=?",(ident,))
    def _record_failure(self,owner,ident,rejected,exc=None,phase='pre_submit'):
        with self.db.write() as c:
            r=c.execute('SELECT * FROM posting_jobs WHERE id=? AND owner_user_id=?',(ident,owner)).fetchone()
            if not r or r['status']=='cleanup_pending':return
            uncertain=bool(r['attempt_id'] or r['submitted_at']) and not rejected
            stage='unknown' if uncertain else 'rejected' if rejected else 'pre_submit'
            code,message=('submission_unknown','提交结果待核验；保留素材和窗口占用，禁止重发') if uncertain else ('instagram_rejected','Instagram 明确返回分享失败；完成窗口清理后可重新检查并重试') if rejected else posting_failure(exc,phase)
            c.execute('UPDATE posting_jobs SET status=?,failure_stage=?,failure_code=?,message=?,updated_at=? WHERE id=?',('needs_review' if uncertain else 'failed',stage,code,message,isoformat(),ident))
    async def _cleanup(self,job,executor=None,resolve_unknown=False):
        token=job['lease_token'];profile=job['profile_id']
        if not token:return
        executor=executor or self.executors.get(job['id']) or self.executor_factory(self)
        self.executors[job['id']]=executor
        try:
            self.assert_lease(job)
            executor.lease_job=dict(job)
            if job['status']=='cleanup_pending':await asyncio.to_thread(self.provider.cleanup,self._asset(job['asset_id']))
            await finish_owned(executor.close(profile,token))
            # Atomically retire only this exact lease generation with its queue state.
            with self.db.write() as c:
                self.assert_lease(job,c)
                c.execute('DELETE FROM browser_operation_leases WHERE profile_id=? AND lease_token=?',(profile,token))
                if job['status']=='cleanup_pending':c.execute("UPDATE posting_jobs SET status='completed',lease_token='',hidden_at=?,message='成功，素材已移入可恢复区且窗口已关闭',updated_at=? WHERE id=? AND lease_token=?",(isoformat(),isoformat(),job['id'],token))
                elif resolve_unknown:c.execute("UPDATE posting_jobs SET status='unknown_closed',lease_token='',message='已按确认关闭；发布结果仍未知，未重发、不计成功或失败',updated_at=? WHERE id=? AND lease_token=?",(isoformat(),job['id'],token))
                else:c.execute("UPDATE posting_jobs SET lease_token='',updated_at=? WHERE id=? AND lease_token=?",(isoformat(),job['id'],token))
            self.db.live_browser_lease_tokens.discard(token)
            self.executors.pop(job['id'],None)
        except Exception:
            self.retry_after[job['id']]=asyncio.get_running_loop().time()+30
            with self.db.write() as c:
                original=c.execute('SELECT message FROM posting_jobs WHERE id=?',(job['id'],)).fetchone()[0]
                suffix='窗口关闭尚未确认，保留占用；请重试关闭，不会重新发布'
                message=original if suffix in original else original+'；'+suffix
                c.execute("UPDATE posting_jobs SET message=?,updated_at=? WHERE id=?",(message,isoformat(),job['id']))
    async def shutdown(self):
        self.stopping=True
        if self.scheduler:self.scheduler.cancel();await asyncio.gather(self.scheduler,return_exceptions=True)
        for task in self.tasks.values():task.cancel()
        await asyncio.gather(*self.tasks.values(),return_exceptions=True)
    def snapshot(self,owner,timezone_name='UTC',**kwargs):
        timezone_name=kwargs.get('timezone',timezone_name)
        try:tz=ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError,ValueError):raise ValidationError('时区无效') from None
        cursor=kwargs.get('cursor');limit=kwargs.get('limit',50)
        if type(limit)!=int or not 1<=limit<=200:raise ValidationError('分页大小须为1–200')
        after=None
        if cursor:
            try:
                after=json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if not isinstance(after,list) or len(after)!=2 or any(not isinstance(v,str) for v in after):raise ValueError()
            except Exception:raise ValidationError('分页标识无效') from None
        today=datetime.now(tz).date();start=datetime.combine(today,datetime.min.time(),tzinfo=tz).astimezone(timezone.utc);end=datetime.combine(today+timedelta(days=1),datetime.min.time(),tzinfo=tz).astimezone(timezone.utc)
        with self.db.read() as c:
            clause=' AND (j.created_at,j.id)<(?,?)' if after else ''
            params=[owner]+(after or [])+[limit+1]
            rows=[dict(r) for r in c.execute('SELECT j.*,a.preview,a.source_url,a.photographer,a.photographer_url,a.provider_id,a.source_license,a.license_url,a.credit,a.verified_at,a.state AS asset_state FROM posting_jobs j LEFT JOIN posting_assets a ON a.id=j.asset_id WHERE j.owner_user_id=? AND j.hidden_at IS NULL'+clause+' ORDER BY j.created_at DESC,j.id DESC LIMIT ?',params)]
            has_more=len(rows)>limit;rows=rows[:limit]
            next_cursor=base64.urlsafe_b64encode(json.dumps([rows[-1]['created_at'],rows[-1]['id']]).encode()).decode() if has_more else None
            states={r[0]:r[1] for r in c.execute('SELECT status,COUNT(*) FROM posting_jobs WHERE owner_user_id=? GROUP BY status',(owner,))}
            success=c.execute('SELECT COUNT(*) FROM posting_receipts WHERE owner_user_id=?',(owner,)).fetchone()[0]
            daily=c.execute('SELECT COUNT(*) FROM posting_receipts WHERE owner_user_id=? AND confirmed_at>=? AND confirmed_at<?',(owner,isoformat(start),isoformat(end))).fetchone()[0]
            accounts=[dict(r) for r in c.execute('SELECT profile_id,username,checked_at FROM posting_account_snapshots WHERE owner_user_id=?',(owner,))]
            for r in rows:
                retry_action,reason=self._retry_state(r,c)
                in_progress=r['id'] in self.tasks and not self.tasks[r['id']].done()
                r['retry_action']=None if in_progress else retry_action
                r['retry_blocked_reason']='任务仍在处理，请稍后再试' if in_progress else reason.strip() if not retry_action else ''
                r['can_modify']=r['status'] in {'preparing','ready','failed'} and not r['attempt_id'] and not r['submitted_at'] and not r['lease_token'] and not in_progress
                r['can_retry_cleanup']=r['status'] in {'cleanup_pending','failed','cancelled'} and bool(r['lease_token']) and not in_progress
                r['can_withdraw']=self._can_withdraw(r,c)
        for r in rows:
            r['window_held']=bool(r['lease_token'])
            for field in ('lease_token','attempt_id'):r.pop(field,None)
        return {'pagination':{'has_more':has_more,'next_cursor':next_cursor,'limit':limit},'scheduler_error':self.scheduler_error,'jobs':rows,'accounts':accounts,'configured':self.provider.configured(),'provider':'pexels','credentials':{'pexels_configured':self.provider.configured()},'totals':{'waiting':sum(states.get(s,0) for s in WAITING),'success':success,'failed':states.get('failed',0),'today_success':daily,'needs_review':states.get('needs_review',0)+states.get('unknown_closed',0)},'timezone':timezone_name,'capabilities':{'single_photo':True,'video':False,'live_verified':False,'dedupe':'global_provider_id_and_sha256','max_background_workers':2}}
