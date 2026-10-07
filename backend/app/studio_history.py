"""Remove task list entries only after the owning worker releases them."""
import asyncio
import json
from .errors import ConflictError, NotFoundError
from .service import isoformat


def failed_nurture_protected_reason(manager, connection, row):
    if row['kind']!='nurture':
        return '仅支持删除明确失败的养号任务'
    if row['status']!='failed' or row['inflight']:
        return '仅能删除明确失败且结果已确认的养号任务'
    try:
        result=json.loads(row['result_json'])
        if not isinstance(result,dict):raise ValueError()
        pending=any(item.get('state')=='pending' for item in result.get('nurture_actions',{}).values())
    except (ValueError,TypeError,AttributeError):
        return '任务结果记录无效，暂时无法安全删除'
    if pending:
        return '仍有动作结果待确认，请先核验结果'
    # A historical JSON flag is not a current lease. Older cleanup released
    # the lease but left window_hold=true, permanently blocking this button.
    if row['id'] in manager.active_ids():
        return '任务仍占用窗口或正在清理，请等待释放后删除'
    if connection.execute("SELECT 1 FROM browser_operation_leases WHERE operation_type='studio' AND entity_id=?",(row['id'],)).fetchone():
        return '窗口占用尚未释放，请稍后删除'
    return ''


async def delete_failed_nurture(manager, owner, ident):
    # No stop, browser close, lease release, or event deletion happens here.
    # BEGIN IMMEDIATE also serializes against retries by another DB connection.
    async with manager.control_lock:
        with manager.db.write() as c:
            row=c.execute('SELECT * FROM studio_jobs WHERE id=? AND owner_user_id=?',(ident,owner)).fetchone()
            if row is None:raise NotFoundError('任务不存在')
            reason=failed_nurture_protected_reason(manager,c,row)
            if reason:return {'deleted_ids':[], 'skipped':[{'job_id':ident,'reason':reason}]}
            if not row['deleted_at']:
                # Preserve status, progress, failure/action receipts and original
                # updated_at, which is also used by historical action accounting.
                c.execute("UPDATE studio_jobs SET deleted_at=? WHERE id=? AND owner_user_id=? AND deleted_at IS NULL AND kind='nurture' AND status='failed' AND inflight=0",(isoformat(),ident,owner))
        return {'deleted_ids':[ident], 'skipped':[]}

