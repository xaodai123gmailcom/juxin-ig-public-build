"""Exact, read-only lookup and guarded lifecycle stop for hidden paused collection.

Never restores dismissed cards, closes a browser, removes a lease, or clears a
nurture hold. A live/leased/unfinished-target task must use its normal controls.
"""
from __future__ import annotations

import json

from .errors import ConflictError, NotFoundError, ValidationError
from .social_platform import stored_task_settings


def locate_collection_blocker(studio, execution, owner: str, job_id: str):
    with studio.db.read() as connection:
        connection.execute('BEGIN')
        job = connection.execute('SELECT * FROM studio_jobs WHERE id=? AND owner_user_id=?',
                                 (job_id, owner)).fetchone()
        if job is None:
            raise NotFoundError('任务不存在')
        result = json.loads(job['result_json'])
        if job['kind'] != 'nurture' or job['status'] != 'completed' or result.get('window_hold') is not True:
            raise ConflictError('该任务已不需要历史清理核验，请刷新记录')
        profile = job['profile_id']
        # Match the durable cleanup gate, bypassing only list/paging presentation.
        task = connection.execute("""SELECT task.* FROM (
            SELECT window.task_id FROM task_windows window JOIN tasks assigned ON assigned.id=window.task_id
            WHERE window.profile_id=? AND assigned.status NOT IN ('completed','failed','stopped')
            UNION SELECT task_id FROM task_targets WHERE current_window_id=?
                  AND status NOT IN ('completed','failed','stopped','cancelled')
            ) related JOIN tasks task ON task.id=related.task_id ORDER BY task.id LIMIT 1""", (profile, profile)).fetchone()
        if task is None:
            return {'job_id':job_id, 'profile_id':profile, 'blocker':None,
                    'message':'未发现未结束的采集任务；请重新核验窗口清理，以检查其他占用'}
        if task['owner_user_id'] != owner:
            raise ConflictError('该窗口仍有其他用户的未结束任务，请由任务所属用户处理',
                                details={'reason':'unfinished_workflow'})
        stored_task_settings(task['settings_json'])
        windows = [row[0] for row in connection.execute("""SELECT profile_id FROM task_windows WHERE task_id=?
            UNION SELECT current_window_id FROM task_targets WHERE task_id=? AND current_window_id IS NOT NULL
            ORDER BY 1""", (task['id'], task['id']))]
        dismissed = connection.execute('SELECT 1 FROM task_list_dismissals WHERE task_id=? AND owner_user_id=?',
                                       (task['id'], owner)).fetchone() is not None
        unfinished = connection.execute("""SELECT 1 FROM task_targets WHERE task_id=? AND current_window_id IS NOT NULL
            AND status NOT IN ('completed','failed','stopped','cancelled') LIMIT 1""", (task['id'],)).fetchone() is not None
        lease = connection.execute("""SELECT 1 FROM browser_operation_leases lease WHERE
            (lease.operation_type='collection' AND lease.entity_id=?) OR lease.profile_id=?
            OR EXISTS (SELECT 1 FROM task_windows window WHERE window.task_id=? AND window.profile_id=lease.profile_id)
            OR EXISTS (SELECT 1 FROM task_targets target WHERE target.task_id=? AND target.current_window_id=lease.profile_id)
            LIMIT 1""", (task['id'], profile, task['id'], task['id'])).fetchone() is not None
    control = execution._runs.get(task['id'])
    live = bool(control and ((control.coordinator and not control.coordinator.done())
                or any(not worker.done() for worker in control.worker_tasks)))
    external = getattr(studio, 'cleanup_profile_active', None)
    studio_active = studio.active_ids()
    active_profile = (job_id in studio_active or any(
        ident in studio_active and profile in windows
        for ident, profile in tuple(studio.task_profiles.items()))
        or bool(callable(external) and any(external(window) for window in windows)))
    reason = ('该任务已不再暂停，请刷新并通过正常采集控制处理' if task['status'] != 'paused'
              else '仍有活动工作线程，请先通过正常任务控制停止并等待收尾' if live or active_profile
              else '相关窗口仍有占用凭证；保留全部凭证，不能停止历史占用' if lease
              else '仍有绑定窗口的未结束目标，请通过正常采集控制处理' if unfinished
              else '')
    return {'job_id':job_id, 'profile_id':profile, 'blocker':{
        'task_id':task['id'], 'name':task['name'], 'status':task['status'], 'version':task['version'],
        'window_ids':windows, 'dismissed':dismissed, 'can_stop':not bool(reason), 'blocked_reason':reason,
        'message':'此入口按任务编号读取，不受列表归档、筛选、分页或任务卡显示条件影响',
    }}


def stop_inactive_collection_blocker(studio, execution, owner: str, job_id: str,
                                     task_id: str, version: int):
    if not isinstance(task_id, str) or not task_id or type(version) is not int:
        raise ValidationError('请先定位关联任务，再确认停止当前版本')
    # The manager lock is held by the caller. Surface admission cannot insert a
    # new lease between the authoritative recheck and canonical lifecycle stop.
    with studio.db.browser_surface_lock:
        found = locate_collection_blocker(studio, execution, owner, job_id)
        blocker = found['blocker']
        if not blocker or blocker['task_id'] != task_id or blocker['version'] != version:
            raise ConflictError('关联任务已变化，请重新定位后再确认')
        if not blocker['can_stop']:
            raise ConflictError(blocker['blocked_reason'])
        execution.service.control_task(owner, task_id, 'stop', expected_version=version)
    return {'job_id':job_id, 'task_id':task_id, 'status':'stopped', 'cleanup_pending':True,
            'message':'已正常停止这条无活动线程的暂停采集任务；历史和去重保留，请再次核验窗口清理'}
