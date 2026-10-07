from __future__ import annotations
import asyncio
import json
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from pydantic import ValidationError as ModelError
from .errors import ConflictError, NotFoundError, ValidationError, UpstreamUnavailableError
from .service import isoformat
from .playwright_worker import PlaywrightWorker
from .browser_cleanup import disconnect_worker, close_profile_and_wait
from .studio_models import NurtureConfig
from .studio_worker import StudioBrowser, ResultUncertain, instagram_target
from .standalone_nurture import POLICY as NURTURE_POLICY

TERMINAL={'completed','failed','cancelled','needs_review'}

async def finish_preparation(function,*args):
    # Cancelling to_thread does not stop its file/DB writes. Keep the owning
    # task alive until preparation exits, so deletion/cleanup cannot race it.
    operation=asyncio.create_task(asyncio.to_thread(function,*args))
    cancelled=None
    while not operation.done():
        try:await asyncio.shield(operation)
        except asyncio.CancelledError as exc:cancelled=exc
        except Exception:break
    if cancelled:
        if not operation.cancelled():operation.exception()
        raise cancelled
    return operation.result()

def config_for(kind, data):
    if kind != 'nurture':
        raise ValidationError('仅支持养号任务')
    try:
        return NurtureConfig.model_validate(data).model_dump()
    except ModelError as exc:
        raise ValidationError('; '.join(e['msg'] for e in exc.errors())) from None

def nurture_targets(config):
    targets={'post':[], 'profile':[]}
    for value in config['targets']:
        url=instagram_target(value)
        targets['post' if any(p in url for p in ('/p/','/reel/')) else 'profile'].append(url)
    for surface in ('post','profile'):
        if surface in config['surfaces'] and not targets[surface]:
            raise ValidationError('帖子浏览需填写完整帖子链接' if surface=='post' else '主页浏览需填写用户名')
    return targets

def build_nurture_steps(config):
    # Partition the configured duration without creating a final sub-eight-second
    # video. Runtime additionally enforces the actual active-time budget.
    target=config['minutes']*60
    steps=[]
    while target:
        duration=target if target<=20 else random.randint(8,min(20,target-8))
        steps.append({'surface':'reels','url':'https://www.instagram.com/reels/','seconds':duration})
        target-=duration
    return steps


def standalone_config(config):
    if not isinstance(config,dict) or config.get('standalone_policy')!=NURTURE_POLICY:
        raise ValidationError('旧版养号计划不能继续执行，请保留历史并重新创建养号任务')
    fixed=config_for('nurture',{key:value for key,value in config.items() if key not in {'steps','standalone_policy'}})
    steps=config.get('steps')
    if not isinstance(steps,list) or not steps or any(not isinstance(step,dict) or
            step.get('surface')!='reels' or step.get('url')!='https://www.instagram.com/reels/' or
            type(step.get('seconds')) not in (int,float) or not 8<=step['seconds']<=20 for step in steps):
        raise ValidationError('养号步骤不符合固定策略，请重新创建任务')
    if abs(sum(step['seconds'] for step in steps)-fixed['minutes']*60)>.001:
        raise ValidationError('养号步骤时长与配置不一致，请重新创建任务')
    return dict(fixed,steps=steps,standalone_policy=NURTURE_POLICY)

class StudioManager:
    def __init__(self, service, bitbrowser):
        self.service=service; self.db=service.database; self.bitbrowser=bitbrowser
        self.tasks={}; self.gates={}; self.task_meta={}; self.task_profiles={}; self.scheduler=None; self.stopping=False
        # Control operations are short, loop-owned state transitions. Share this
        # lock with failed-job removal so a retry and removal cannot both win.
        self.control_lock=asyncio.Lock()
        self.scheduler_error=''
        self.cleanup_recovery_ids=[];self.cleanup_recovery_task=None
        self.cleanup_profile_active=None
        self.reconcile_retired_window=None
        self.cleanup_recovery_retry_delays=(5.0,15.0,60.0)
        self.cleanup_recovery_batch_size=25

    def active_ids(self):
        # Inventory reads can run beside loop-owned registration and cleanup.
        return {k for k,t in tuple(self.tasks.items()) if not t.done()}

    def recover(self):
        with self.db.write() as c:
            c.execute("UPDATE studio_jobs SET status=CASE WHEN inflight=1 THEN 'needs_review' ELSE 'paused' END, message=CASE WHEN inflight=1 THEN '上次在提交动作时中断，请人工确认结果' ELSE '上次运行中断，可继续未完成步骤' END WHERE kind='nurture' AND status IN ('running','waiting_window')")
            # A completed round can still own a browser whose retirement failed
            # or was interrupted. Keep that exact generation across startup.
            c.execute("""DELETE FROM browser_operation_leases WHERE operation_type='studio'
                AND EXISTS (SELECT 1 FROM studio_jobs own_job WHERE own_job.id=browser_operation_leases.entity_id AND own_job.kind='nurture' AND own_job.owner_user_id=browser_operation_leases.owner_user_id AND own_job.profile_id=browser_operation_leases.profile_id)
                AND NOT EXISTS (SELECT 1 FROM studio_jobs job
                    WHERE job.profile_id=browser_operation_leases.profile_id AND job.kind='nurture'
                    AND job.status='completed' AND json_extract(job.result_json,'$.window_hold')=1)""")
            for row in c.execute("SELECT * FROM studio_jobs WHERE kind='nurture' AND status='completed' AND json_extract(result_json,'$.window_hold')=1").fetchall():
                saved=json.loads(row['result_json'])
                receipt=saved.setdefault('window_cleanup',{'state':'pending'})
                lease=c.execute("SELECT lease_token FROM browser_operation_leases WHERE profile_id=? AND owner_user_id=? AND operation_type='studio' AND entity_id=?",(row['profile_id'],row['owner_user_id'],row['id'])).fetchone()
                # Upgrade legacy completed-but-held rows only from an existing
                # owned lease. Never create a new generation to finish an old one.
                if not receipt.get('lease_token') and lease:receipt['lease_token']=lease['lease_token']
                owned=lease is not None and receipt.get('lease_token')==lease['lease_token']
                receipt['state']='pending' if owned else 'lease_lost'
                if not owned:self.cleanup_recovery_ids.append((row['owner_user_id'],row['id'],row['profile_id']))
                if owned:self.db.live_browser_lease_tokens.add(lease['lease_token'])
                c.execute('UPDATE studio_jobs SET result_json=?,message=? WHERE id=?',(json.dumps(saved,ensure_ascii=False),'执行已完成，正在重试关闭窗口' if owned else '执行已完成；窗口占用凭证已失效，保留清理记录等待核验',row['id']))
            held=[dict(r) for r in c.execute("SELECT id,owner_user_id,profile_id FROM studio_jobs WHERE kind='nurture' AND deleted_at IS NULL AND (status='needs_review' OR (status='paused' AND json_extract(result_json,'$.window_hold')=1))")]
        for row in held:
            try:self.service._acquire_browser_lease_record(row['owner_user_id'],row['profile_id'],operation_type='studio',entity_id=row['id'],ttl_seconds=600)
            except ConflictError:pass

    def start_scheduler(self):
        if self.scheduler is not None and not self.scheduler.done(): return
        self.stopping=False
        self.scheduler=asyncio.create_task(self._schedule())
        if self.cleanup_recovery_ids and (self.cleanup_recovery_task is None or self.cleanup_recovery_task.done()):
            pending=list(dict.fromkeys(self.cleanup_recovery_ids));self.cleanup_recovery_ids.clear()
            self.cleanup_recovery_task=asyncio.create_task(self._recover_closed_nurture(pending))

    async def _recover_closed_nurture(self, pending):
        # Retry transport availability at a bounded cadence, independently of
        # the per-second job scheduler. An actually open/busy profile requires
        # explicit user recovery; only unavailable proof defers remaining work.
        from .nurture_cleanup_recovery import reconcile_closed_nurture
        import logging
        remaining=list(pending);failed_profiles=set();retry_index=0
        while remaining and not self.stopping:
            batch=remaining[:self.cleanup_recovery_batch_size]
            remaining=remaining[self.cleanup_recovery_batch_size:]
            for index,(owner,ident,profile) in enumerate(batch):
                if self.stopping:return
                if profile in failed_profiles:continue
                try:
                    await finish_preparation(reconcile_closed_nurture,self,owner,ident)
                    retry_index=0
                except asyncio.CancelledError:raise
                except UpstreamUnavailableError as exc:
                    if exc.details.get('reason')=='closed_profile_guard_unsupported':
                        failed_profiles.add(profile)
                        continue
                    # Stop this entire batch at its first unavailable provider.
                    # The thread has drained and released its fences before sleep.
                    remaining=batch[index:]+remaining
                    delays=self.cleanup_recovery_retry_delays
                    delay=delays[min(retry_index,len(delays)-1)];retry_index+=1
                    logging.getLogger(__name__).info('Historical nurture cleanup deferred until the closed-state provider is available')
                    await asyncio.sleep(delay)
                    break
                except Exception:
                    failed_profiles.add(profile)
                    logging.getLogger(__name__).info('Historical nurture cleanup remains fenced after closed-state check')
            await asyncio.sleep(0)

    def get(self, owner, ident):
        with self.db.read() as c:
            row=c.execute('SELECT * FROM studio_jobs WHERE id=? AND owner_user_id=?',(ident,owner)).fetchone()
        if not row or row['deleted_at']: raise NotFoundError('任务不存在')
        return dict(row)

    def update(self, ident, **fields):
        allowed={'status','cursor','total_steps','inflight','result_json','message','config_json','due_at'}
        if not set(fields)<=allowed: raise ValueError('invalid job update')
        with self.db.write() as c:
            c.execute('UPDATE studio_jobs SET '+','.join(k+'=?' for k in fields)+',updated_at=? WHERE id=?',(*fields.values(),isoformat(),ident))

    async def command(self, owner, body):
        action=body.get('action'); kind=body.get('kind')
        if action=='delete_failed_nurture':
            from .studio_history import delete_failed_nurture
            ident=body.get('job_id')
            if not isinstance(ident,str) or not ident.strip():raise ValidationError('任务编号无效')
            return await delete_failed_nurture(self,owner,ident)
        if action=='control': return await self.control(owner,str(body.get('job_id','')),body.get('operation'))
        if action=='start_waiting_nurture': return self.start_waiting_nurture(owner,body.get('job_ids'))
        if kind != 'nurture': raise ValidationError('仅支持养号任务')
        config=config_for(kind,body.get('config',{}))
        if action=='save_template':
            if kind=='nurture':nurture_targets(config)
            with self.db.write() as c:
                c.execute('INSERT INTO studio_templates VALUES(?,?,?,?) ON CONFLICT(owner_user_id,kind) DO UPDATE SET config_json=excluded.config_json,updated_at=excluded.updated_at',
                          (owner,kind,json.dumps(config,ensure_ascii=False),isoformat()))
            return {'saved':True}
        if action!='start': raise ValidationError('无效操作')
        if not isinstance(body.get('profile_ids',[]),list): raise ValidationError('窗口列表格式无效')
        profiles=list(dict.fromkeys(str(p).strip() for p in body.get('profile_ids',[]) if str(p).strip()))
        if not profiles: raise ValidationError('请选择窗口')
        if kind=='nurture' and len(profiles)>1000:raise ValidationError('每批养号最多选择 1000 个窗口')
        request=str(body.get('request_id',''))
        if not 10<=len(request)<=100: raise ValidationError('缺少任务请求标识')
        try:
            due=datetime.fromisoformat(config['scheduled_at'].replace('Z','+00:00')) if config['scheduled_at'] else datetime.now(timezone.utc)
            if due.tzinfo is None: raise ValueError()
            due=due.astimezone(timezone.utc)
        except ValueError: raise ValidationError('计划时间必须包含时区') from None
        jobs=[]; rounds=config['rounds'] if kind=='nurture' else 1
        with self.db.write() as c:
            for round_index in range(rounds):
                for i,profile in enumerate(profiles):
                    cfg=dict(config)
                    if kind=='nurture':
                        cfg['steps']=build_nurture_steps(config)
                        cfg['standalone_policy']=NURTURE_POLICY
                        cfg['concurrency']=config['concurrency'] or len(profiles)
                    job_id=str(uuid.uuid4()); key=f'{request}:{round_index}:{i}'
                    existing=c.execute('SELECT id FROM studio_jobs WHERE owner_user_id=? AND request_key=?',(owner,key)).fetchone()
                    if existing:
                        jobs.append(existing['id']);continue
                    if kind=='nurture' and c.execute("SELECT 1 FROM studio_jobs WHERE profile_id=? AND kind='nurture' AND deleted_at IS NULL AND status IN ('queued','waiting_window','running','paused','needs_review')",(profile,)).fetchone():
                        raise ConflictError('所选窗口已有未完成任务，请先停止或完成原任务')
                    from .browser_admission import assert_no_durable_window_hold
                    assert_no_durable_window_hold(c,owner,profile)
                    if c.execute('SELECT 1 FROM browser_operation_leases WHERE profile_id=?',(profile,)).fetchone():
                        raise ConflictError('所选窗口已被任务占用，请等待释放后重新选择')
                    if c.execute("SELECT 1 FROM studio_jobs WHERE profile_id=? AND kind='nurture' AND status='completed' AND json_extract(result_json,'$.window_hold')=1",(profile,)).fetchone():
                        raise ConflictError('所选窗口的养号清理仍待确认，请等待释放后重新选择')
                    planned=due+timedelta(seconds=config['interval_seconds']*(round_index*len(profiles)+i))
                    c.execute('INSERT OR IGNORE INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,config_json,total_steps,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (job_id,owner,key,kind,profile,json.dumps(cfg,ensure_ascii=False),len(cfg.get('steps',[])) or 1,isoformat(planned),isoformat(),isoformat()))
                    row=c.execute('SELECT id FROM studio_jobs WHERE owner_user_id=? AND request_key=?',(owner,key)).fetchone();jobs.append(row['id'])
        return {'job_ids':jobs}

    def start_waiting_nurture(self, owner, job_ids):
        """Explicitly advance one waiting round per free window, preserving progress."""
        if not isinstance(job_ids,list) or not job_ids or any(not isinstance(i,str) or not i for i in job_ids):
            raise ValidationError('请选择等待中的养号任务')
        ids=list(dict.fromkeys(job_ids));active=self.active_ids();now=isoformat()
        used=sum(meta==(owner,'nurture') for ident,meta in self.task_meta.items() if ident in active)
        ready=[];skipped=[];profiles=set()
        with self.db.write() as c:
            # Validate ownership for the whole command before updating any record.
            rows=[]
            for ident in ids:
                row=c.execute('SELECT rowid AS queue_order,* FROM studio_jobs WHERE id=? AND owner_user_id=? AND deleted_at IS NULL',(ident,owner)).fetchone()
                if row is None: raise NotFoundError('任务不存在')
                rows.append(row)
            for row in sorted(rows,key=lambda r:(r['due_at'],r['created_at'],r['queue_order'])):
                reason=''
                if row['kind']!='nurture' or row['status'] not in {'queued','waiting_window'} or row['inflight'] or row['id'] in active:
                    reason='仅可启动等待中的养号任务'
                elif row['profile_id'] in profiles:
                    reason='同一窗口本次只启动一轮'
                elif c.execute('SELECT 1 FROM browser_operation_leases WHERE profile_id=?',(row['profile_id'],)).fetchone():
                    reason='窗口被任务占用，等待当前任务释放'
                elif c.execute("SELECT 1 FROM studio_jobs WHERE profile_id=? AND kind='nurture' AND status='completed' AND json_extract(result_json,'$.window_hold')=1",(row['profile_id'],)).fetchone():
                    reason='窗口清理尚未确认，等待当前任务释放'
                else:
                    head=c.execute("SELECT id FROM studio_jobs WHERE owner_user_id=? AND profile_id=? AND kind='nurture' AND deleted_at IS NULL AND status IN ('queued','waiting_window','running','paused','needs_review') ORDER BY due_at,created_at,rowid LIMIT 1",(owner,row['profile_id'])).fetchone()
                    if head is not None and head['id']!=row['id']:reason='前一轮未结束，本次保留后续轮次'
                if reason:
                    skipped.append({'job_id':row['id'],'reason':reason});continue
                try:
                    cfg=standalone_config(json.loads(row['config_json']))
                    if not isinstance(cfg,dict):raise ValueError()
                except (ValueError,TypeError,ValidationError):
                    skipped.append({'job_id':row['id'],'reason':'计划配置无效，请重新生成任务'});continue
                profiles.add(row['profile_id']);ready.append((row,cfg))
            concurrency=used+len(profiles)
            for row,cfg in ready:
                cfg['concurrency']=max(1,concurrency)
                if cfg['concurrency']>1000:raise ValidationError('同时执行窗口数量不能超过 1000')
                c.execute("UPDATE studio_jobs SET status='queued',due_at=?,config_json=?,message=?,updated_at=? WHERE id=?",
                          (now,json.dumps(cfg,ensure_ascii=False),'已请求立即并行执行，保留原有进度',now,row['id']))
        return {'job_ids':[row['id'] for row,_ in ready],'skipped':skipped}

    async def control(self, owner, ident, operation):
        async with self.control_lock:
            return await self._control(owner,ident,operation)

    async def _control(self, owner, ident, operation):
        if operation=='retry_cleanup':
            with self.db.read() as c:
                saved=c.execute('SELECT * FROM studio_jobs WHERE id=? AND owner_user_id=?',(ident,owner)).fetchone()
            if saved is None:raise NotFoundError('任务不存在')
            row=dict(saved)
        else:row=self.get(owner,ident)
        if row['kind']!='nurture':raise ValidationError('仅支持养号任务')
        if operation=='retry_cleanup':
            result=json.loads(row['result_json'])
            if row['kind']!='nurture' or row['status']!='completed' or not result.get('window_hold'):
                raise ConflictError('仅可重试已完成养号任务的窗口清理')
            if self.reconcile_retired_window is not None:
                await finish_preparation(self.reconcile_retired_window,owner,row['profile_id'])
            with self.db.read() as c:
                owned=c.execute("SELECT lease_token FROM browser_operation_leases WHERE profile_id=? AND owner_user_id=? AND operation_type='studio' AND entity_id=?",(row['profile_id'],owner,ident)).fetchone()
            if result.get('window_cleanup',{}).get('state')=='lease_lost' or owned is None:
                from .nurture_cleanup_recovery import reconcile_closed_nurture
                return await finish_preparation(reconcile_closed_nurture,self,owner,ident)
            self._schedule_nurture_cleanup()
            return {'status':row['status'],'cleanup_pending':True}
        if row['kind']=='nurture' and operation in {'resume','retry'}:
            standalone_config(json.loads(row['config_json']))
        if operation in {'confirm_actions','confirm_no_actions','cancel_review'}:
            if row['kind']!='nurture' or row['status']!='needs_review':raise ConflictError('仅能核验结果待确认的养号任务')
            if ident in self.active_ids():raise ConflictError('任务正在清理旧页面，请稍后再处理')
            result=json.loads(row['result_json']);counts=result.setdefault('counts',{})
            pending=[item for item in result.get('nurture_actions',{}).values() if item.get('state')=='pending']
            if not pending and operation=='confirm_actions':raise ConflictError('旧任务缺少逐项动作记录，无法安全计入次数；请停止此轮或在确认未执行后继续')
            for item in pending:
                if item.get('state')!='pending':continue
                if operation=='confirm_actions':
                    counts[item['action']]=counts.get(item['action'],0)+1;item['state']='confirmed'
                else:item['state']='not_executed' if operation=='confirm_no_actions' else 'unresolved_stopped'
            result.setdefault('review_history',[]).append({'at':isoformat(),'decision':operation})
            self.commit_nurture_step(owner,ident,None,result)
            status='cancelled' if operation=='cancel_review' else 'paused'
            self.update(ident,status=status,inflight=0,message='已停止此轮并释放窗口' if status=='cancelled' else '已记录人工核验，点击继续执行剩余步骤')
            if status=='cancelled':self.service.release_browser_leases_for_entity(owner,ident,operation_type='studio')
            return {'status':status}
        if operation=='retry':
            if ident in self.active_ids(): raise ConflictError('任务正在释放窗口，请稍后再处理')
            # Re-read inside the write transaction: an archive/removal through
            # another connection must win over this command's earlier read.
            with self.db.write() as c:
                current=c.execute('SELECT * FROM studio_jobs WHERE id=? AND owner_user_id=?',(ident,owner)).fetchone()
                if current is None or current['deleted_at']:raise NotFoundError('任务不存在')
                if current['kind']!='nurture':raise ValidationError('仅支持养号任务')
                standalone_config(json.loads(current['config_json']))
                if current['status']!='failed' or current['inflight']:raise ConflictError('只可重试明确失败且未提交的任务')
                result=json.loads(current['result_json'])
                result.setdefault('attempts',[]).append({'at':isoformat(),'message':current['message'],'failure':result.pop('failure',None)})
                now=isoformat()
                c.execute("UPDATE studio_jobs SET status='queued',due_at=?,result_json=?,message=?,updated_at=? WHERE id=? AND owner_user_id=? AND deleted_at IS NULL",(now,json.dumps(result,ensure_ascii=False),'已排队重试',now,ident,owner))
            return {'status':'queued'}
        if operation not in {'pause','resume','cancel'}: raise ValidationError('无效控制操作')
        if row['status'] in TERMINAL: raise ConflictError('任务已经结束；结果待确认的任务不能自动重试')
        task=self.tasks.get(ident); gate=self.gates.get(ident)
        if operation=='pause':
            self.update(ident,status='paused',message='已暂停，进度保留')
            if gate: gate.clear()
        elif operation=='resume':
            self.update(ident,status='running' if task and not task.done() else 'queued',message='继续执行')
            if gate: gate.set()
        else:
            self.update(ident,status='needs_review' if row['inflight'] else 'cancelled',message='提交结果待确认' if row['inflight'] else '任务已取消，已完成记录保留')
            if gate: gate.set()
            if task: task.cancel()
            elif not row['inflight']:
                self.service.release_browser_leases_for_entity(owner,ident,operation_type='studio')
        return {'status':self.get(owner,ident)['status']}

    def _task_finished(self, ident, task):
        if self.tasks.get(ident) is not task: return
        self.tasks.pop(ident,None);self.gates.pop(ident,None)
        self.task_meta.pop(ident,None);self.task_profiles.pop(ident,None)
        if not task.cancelled(): task.exception()

    def _schedule_ready(self):
        self._schedule_nurture_cleanup()
        active=self.active_ids()
        occupied={p for ident,p in self.task_profiles.items() if ident in active and p}
        usage={}
        for ident,meta in self.task_meta.items():
            if ident in active: usage[meta]=usage.get(meta,0)+1
        with self.db.read() as c:
            rows=c.execute("SELECT * FROM studio_jobs WHERE deleted_at IS NULL AND status IN ('queued','waiting_window') AND kind='nurture' AND due_at<=? ORDER BY due_at,created_at,rowid",(isoformat(),)).fetchall()
            locked={r[0] for r in c.execute('SELECT profile_id FROM browser_operation_leases')}
            locked.update(r[0] for r in c.execute("SELECT profile_id FROM studio_jobs WHERE kind='nurture' AND status='completed' AND json_extract(result_json,'$.window_hold')=1"))
            heads={}
            for r in c.execute("SELECT id,owner_user_id,profile_id FROM studio_jobs WHERE kind='nurture' AND deleted_at IS NULL AND status IN ('queued','waiting_window','running','paused','needs_review') ORDER BY due_at,created_at,rowid"):
                heads.setdefault((r['owner_user_id'],r['profile_id']),r['id'])
        for row in rows:
            if row['id'] in active: continue
            try:
                cfg=json.loads(row['config_json'])
                if row['kind']=='nurture':cfg=standalone_config(cfg)
                limit=cfg['concurrency']
                if isinstance(limit,bool) or not isinstance(limit,int) or limit<1: raise ValueError()
            except ValidationError as exc:
                self.update(row['id'],status='paused',message=str(exc));continue
            except (ValueError,KeyError,TypeError):
                self.update(row['id'],status='failed',message='计划并发配置无效，请重新生成任务');continue
            # A busy account never consumes a runnable slot for another window.
            if row['profile_id'] and row['profile_id'] in locked|occupied:
                message='等待当前任务释放窗口'
                if row['status']!='waiting_window' or row['message']!=message:
                    self.update(row['id'],status='waiting_window',message=message)
                continue
            if row['kind']=='nurture' and heads.get((row['owner_user_id'],row['profile_id']))!=row['id']:
                message='等待本窗口前一轮完成或继续'
                if row['status']!='queued' or row['message']!=message:
                    self.update(row['id'],status='queued',message=message)
                continue
            meta=(row['owner_user_id'],row['kind'])
            if usage.get(meta,0)>=limit:
                if row['status']!='queued' or row['message']!='等待并发名额，前一任务释放后自动执行':
                    self.update(row['id'],status='queued',message='等待并发名额，前一任务释放后自动执行')
                continue
            ident=row['id'];gate=asyncio.Event();gate.set();self.gates[ident]=gate
            task=asyncio.create_task(self._execute(dict(row)))
            self.tasks[ident]=task;self.task_meta[ident]=meta;self.task_profiles[ident]=row['profile_id']
            usage[meta]=usage.get(meta,0)+1
            if row['profile_id']:occupied.add(row['profile_id'])
            task.add_done_callback(lambda t,key=ident:self._task_finished(key,t))

    def _schedule_nurture_cleanup(self):
        """Retry retirement independently of the already completed activity."""
        with self.db.read() as c:
            rows=c.execute("SELECT * FROM studio_jobs WHERE kind='nurture' AND status='completed' AND json_extract(result_json,'$.window_hold')=1 AND json_extract(result_json,'$.window_cleanup.state')='pending'").fetchall()
        for row in rows:
            ident=row['id']
            if ident in self.active_ids():continue
            token=json.loads(row['result_json'])['window_cleanup'].get('lease_token')
            if not token:continue
            task=asyncio.create_task(self._finish_nurture_cleanup(row['owner_user_id'],ident,row['profile_id'],token))
            self.tasks[ident]=task;self.task_profiles[ident]=row['profile_id']
            # Cleanup must not consume a free account's activity concurrency slot.
            self.task_meta[ident]=(row['owner_user_id'],'cleanup')
            task.add_done_callback(lambda t,key=ident:self._task_finished(key,t))

    async def _finish_nurture_cleanup(self,owner,ident,profile,token,worker=None):
        """Release and clear the durable fence only after confirmed owned close."""
        def owned(connection):
            return connection.execute("SELECT 1 FROM browser_operation_leases WHERE profile_id=? AND lease_token=? AND owner_user_id=? AND operation_type='studio' AND entity_id=?",(profile,token,owner,ident)).fetchone() is not None
        try:
            with self.db.read() as c:has_lease=owned(c)
            if has_lease:
                if worker:await disconnect_worker(worker)
                closed=await close_profile_and_wait(self.service,self.bitbrowser,profile,token)
            else:closed=False
            with self.db.write() as c:
                row=c.execute('SELECT result_json FROM studio_jobs WHERE id=? AND owner_user_id=?',(ident,owner)).fetchone()
                saved=json.loads(row['result_json'])
                receipt=saved['window_cleanup']
                if receipt.get('lease_token')!=token:return
                if not closed or not owned(c):
                    receipt['state']='lease_lost'
                    message='执行已完成；窗口占用凭证已失效，保留清理记录等待核验'
                else:
                    # Single transaction prevents restart seeing a missing lease
                    # alongside a receipt that still says cleanup is pending.
                    c.execute("DELETE FROM browser_operation_leases WHERE profile_id=? AND lease_token=? AND owner_user_id=? AND operation_type='studio' AND entity_id=?",(profile,token,owner,ident))
                    receipt.update(state='closed',confirmed_at=isoformat())
                    saved['window_hold']=False
                    message='已完成，窗口已关闭并释放'
                c.execute('UPDATE studio_jobs SET result_json=?,message=?,updated_at=? WHERE id=? AND owner_user_id=?',(json.dumps(saved,ensure_ascii=False),message,isoformat(),ident,owner))
            if closed and not saved['window_hold']:self.db.live_browser_lease_tokens.discard(token)
        except Exception as exc:
            # Provider failures normally retry inside close_profile_and_wait.
            # An unexpected disconnect/storage/helper failure still cannot grant
            # release authority; leave a scheduler-retryable durable receipt.
            with self.db.write() as c:
                c.execute("UPDATE studio_jobs SET message=?,result_json=json_set(result_json,'$.window_cleanup.last_error',?,'$.window_cleanup.state','pending') WHERE id=? AND owner_user_id=? AND json_extract(result_json,'$.window_cleanup.lease_token')=?",('执行已完成；窗口清理尚未确认，保留占用并自动重试',type(exc).__name__,ident,owner,token))

    async def _schedule(self):
        while not self.stopping:
            try:
                self._schedule_ready()
                self.scheduler_error=''
            except asyncio.CancelledError: raise
            except Exception:
                self.scheduler_error='任务调度暂时异常，正在重试；若持续等待请查看运行日志'
            await asyncio.sleep(1)

    async def _execute(self,row):
        ident=row['id'];owner=row['owner_user_id'];profile=row['profile_id'];kind=row['kind']
        if kind!='nurture':raise ValidationError('仅支持养号任务')
        lease=None;worker=None;renew=None;active_started=None;paused_seconds=0;base_seconds=0;watched_seconds=0.0;watch_committed=0.0;result={}
        def active_seconds():
            return watched_seconds
        def stamp_time():
            if kind=='nurture' and active_started is not None:
                result['nurture_actual_seconds']=round(base_seconds+active_seconds(),3)
        async def checkpoint():
            nonlocal paused_seconds
            if self.stopping: raise asyncio.CancelledError()
            pause_start=time.monotonic()
            was_paused=not self.gates[ident].is_set()
            if was_paused and kind=='nurture' and active_started is not None:
                stamp_time()
                self.update(ident,result_json=json.dumps(result,ensure_ascii=False))
            try:await self.gates[ident].wait()
            finally:
                if was_paused:paused_seconds+=max(0,time.monotonic()-pause_start)
            stamp_time()
            # Use one fresh autocommit read connection for both checkpoint
            # fences. Neither job state nor lease ownership is cached.
            with self.db.read() as c:
                current=c.execute('SELECT status,deleted_at FROM studio_jobs WHERE id=? AND owner_user_id=?',(ident,owner)).fetchone()
                if not current or current['deleted_at']: raise NotFoundError('任务不存在')
                if current['status'] in {'cancelled','needs_review'}: raise asyncio.CancelledError()
                if lease:
                    held=c.execute('SELECT 1 FROM browser_operation_leases WHERE profile_id=? AND lease_token=?',(profile,lease)).fetchone()
                    if not held: raise ConflictError('窗口占用凭证已失效，已停止操作')
        async def effect(message):
            await checkpoint()
            if worker is not None:
                from .work_reports import capture_executor
                if kind!='nurture' or not result.get('account_snapshot',{}).get('username'):
                    result['executor'] = await capture_executor(worker,self.db,owner,profile)
            self.update(ident,inflight=1,message=message,result_json=json.dumps(result,ensure_ascii=False))
        async def progress(message):
            await checkpoint();self.update(ident,message=message)
        async def diagnostic(data):
            await checkpoint()
            result['entry_diagnostics']=data
            self.update(ident,result_json=json.dumps(result,ensure_ascii=False))
        async def account_snapshot(data):
            await checkpoint()
            from .account_profile_stats import save_account_snapshot
            save_account_snapshot(self.db,owner,profile,data)
            if kind=='nurture':
                snapshot=dict(data)
                snapshot.setdefault('checked_at',isoformat())
                # Per-attempt snapshots remain bound to this job/window forever;
                # resumption may verify again but never overwrites its first read.
                if not result.get('account_snapshot',{}).get('username'):result['account_snapshot']=snapshot
                result.setdefault('account_snapshots',[]).append(snapshot)
                from .work_reports import capture_executor
                if data.get('username') or not result.get('executor',{}).get('username'):
                    result['executor']=await capture_executor(worker,self.db,owner,profile)
                    result['executor'].update(username=data.get('username',''),instagram_user_id=data.get('instagram_user_id',''))
            else:
                from .work_reports import capture_executor
                result['executor']=await capture_executor(worker,self.db,owner,profile)
                if data.get('username'):result['executor']['username']=data['username']
            self.update(ident,result_json=json.dumps(result,ensure_ascii=False))
        async def nurture_ready():
            nonlocal active_started,paused_seconds
            await checkpoint()
            active_started=time.monotonic();paused_seconds=0
            result.setdefault('nurture_reels_started_at',isoformat())
            result['nurture_clock_policy']='verified-playback-v2'
            stamp_time()
            self.update(ident,result_json=json.dumps(result,ensure_ascii=False))
        async def nurture_watched(seconds):
            nonlocal watched_seconds,watch_committed
            await checkpoint()
            if active_started is None:raise ValidationError('视频尚未核验，不得累计养号时长')
            if type(seconds) not in (int,float) or not 0<=seconds<=20.0:
                raise ValidationError('养号播放采样时长无效')
            watched_seconds+=min(seconds,max(0,config['minutes']*60-base_seconds-watched_seconds))
            stamp_time()
            if watched_seconds-watch_committed>=1.0 or base_seconds+watched_seconds>=config['minutes']*60:
                self.update(ident,result_json=json.dumps(result,ensure_ascii=False))
                watch_committed=watched_seconds
        async def nurture_decision(key,data):
            await checkpoint()
            result.setdefault('nurture_decisions',{})[key]=dict(data)
            self.update(ident,result_json=json.dumps(result,ensure_ascii=False))
        async def nurture_observation(data):
            await checkpoint()
            observations=result.setdefault('nurture_observations',[])
            observations.append({**data,'step':self.get(owner,ident)['cursor']+1})
            del observations[:-40]
            summary=result.setdefault('nurture_summary',{})
            key=data['action']+':'+data['status']
            summary[key]=summary.get(key,0)+1
            self.update(ident,result_json=json.dumps(result,ensure_ascii=False))
        async def nurture_browsed(counts):
            await checkpoint()
            result['counts']=dict(counts)
            self.commit_nurture_step(owner,ident,None,result)
        async def nurture_confirmed(counts):
            result['counts']=dict(counts)
            self.commit_nurture_step(owner,ident,None,result)
        async def nurture_action_begin(token,data):
            await checkpoint()
            result.setdefault('nurture_actions',{})[token]={**data,'started_at':isoformat()}
            self.update(ident,inflight=1,result_json=json.dumps(result,ensure_ascii=False),message='正在执行'+data['action'])
        async def nurture_action_confirmed(token,counts):
            # Observed success is persisted before a pause/cancel can interrupt.
            result['nurture_actions'][token]['state']='confirmed'
            result['counts']=dict(counts)
            self.commit_nurture_step(owner,ident,None,result)
        try:
            await checkpoint()
            config=json.loads(row['config_json']);result=json.loads(row['result_json'])
            value=result.get('nurture_actual_seconds',0)
            base_seconds=float(value) if type(value) in (int,float) and 0<=value<=86400 else 0
            if kind=='nurture':
                try:config=standalone_config(config)
                except ValidationError as exc:
                    self.update(ident,status='needs_review' if row['inflight'] else 'paused',message=str(exc));return
                if row['inflight'] or any(item.get('state')=='pending' for item in result.get('nurture_actions',{}).values()):
                    self.update(ident,status='needs_review',inflight=1,message='上次点赞结果待核验，不能自动继续');return
                if base_seconds>0 and result.get('nurture_clock_policy')!='verified-playback-v2':
                    self.update(ident,status='paused',message='旧版实际时长未逐段核验播放，历史已保留；请停止原任务后新建养号任务');return
            self.update(ident,status='running',message='正在准备养号计划')
            if kind=='nurture':
                try:
                    with self.db.read() as c:
                        existing=c.execute("SELECT lease_token FROM browser_operation_leases WHERE owner_user_id=? AND profile_id=? AND operation_type='studio' AND entity_id=?",(owner,profile,ident)).fetchone()
                    lease=existing['lease_token'] if existing else await self.service.acquire_browser_lease_async(owner,profile,operation_type='studio',entity_id=ident,ttl_seconds=600)
                    result['window_hold']=True
                    self.update(ident,result_json=json.dumps(result,ensure_ascii=False))
                except ConflictError:
                    self.update(ident,status='waiting_window',message='等待其他任务释放窗口',due_at=isoformat(datetime.now(timezone.utc)+timedelta(seconds=5))); return
                async def heartbeat():
                    while True:
                        await asyncio.sleep(30)
                        try: await asyncio.to_thread(self.service.renew_browser_lease,profile,lease,ttl_seconds=600)
                        except Exception:
                            task=self.tasks.get(ident)
                            if task: task.cancel()
                            return
                renew=asyncio.create_task(heartbeat())
            if kind=='nurture':
                result.setdefault('nurture_started_at',isoformat())
                unknown={'username':'','instagram_user_id':'','posts_count':None,'followers_count':None,'following_count':None,'checked_at':isoformat(),'status':'unavailable','message':'正在核验自己的主页'}
                result.setdefault('account_snapshot',unknown)
                from .account_profile_stats import save_account_snapshot
                save_account_snapshot(self.db,owner,profile,unknown)
                result.pop('nurture_finished_at',None)
                stamp_time()
                self.update(ident,result_json=json.dumps(result,ensure_ascii=False))
            await progress('正在连接执行窗口')
            worker=PlaywrightWorker(self.bitbrowser)
            await worker.connect(profile,open_if_needed=True)
            browser=StudioBrowser(worker,checkpoint,effect)
            browser.progress=progress
            browser.diagnostic=diagnostic
            browser.account_snapshot=account_snapshot
            browser.nurture_confirmed=nurture_confirmed
            browser.nurture_actions=result.setdefault('nurture_actions',{})
            browser.nurture_action_begin=nurture_action_begin
            browser.nurture_action_confirmed=nurture_action_confirmed
            browser.nurture_observation=nurture_observation
            browser.expected_nurture_identity=result.get('account_snapshot',{})
            browser.nurture_decisions=result.setdefault('nurture_decisions',{})
            browser.nurture_decision=nurture_decision
            browser.nurture_ready=nurture_ready
            browser.nurture_browsed=nurture_browsed
            browser.nurture_elapsed=lambda:base_seconds+active_seconds()
            browser.nurture_clock=lambda:time.monotonic()-paused_seconds
            browser.nurture_watched=nurture_watched
            browser.nurture_remaining=lambda:config['minutes']*60-(base_seconds+active_seconds())
            counts=result.get('counts',{})
            for index in range(row['cursor'],len(config['steps'])):
                await checkpoint()
                remaining=config['minutes']*60-(base_seconds+active_seconds())
                if remaining<8:break
                step=dict(config['steps'][index],seconds=min(config['steps'][index]['seconds'],remaining))
                counts=await browser.nurture_step(step,counts,config)
                stamp_time()
                result['counts']=counts
                self.commit_nurture_step(owner,ident,index+1,result)
            stamp_time()
            message='养号已完成，正在释放窗口' if counts.get('like',0) else '浏览已完成，未确认点赞；请查看互动执行记录'
            finished=isoformat()
            result.setdefault('confirmed_at',finished)
            result['nurture_finished_at']=finished
            result['window_cleanup']={'state':'pending','lease_token':lease}
            self.update(ident,status='completed',result_json=json.dumps(result,ensure_ascii=False),message=message)
        except asyncio.CancelledError:
            current=self.get(owner,ident)
            if current['status'] not in TERMINAL:
                self.update(ident,status='needs_review' if current['inflight'] else 'paused',message='执行中断，提交结果待确认' if current['inflight'] else '已暂停，未完成步骤可继续')
        except Exception as exc:
            current=self.get(owner,ident)
            uncertain=bool(current['inflight'] or isinstance(exc,ResultUncertain))
            message=str(exc)[:240]
            saved=json.loads(current['result_json'])
            saved['failure']={'stage':current['message'],'message':message,'at':isoformat(),'result_uncertain':uncertain}
            self.update(ident,status='needs_review' if uncertain else 'failed',inflight=1 if uncertain else 0,result_json=json.dumps(saved,ensure_ascii=False),message=message)
        finally:
            async def cleanup():
                if kind=='nurture' and result.get('nurture_started_at'):
                    current=self.get(owner,ident)
                    saved=json.loads(current['result_json'])
                    saved['nurture_actual_seconds']=round(base_seconds+active_seconds(),3)
                    saved['nurture_outcome']=current['status']
                    if not saved.get('account_snapshots'):
                        snapshot=dict(saved.get('account_snapshot',{}),message=current['message'],checked_at=isoformat())
                        saved['account_snapshot']=snapshot
                        saved['account_snapshots']=[snapshot]
                    if current['status'] in TERMINAL:saved.setdefault('nurture_finished_at',isoformat())
                    self.update(ident,result_json=json.dumps(saved,ensure_ascii=False))
                if lease:
                    if kind=='nurture' and self.get(owner,ident)['status']=='completed':
                        await self._finish_nurture_cleanup(owner,ident,profile,lease,worker)
                        return
                    try:
                        if worker: await disconnect_worker(worker)
                    except Exception: pass
                    close_failed=False
                    try:
                        if self.get(owner,ident)['status']=='completed':
                            await close_profile_and_wait(self.service,self.bitbrowser,profile,lease)
                    except Exception: close_failed=True
                    finally:
                        if self.get(owner,ident)['status'] not in {'paused','needs_review'}:
                            self.service.release_browser_lease(profile,lease)
                            # Clear only after this exact owner's lease release.
                            with self.db.write() as c:
                                c.execute("UPDATE studio_jobs SET result_json=json_set(result_json,'$.window_hold',json('false')) WHERE id=? AND owner_user_id=?",(ident,owner))
                    current=self.get(owner,ident)
                    if current['status']=='completed':
                        saved=json.loads(current['result_json'])
                        message='执行已完成；窗口关闭失败，请手动关闭' if close_failed else '已完成，窗口已关闭并释放'
                        self.update(ident,message=message)
            # Shutdown can arrive after the result is saved but before disconnect.
            # Do not allow cancellation to interrupt the owned window cleanup.
            from .async_cleanup import finish_owned
            try:await finish_owned(cleanup())
            finally:
                if renew:renew.cancel();await asyncio.gather(renew,return_exceptions=True)

    def commit_nurture_step(self,owner,ident,cursor,result):
        now=isoformat()
        with self.db.write() as c:
            row=c.execute('SELECT * FROM studio_jobs WHERE id=? AND owner_user_id=?',(ident,owner)).fetchone()
            if row is None: raise NotFoundError('任务不存在')
            if cursor is not None and cursor<=row['cursor']: return
            previous=json.loads(row['result_json']).get('counts',{})
            for action,count in result.get('counts',{}).items():
                delta=max(0,count-previous.get(action,0))
                c.execute('INSERT INTO studio_daily_actions VALUES(?,?,?,?,?) ON CONFLICT(owner_user_id,profile_id,day,action) DO UPDATE SET count=count+excluded.count',
                          (owner,row['profile_id'],now[:10],action,delta))
            c.execute('UPDATE studio_jobs SET cursor=?,inflight=0,result_json=?,message=?,updated_at=? WHERE id=?',
                      (row['cursor'] if cursor is None else cursor,json.dumps(result),'已确认互动，继续当前浏览步骤' if cursor is None else f'完成 {cursor}/{row["total_steps"]} 步',now,ident))

    def daily_action_counts(self,owner,profile):
        day=isoformat()[:10]
        with self.db.read() as c:
            counts={r['action']:r['count'] for r in c.execute('SELECT action,count FROM studio_daily_actions WHERE owner_user_id=? AND profile_id=? AND day=?',(owner,profile,day))}
            # Unconfirmed effects consume their daily allowance conservatively,
            # even when the user explicitly stops the unresolved round.
            for row in c.execute("SELECT result_json,updated_at FROM studio_jobs WHERE owner_user_id=? AND profile_id=? AND kind='nurture'",(owner,profile)):
                for item in json.loads(row['result_json']).get('nurture_actions',{}).values():
                    if item.get('state') in {'pending','unresolved_stopped'} and item.get('started_at',row['updated_at'])[:10]==day:
                        action=item['action'];counts[action]=counts.get(action,0)+1
            return counts

    def snapshot(self, owner):
        with self.db.read() as c:
            from .account_profile_stats import account_stats
            window_stats=account_stats(c,owner)
            # Always expose unfinished rounds, including an old paused queue head.
            # Bound only historical rows so they cannot hide actionable work.
            jobs=[dict(r) for r in c.execute("SELECT rowid AS queue_order,* FROM studio_jobs WHERE owner_user_id=? AND kind='nurture' AND deleted_at IS NULL AND status NOT IN ('completed','cancelled') ORDER BY created_at DESC,rowid DESC",(owner,))]
            # A removed failed nurture round leaves the actionable list but
            # retains its original outcome and receipts in bounded history.
            jobs += [dict(r) for r in c.execute("""SELECT rowid AS queue_order,* FROM studio_jobs WHERE owner_user_id=? AND kind='nurture'
                AND ((deleted_at IS NULL AND status IN ('completed','cancelled'))
                    OR (deleted_at IS NOT NULL AND kind='nurture' AND (status='failed'
                        OR (status='completed' AND json_extract(result_json,'$.window_cleanup.state')='reconciled_closed'))))
                ORDER BY COALESCE(json_extract(result_json,'$.window_cleanup.reconciled_at'),deleted_at,created_at) DESC,rowid DESC LIMIT 500""",(owner,))]
            # A durable cleanup fence is actionable even when its completion is
            # older than the history page or its historical card was archived.
            visible={row['id'] for row in jobs}
            jobs += [dict(r) for r in c.execute("SELECT rowid AS queue_order,* FROM studio_jobs WHERE owner_user_id=? AND kind='nurture' AND status='completed' AND json_extract(result_json,'$.window_hold')=1 ORDER BY created_at DESC,rowid DESC",(owner,)) if r['id'] not in visible]
            templates={r['kind']:json.loads(r['config_json']) for r in c.execute("SELECT * FROM studio_templates WHERE owner_user_id=? AND kind='nurture'",(owner,))}
            totals=[dict(r) for r in c.execute("SELECT kind,status,count(*) AS count FROM studio_jobs WHERE owner_user_id=? AND kind='nurture' AND deleted_at IS NULL GROUP BY kind,status",(owner,))]
            daily=[dict(r) for r in c.execute("SELECT substr(updated_at,1,10) AS day,kind,count(*) AS count FROM studio_jobs WHERE owner_user_id=? AND kind='nurture' AND status='completed' GROUP BY day,kind ORDER BY day DESC LIMIT 90",(owner,))]
            monitor=dict(c.execute('SELECT COALESCE(sum(added_count),0) AS added,COALESCE(sum(repeat_count),0) AS repeated FROM follow_monitor_daily_counts WHERE owner_user_id=?',(owner,)).fetchone())
            locked={r[0] for r in c.execute('SELECT profile_id FROM browser_operation_leases')}
            locked.update(r[0] for r in c.execute("SELECT profile_id FROM studio_jobs WHERE kind='nurture' AND status='completed' AND json_extract(result_json,'$.window_hold')=1"))
            heads={}
            for r in c.execute("SELECT id,profile_id FROM studio_jobs WHERE owner_user_id=? AND kind='nurture' AND deleted_at IS NULL AND status IN ('queued','waiting_window','running','paused','needs_review') ORDER BY due_at,created_at,rowid",(owner,)):
                heads.setdefault(r['profile_id'],r['id'])
        active=self.active_ids();now=isoformat()
        occupied={p for ident,p in self.task_profiles.items() if ident in active and p}
        usage={}
        for ident,meta in self.task_meta.items():
            if ident in active:usage[meta]=usage.get(meta,0)+1
        for j in jobs:
            try:
                cfg=json.loads(j.pop('config_json'))
                j['config']=cfg if isinstance(cfg,dict) else {}
            except (ValueError,TypeError):
                j['config']={}
            j['result']=json.loads(j.pop('result_json'))
            if j['kind']=='nurture':
                decisions=j['result'].pop('nurture_decisions',{})
                j['result']['nurture_decision_count']=len(decisions)
                actions=j['result'].get('nurture_actions',{})
                j['result']['nurture_actions']={key:value for key,value in actions.items() if value.get('state') in {'pending','unresolved_stopped'}}
            if j['status'] in {'queued','waiting_window'}:
                limit=j['config'].get('concurrency',1)
                if j['id'] in active:
                    reason,message='starting','正在启动任务'
                elif j['due_at']>now:
                    reason,message='scheduled','尚未到计划时间，可点击立即执行'
                elif j['profile_id'] in locked|occupied:
                    reason,message='window_busy','等待当前任务释放窗口'
                elif j['kind']=='nurture' and heads.get(j['profile_id'])!=j['id']:
                    reason,message='previous_round','等待本窗口前一轮完成或继续'
                elif isinstance(limit,int) and usage.get((owner,j['kind']),0)>=limit:
                    reason,message='capacity','等待并发名额，前一任务释放后自动执行'
                else:
                    reason,message='ready','已到执行时间，等待调度启动'
                j['wait_reason']=reason;j['wait_message']=message
            j.pop('owner_user_id',None);j.pop('request_key',None)
        return {'window_stats':window_stats,'jobs':jobs,'templates':templates,'totals':totals,'daily':daily,'monitor_totals':monitor,
                'active_ids':[j['id'] for j in jobs if j['id'] in active],
                'scheduler_error':self.scheduler_error}

    async def shutdown(self):
        self.stopping=True
        if self.scheduler: self.scheduler.cancel();await asyncio.gather(self.scheduler,return_exceptions=True)
        if self.cleanup_recovery_task:
            self.cleanup_recovery_task.cancel();await asyncio.gather(self.cleanup_recovery_task,return_exceptions=True)
        tasks=list(self.tasks.values())
        for t in tasks: t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
