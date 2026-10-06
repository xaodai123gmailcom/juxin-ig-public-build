"""Publishing-specific regression tests; no Instagram account is contacted."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
import test_studio as fixtures
from test_studio import Worker
from app.errors import ConflictError, ValidationError
from app.studio import config_for


class PostingWorkflowTests(unittest.TestCase):
    setUp = fixtures.StudioTests.setUp
    tearDown = fixtures.StudioTests.tearDown
    start = fixtures.StudioTests.start
    execute = fixtures.StudioTests.execute
    def test_retry_reuses_prepared_and_records_failure(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            prepared={'asset_ids':[self.asset],'caption':'ready','location':''}
            self.m.update(ident,status='failed',result_json=json.dumps({'prepared':prepared}),message='upload failed')
            await self.m.control(self.owner,ident,'retry')
            with patch.object(self.m.media,'prepare') as prepare,patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',AsyncMock(return_value={'published':1})):
                await self.execute(ident)
                prepare.assert_not_called()
            row=self.m.get(self.owner,ident)
            self.assertEqual('completed',row['status'])
            self.assertEqual('upload failed',json.loads(row['result_json'])['attempts'][0]['message'])
        asyncio.run(run())

    def test_uncertainty_requires_explicit_review_before_retry(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            self.m.update(ident,status='needs_review',inflight=1)
            with self.assertRaises(ConflictError): await self.m.control(self.owner,ident,'retry')
            await self.m.control(self.owner,ident,'confirm_not_published')
            self.assertEqual(0,self.m.get(self.owner,ident)['inflight'])
            await self.m.control(self.owner,ident,'retry')
            self.assertEqual('queued',self.m.get(self.owner,ident)['status'])
        asyncio.run(run())

    def test_manual_confirm_is_audited_and_terminal(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            self.m.update(ident,status='needs_review',inflight=1)
            await self.m.control(self.owner,ident,'confirm_published')
            row=self.m.get(self.owner,ident);result=json.loads(row['result_json'])
            self.assertEqual(('completed',1,0),(row['status'],row['cursor'],row['inflight']))
            self.assertEqual('manual',result['verification'])
            self.assertEqual('confirm_published',result['review_history'][0]['decision'])
            with self.assertRaises(ConflictError): await self.m.control(self.owner,ident,'retry')
        asyncio.run(run())

    def test_preparing_does_not_acquire_window(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            real=self.m.media.prepare
            def prepare(*args):
                with self.db.read() as c:
                    self.assertEqual(0,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
                return real(*args)
            with patch.object(self.m.media,'prepare',side_effect=prepare),patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',AsyncMock(return_value={'published':1})):
                await self.execute(ident)
        asyncio.run(run())

    def test_success_is_committed_in_one_terminal_update(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            real=self.m.update
            def update(key,**fields):
                if fields.get('cursor')==1:
                    self.assertEqual('completed',fields['status'])
                    self.assertEqual(0,fields['inflight'])
                return real(key,**fields)
            with patch.object(self.m,'update',side_effect=update),patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',AsyncMock(return_value={'published':1})):
                await self.execute(ident)
        asyncio.run(run())

    def test_lost_lease_cannot_close_another_tasks_window(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            async def publish(browser,*args):
                with self.db.write() as c:c.execute("UPDATE browser_operation_leases SET lease_token='another-token' WHERE profile_id='w1'")
                await browser.checkpoint()
                self.fail('must not continue')
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',publish):await self.execute(ident)
            self.assertEqual([],self.browser.closed)
            with self.db.read() as c:self.assertEqual('another-token',c.execute('SELECT lease_token FROM browser_operation_leases').fetchone()[0])
        asyncio.run(run())

    def test_generated_caption_is_validated_after_combining_tags(self):
        cfg=config_for('posting',{'asset_ids':[self.asset],'auto_caption':True,'hashtags':'x'*500})
        with patch.object(self.m.media,'caption',return_value='a'*1800):
            with self.assertRaises(ValidationError):self.m.media.prepare(self.owner,cfg)

    def test_pexels_skips_already_imported_sources(self):
        with self.db.write() as c:c.execute("UPDATE studio_assets SET source='pexels',source_url='https://www.pexels.com/photo/used-1/' WHERE id=?",(self.asset,))
        cfg=config_for('posting',{'source':'pexels','query':'beach','image_count':1})
        rows=[{'id':'1','url':'https://www.pexels.com/photo/used-1/'},{'id':'2','url':'https://www.pexels.com/photo/new-2/'}]
        with patch.object(self.m.media,'search',return_value={'items':rows}),patch.object(self.m.media,'import_pexels',return_value=self.asset) as download:
            self.m.media.prepare(self.owner,cfg)
            download.assert_called_once_with(self.owner,'2','photo')

    def test_ai_video_rejected_before_a_job_or_window_is_created(self):
        with self.assertRaises(ValidationError):config_for('posting',{'source':'ai','media_type':'video','query':'beach'})
        self.assertEqual(101,config_for('posting',{'concurrency':101})['concurrency'])

    def test_selected_pexels_asset_download_returns_owner_scoped_id(self):
        async def run():
            with patch.object(self.m.media,'import_pexels',return_value=self.asset) as download:
                result=await self.m.command(self.owner,{'action':'import_pexels','provider_id':'42','media_type':'photo'})
                self.assertEqual(self.asset,result['asset_id'])
                download.assert_called_once_with(self.owner,'42','photo')
        asyncio.run(run())

    def test_start_rejects_locked_window_and_replay_remains_idempotent(self):
        async def run():
            existing=await self.start()
            token=self.s.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id=existing['job_ids'][0],ttl_seconds=600)
            self.assertEqual(existing,await self.start())
            with self.assertRaises(ConflictError): await self.start(key='a-different-request')
            self.s.release_browser_lease('w1',token)
        asyncio.run(run())

    def test_confirmed_success_survives_cancel_during_done_and_refresh(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            async def publish(browser,*args):
                await browser.before_effect('正在提交')
                await browser.confirmed({'published':1,'verification':'instagram_dialog'})
                saved=self.m.get(self.owner,ident)
                self.assertEqual(('completed',0),(saved['status'],saved['inflight']))
                raise asyncio.CancelledError()
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',publish):await self.execute(ident)
            row=self.m.get(self.owner,ident)
            self.assertEqual('completed',row['status'])
            self.assertEqual(1,json.loads(row['result_json'])['published'])
            with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'retry')
        asyncio.run(run())

    def test_explicit_failure_is_noted_with_stage_and_assets_preserved(self):
        from app.studio_worker import PostingRejected
        async def run():
            ident=(await self.start())['job_ids'][0]
            async def publish(browser,*args):
                await browser.before_effect('6/6 等待分享完成')
                raise PostingRejected('分享阶段失败：分享失败')
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',publish):await self.execute(ident)
            row=self.m.get(self.owner,ident);result=json.loads(row['result_json'])
            self.assertEqual(('failed',0),(row['status'],row['inflight']))
            self.assertEqual('6/6 等待分享完成',result['failure']['stage'])
            self.assertIn('分享失败',result['failure']['message'])
            self.assertEqual([self.asset],result['prepared']['asset_ids'])
        asyncio.run(run())

    def test_refresh_problem_note_survives_window_cleanup(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            async def publish(browser,*args):
                await browser.before_effect('正在提交')
                await browser.confirmed({'published':1})
                raise RuntimeError('刷新首页失败')
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',publish):await self.execute(ident)
            row=self.m.get(self.owner,ident)
            self.assertEqual('completed',row['status']);self.assertIn('刷新首页失败',row['message'])
        asyncio.run(run())
