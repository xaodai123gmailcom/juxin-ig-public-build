"""Authorize presentation of a desktop-owned WebContentsView.

The same database write fence serializes presentation with task acquisition.
Tasks hide the real native view before their lease is committed; no HWND or
external browser embedding is involved.
"""
from __future__ import annotations
import threading
import time
import secrets
import hashlib
from .errors import ConflictError, ValidationError
from .service import isoformat

class AccountSurface:
    def __init__(self,service,native,*,adapter=None):
        self.service=service;self.native=native
        self.adapter=adapter or (native if callable(getattr(native,'surface_show',None)) else None)
        self.lock=threading.RLock();self.interference={}
        # Only the execution manager can confirm that all automation on this
        # lease has reached a safe stopping point. A UI pause flag is not proof.
        self.manual_control_active = None
        self.supported=bool(self.adapter)

    @staticmethod
    def task_key(lease):
        return hashlib.sha256(lease['lease_token'].encode()).hexdigest()[:24] if lease else None

    def permit_interference(self,owner,row,expected_task,target=None):
        if not isinstance(target,str) or not target or len(target)>128:raise ValidationError('请选择正在执行的任务页面')
        if not row['profile_id'].startswith('native:'):raise ValidationError('仅支持内置任务窗口')
        with self.service.database.browser_surface_lock:
            with self.service.database.read() as c:
                current=c.execute('SELECT * FROM account_window_plans WHERE id=? AND owner_user_id=? AND archived=0',(row['id'],owner)).fetchone()
                lease=c.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?',(row['profile_id'],)).fetchone()
            if (not current or current['revision']!=row['revision'] or current['profile_id']!=row['profile_id']
                    or not lease or lease['owner_user_id']!=owner or self.task_key(lease)!=expected_task):
                raise ConflictError('任务状态已变化，请重新查看后确认')
            if not self._manual_is_stopped(owner,row['profile_id'],lease):
                raise ConflictError('自动操作尚未停稳，请等待暂停完成后再操作')
            with self.lock:
                self.interference[row['profile_id']]={
                    'owner':owner,'plan_id':row['id'],'revision':row['revision'],
                    'lease_token':lease['lease_token'],'task_key':expected_task,
                    'target':target,'grant':secrets.token_urlsafe(32),
                }
                return {**self._public_manual(self.interference[row['profile_id']]),
                        'message':'自动采集已停稳，可以操作此页；窗口仍由当前任务占用。'}

    def _manual_is_stopped(self,owner,profile,lease):
        return bool(lease and lease['operation_type']=='collection'
                    and callable(self.manual_control_active)
                    and self.manual_control_active(owner,profile,lease['lease_token']))

    @staticmethod
    def _public_manual(state):
        return {'active':True,'grant':state['grant'],'task_key':state['task_key'],'target':state['target']}

    def _manual_matches(self,owner,row,lease,state):
        return bool(state and lease and state['owner']==owner and state['plan_id']==row['id']
                    and state['revision']==row['revision'] and state['lease_token']==lease['lease_token']
                    and lease['owner_user_id']==owner and self._manual_is_stopped(owner,row['profile_id'],lease))

    def manual_info(self,owner,row):
        with self.service.database.browser_surface_lock:
            with self.service.database.read() as c:
                lease=c.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?',(row['profile_id'],)).fetchone()
            with self.lock:
                state=self.interference.get(row['profile_id'])
                if self._manual_matches(owner,row,lease,state):return self._public_manual(state)
                # Do not let a request by another owner revoke the true owner's grant.
                if state and state['owner']==owner:self.interference.pop(row['profile_id'],None)
                return {'active':False}

    def validate_manual_grant(self,owner,row,task_key,grant):
        with self.service.database.browser_surface_lock:
            with self.service.database.read() as c:
                lease=c.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?',(row['profile_id'],)).fetchone()
            with self.lock:
                state=self.interference.get(row['profile_id'])
                if (not self._manual_matches(owner,row,lease,state) or state['task_key']!=task_key
                        or not isinstance(grant,str) or not secrets.compare_digest(state['grant'],grant)):
                    raise ConflictError('手动操作授权已变化，请刷新任务画面')
                return lease['lease_token']

    def revoke_manual(self,owner,profile):
        # Hide interactive native input before the manager resumes any worker.
        # A failed hide propagates: it must never be treated as permission to run.
        with self.service.database.browser_surface_lock:
            with self.lock:
                state=self.interference.get(profile)
                if state and state['owner']!=owner:raise ConflictError('手动操作归属已变化')
                if self.adapter:self.adapter.surface_hide(profile)
                self.interference.pop(profile,None)

    def revoke_manual_grant(self,owner,row,task_key,grant):
        # Validation and native input revocation are one transaction against a
        # new confirmation; an old grant may not revoke the replacement grant.
        with self.service.database.browser_surface_lock:
            token=self.validate_manual_grant(owner,row,task_key,grant)
            self.revoke_manual(owner,row['profile_id'])
            return token

    def suspend(self,profile=None):
        # This is synchronous and fail-closed before the task lease commits.
        # The desktop watchdog independently hides stale renderer surfaces.
        with self.lock:
            if self.adapter:self.adapter.surface_hide(profile)

    def update(self,owner,row,body):
        if not self.supported:return {'attached':False,'message':'请使用新版桌面程序的内置窗口。'}
        if not body.get('visible'):
            self.suspend();return {'attached':False}
        if not row or not row['profile_id'].startswith('native:'):
            raise ValidationError('仅内置账号窗口支持嵌入显示')
        profile=row['profile_id'];bounds=body.get('bounds',{})
        if any(type(bounds.get(k)) is not int for k in ('x','y','width','height')) or min(bounds['x'],bounds['y'])<0 or not 100<=bounds['width']<=16384 or not 100<=bounds['height']<=16384:
            raise ValidationError('网页显示区域无效')
        with self.service.database.browser_surface_lock:
            with self.service.database.read() as c:
                current=c.execute('SELECT * FROM account_window_plans WHERE id=? AND owner_user_id=? AND archived=0',(row['id'],owner)).fetchone()
                if not current or current['profile_id']!=profile or current['revision']!=row['revision']:
                    raise ConflictError('窗口方案已变化，请刷新后再操作')
                lease=c.execute('SELECT * FROM browser_operation_leases WHERE profile_id=? ',(profile,)).fetchone()
                if lease is None and c.execute("SELECT 1 FROM studio_jobs WHERE profile_id=? AND kind='nurture' AND status='completed' AND json_extract(result_json,'$.window_hold')=1 LIMIT 1",(profile,)).fetchone():
                    raise ConflictError('养号窗口清理仍待核验，请前往养号异常任务核验清理')
                # A lost transient row does not retire a durable ambiguous
                # posting/cleanup hold. With no matching live row even a
                # read-only grant cannot identify a safe task page to expose.
                if (lease is None and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='posting_jobs'").fetchone()
                        and c.execute("SELECT 1 FROM posting_jobs WHERE profile_id=? AND lease_token<>'' LIMIT 1",(profile,)).fetchone()):
                    raise ConflictError('发帖窗口仍待核验，暂不能显示；请先恢复或核对占用记录')
                if lease and body.get('read_only') is not True:
                    # Task acquisition already hides interactive input before
                    # committing its lease, under this same database fence.
                    # An aborted older HTTP request can arrive after a newer
                    # read-only grant: rejecting it must not hide that view.
                    # The presenter hides a denial only if its grant is current.
                    with self.lock:
                        state=self.interference.get(profile)
                        grant=body.get('interference_grant')
                        if (not self._manual_matches(owner,row,lease,state)
                                or state['target']!=body.get('view_target')
                                or not isinstance(grant,str) or not secrets.compare_digest(state['grant'],grant)):
                            raise ConflictError('任务仍占用此窗口，请点击画面确认干扰，并等待自动操作停稳')
            with self.lock:
                if body.get('read_only') is True:
                    if not lease or not body.get('view_target'):raise ConflictError('任务状态已变化，请刷新后查看')
                    return self.adapter.surface_show(profile,bounds,body.get('grant',''),target=body['view_target'],read_only=True)
                if body.get('view_target'):return self.adapter.surface_show(profile,bounds,body.get("grant",""),target=body['view_target'])
                return self.adapter.surface_show(profile,bounds,body.get("grant",""))
