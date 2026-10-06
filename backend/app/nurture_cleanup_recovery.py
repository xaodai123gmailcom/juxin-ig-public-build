"""Reconcile an orphaned completion fence only with an authoritative closed proof.

This operation never opens/closes a browser or adopts/releases a lease. The
provider guard and surface fence remain held through the final SQLite commit.
"""
from __future__ import annotations

import json

from .errors import ConflictError, NotFoundError, UpstreamUnavailableError
from .service import isoformat


def _active(manager, ident, profile):
    active = manager.active_ids()
    external = getattr(manager, 'cleanup_profile_active', None)
    return (callable(external) and bool(external(profile))) or ident in active or any(
        key in active and value == profile
        for key, value in tuple(manager.task_profiles.items())
    )


def _eligible(manager, connection, owner, ident):
    row = connection.execute('SELECT * FROM studio_jobs WHERE id=? AND owner_user_id=?',
                             (ident, owner)).fetchone()
    if row is None:
        raise NotFoundError('任务不存在')
    row = dict(row)
    try:
        result = json.loads(row['result_json'])
        actions = result.get('nurture_actions', {})
        if not isinstance(actions, dict) or any(not isinstance(item, dict) for item in actions.values()):
            raise ValueError()
        unresolved = any(item.get('state') not in {'confirmed', 'not_executed'} for item in actions.values())
    except (ValueError, TypeError, AttributeError):
        raise ConflictError('清理记录无效，请先核验原始记录') from None
    if (row['kind'] != 'nurture' or row['status'] != 'completed'
            or result.get('window_hold') is not True or row['inflight'] or unresolved):
        raise ConflictError('仅可核验已完成且无待确认互动的养号清理记录')
    profile = row['profile_id']
    if not profile or _active(manager, ident, profile):
        raise ConflictError('窗口仍有活动任务，不能核验历史清理')
    # ANY generation is authoritative. In particular, never remove a successor
    # owned by another user or infer expiry from a timestamp.
    if connection.execute('SELECT 1 FROM browser_operation_leases WHERE profile_id=?', (profile,)).fetchone():
        raise ConflictError('窗口仍有占用凭证，请等待当前任务释放')
    unfinished = connection.execute("""SELECT id,owner_user_id,status,kind FROM studio_jobs job WHERE profile_id=? AND id<>?
        AND (status NOT IN ('completed','failed','cancelled') OR inflight=1 OR EXISTS (
            SELECT 1 FROM json_each(job.result_json,'$.nurture_actions') action
            WHERE COALESCE(json_extract(action.value,'$.state'),'') NOT IN ('confirmed','not_executed'))) LIMIT 1""", (profile, ident)).fetchone()
    collection = connection.execute("""SELECT task.id,task.owner_user_id,task.status FROM task_windows window JOIN tasks task ON task.id=window.task_id
        WHERE window.profile_id=? AND task.status NOT IN ('completed','failed','stopped') LIMIT 1""", (profile,)).fetchone()
    targets = connection.execute("""SELECT target.task_id AS id,task.owner_user_id,target.status FROM task_targets target JOIN tasks task ON task.id=target.task_id WHERE target.current_window_id=?
        AND target.status NOT IN ('completed','failed','stopped','cancelled') LIMIT 1""", (profile,)).fetchone()
    actions = connection.execute("""SELECT id,owner_user_id,status FROM action_campaigns WHERE profile_id=?
        AND status NOT IN ('completed','failed','stopped','cancelled') LIMIT 1""", (profile,)).fetchone()
    monitors = connection.execute("""SELECT run.id,run.owner_user_id,run.status FROM follow_monitor_runs run,json_each(run.profile_ids_json) profile
        WHERE profile.value=? AND run.status IN ('queued','running','paused','cancelling') LIMIT 1""", (profile,)).fetchone()
    posting = None
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='posting_jobs'").fetchone():
        posting = connection.execute("""SELECT id,owner_user_id,status FROM posting_jobs WHERE profile_id=?
            AND (lease_token<>'' OR status NOT IN ('completed','failed','cancelled')) LIMIT 1""", (profile,)).fetchone()
    blockers = [
        ('studio', '养号' if unfinished and unfinished['kind'] == 'nurture' else '旧版发布', unfinished),
        ('collection', '采集', collection or targets), ('action', '互动任务', actions),
        ('monitor', '检查', monitors), ('posting', '发布', posting),
    ]
    for module, label, blocker in blockers:
        if blocker is None:continue
        if blocker['owner_user_id'] != owner:
            raise ConflictError('该窗口仍有其他用户的未结束任务，请由任务所属用户处理后再核验',
                                details={'reason':'unfinished_workflow'})
        status = blocker['status']
        status_label = {'queued':'等待执行','waiting_window':'等待窗口','running':'正在执行',
            'paused':'已暂停','ready':'待发布','prepared':'已备稿','review':'待审核',
            'needs_review':'结果待确认','cancelling':'正在停止'}.get(status, status)
        route = f'请到“{label}”处理或停止该任务，再返回养号核验窗口清理'
        if label == '旧版发布':
            route = '这是旧版发布任务；请在历史记录核对该任务，并联系支持处理旧版未结束记录后再核验，勿删除登录资料'
        raise ConflictError(f'窗口仍有关联的{label}任务（{status_label}，任务编号 {blocker["id"]}）。{route}',
            details={'reason':'unfinished_workflow','module':module,'job_id':blocker['id'],
                     'status':status,'action':'resolve_existing_task_then_retry_cleanup'})
    return row, result


def reconcile_closed_nurture(manager, owner, ident):
    """Blocking transaction boundary; callers must execute in a worker thread."""
    guard = getattr(manager.bitbrowser, 'closed_profile_guard', None)
    if not callable(guard):
        raise UpstreamUnavailableError('当前窗口服务不能安全核验关闭状态，请保留清理记录并更新桌面程序',
                                       details={'reason':'closed_profile_guard_unsupported'})
    with manager.db.browser_surface_lock:
        with manager.db.read() as connection:
            original, _ = _eligible(manager, connection, owner, ident)
        # The provider must reject missing/unknown inventory, local opening,
        # closing, reset, connection or action tickets, and any open profile.
        with guard(original['profile_id'], owner) as evidence:
            if (not isinstance(evidence, dict) or evidence.get('closed') is not True
                    or evidence.get('profile_id') != original['profile_id']
                    or evidence.get('owner_user_id') != owner
                    or evidence.get('verification') != 'desktop-absence-v1'):
                raise UpstreamUnavailableError('窗口服务未提供可信的关闭核验，清理占用已保留')
            with manager.db.write() as connection:
                current, result = _eligible(manager, connection, owner, ident)
                if current != original:
                    raise ConflictError('清理记录在核验期间已变化，请刷新后重试')
                receipt = result.get('window_cleanup', {})
                if not isinstance(receipt, dict):
                    raise ConflictError('清理记录无效，请先核验原始记录')
                receipt = dict(receipt)
                receipt['reconciled_from_state'] = receipt.get('state', 'legacy_missing')
                receipt.update(state='reconciled_closed', reconciled_at=isoformat(),
                               reconciliation_evidence=dict(evidence))
                result['window_cleanup'] = receipt
                result['window_hold'] = False
                # Historical completion timestamps and all activity/accounting
                # data remain untouched, including updated_at and receipt token.
                connection.execute('UPDATE studio_jobs SET result_json=?,message=? WHERE id=? AND owner_user_id=?',
                    (json.dumps(result, ensure_ascii=False), '已核验窗口关闭，历史清理占用已解除', ident, owner))
    return {'status': 'completed', 'cleanup_pending': False, 'cleanup_reconciled': True}
