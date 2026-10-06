"""Best-effort labels for observing existing task pages; never navigate or select."""
import asyncio

async def label_task_page(worker,role,*,viewport_mode=None,cdp_session=None):
    worker._task_page_role=role
    native=getattr(getattr(worker,'bitbrowser',None),'native',None);bridge=getattr(native,'bridge',None)
    cdp=cdp_session if cdp_session is not None else getattr(worker,'_cdp_session',None)
    if not bridge or not cdp or not str(getattr(worker,'profile_id',None) or '').startswith('native:'):return
    try:
        target=(await cdp.send('Target.getTargetInfo'))['targetInfo']['targetId']
        slot=getattr(worker,'_task_page_slot',None)
        options={'slot':slot} if role=='screening' and slot in (1,2,3) else {}
        if viewport_mode is not None:options['viewport_mode']=viewport_mode
        await asyncio.to_thread(bridge.call,'label-task-page',profile=worker.profile_id,target=target,role=role,**options)
    except Exception:
        pass
