"""Single-photo adapter over the existing semantic Instagram composer, never a live test fixture."""
import asyncio,hashlib
from pathlib import Path
from .errors import ValidationError
from .playwright_worker import PlaywrightWorker
from .studio_worker import StudioBrowser
from .instagram_identity import OWN_LINK,OWN_METRICS,signed_in_id,wait_for_own_profile,resolve_own_identity
from .browser_cleanup import disconnect_worker
from .async_cleanup import finish_owned
class PostingExecutor:
    identity_ready_timeout_seconds=10.0
    identity_poll_seconds=.25
    def __init__(self,manager):self.manager=manager;self.worker=None;self.owned_tabs=[]
    async def _wait_for_own_profile(self,tab,name,uid,checkpoint):
        return await wait_for_own_profile(tab,name,uid,checkpoint,
            timeout=self.identity_ready_timeout_seconds,poll=self.identity_poll_seconds)
    async def publish(self,job,asset,checkpoint,before_submit,confirmed):
        path=Path(asset['path']);digest=hashlib.sha256()
        if path.is_symlink():raise ValidationError('素材路径已变化，未发布',details={'reason':'posting_asset_changed'})
        with path.open('rb') as f:
            for block in iter(lambda:f.read(65536),b''):digest.update(block)
        if not asset.get('render_sha256') or digest.hexdigest()!=asset['render_sha256']:raise ValidationError('素材文件与已审阅版本不一致，未发布',details={'reason':'posting_asset_changed'})
        self.worker=PlaywrightWorker(self.manager.bitbrowser)
        job['_posting_phase']='browser_open'
        await self.worker.connect(job['profile_id'],open_if_needed=True)
        job['_posting_phase']='composer'
        async def identity(data):
            job['_posting_phase']='identity'
            name=str(data.get('username','')).casefold()
            if not name:
                resolved=await resolve_own_identity(self.worker.page,checkpoint,
                    expected={'username':job['expected_username'],'instagram_user_id':job.get('expected_actor_id','')},
                    timeout=self.identity_ready_timeout_seconds,poll=self.identity_poll_seconds)
                name=resolved['username']
            if name!=job['expected_username'].casefold():raise ValidationError('当前登录账号与任务目标不一致，未发布',details={'reason':'posting_account_mismatch'})
            uid=await signed_in_id(self.worker.page)
            if job.get('expected_actor_id') and job['expected_actor_id']!=uid:raise ValidationError('登录账号身份已改变，停止发帖',details={'reason':'posting_account_mismatch'})
            tab=await self.worker.page.context.new_page();self.owned_tabs.append(tab)
            try:
                await tab.goto('https://www.instagram.com/'+name+'/',wait_until='domcontentloaded',timeout=30000)
                # DOMContentLoaded precedes React's own-profile/sidebar render.
                # Only missing proof may wait; changed identity fails immediately.
                await self._wait_for_own_profile(tab,name,uid,checkpoint)
            finally:
                await finish_owned(tab.close())
                self.owned_tabs.remove(tab)
            job['expected_actor_id']=uid
            with self.manager.db.write() as c:c.execute('UPDATE posting_jobs SET expected_actor_id=? WHERE id=? AND expected_actor_id IN (?,?)',(uid,job['id'],'',uid))
            job['_posting_phase']='composer'
        async def effect(message):
            await checkpoint()
            job['_posting_phase']='identity'
            name=await self.worker.page.evaluate(OWN_LINK)
            if name!=job['expected_username'].casefold() or await signed_in_id(self.worker.page)!=job.get('expected_actor_id'):raise ValidationError('分享前账号身份不一致，未发布',details={'reason':'posting_account_mismatch'})
            job['_posting_phase']='submission'
            await before_submit()
        browser=StudioBrowser(self.worker,checkpoint,effect)
        browser.account_snapshot=identity
        browser.confirmed=confirmed
        browser.progress=lambda text:checkpoint()
        # Existing publisher selects original crop, preserves caption, submits only once,
        # and calls confirmed only after an explicit in-composer success receipt.
        async with self.worker._destructive_action_lease():
            return await browser.publish([dict(asset,media_type='photo')],job['caption'],'')
    async def drain_tabs(self):
        for tab in list(self.owned_tabs):
            await finish_owned(tab.close());self.owned_tabs.remove(tab)
    async def close(self,profile,token):
        job=getattr(self,'lease_job',None)
        if not job or job['profile_id']!=profile or job['lease_token']!=token:raise ValidationError('缺少任务窗口所有权，不能关闭')
        self.manager.assert_lease(job)
        await self.disconnect()
        self.manager.assert_lease(job)
        result=await asyncio.to_thread(self.manager.bitbrowser.close_profile,profile)
        # A returned call or None is not proof. Never release on rejection/timeout.
        if not isinstance(result,dict) or result.get('closed') is not True:raise ValidationError('尚未确认窗口已关闭，保留占用等待重试')
    async def disconnect(self):
        await self.drain_tabs()
        if self.worker:await finish_owned(disconnect_worker(self.worker));self.worker=None
