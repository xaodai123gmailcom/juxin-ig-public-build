"""Local files really disappear, while task fences and histories survive."""
import asyncio,json,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
import test_studio as fixtures
from test_studio import Worker
from app.errors import ConflictError,NotFoundError,ValidationError

class StudioCleanupTests(unittest.TestCase):
    setUp=fixtures.StudioTests.setUp
    tearDown=fixtures.StudioTests.tearDown
    start=fixtures.StudioTests.start
    execute=fixtures.StudioTests.execute

    async def published(self):
        ident=(await self.start())['job_ids'][0]
        with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',AsyncMock(return_value={'published':1,'verification':'instagram_dialog'})):
            await self.execute(ident)
        return ident

    def test_clear_success_removes_original_selection_and_task_files_but_keeps_history(self):
        async def run():
            ident=await self.published();before=self.m.get(self.owner,ident);result=json.loads(before['result_json'])
            original=Path(self.m.media.get(self.owner,self.asset)['path'])
            selected=Path(self.m.media.select(self.owner,self.asset)['desktop_path'])
            uploaded=Path(result['prepared']['files'][0]['path'])
            cleared=await self.m.command(self.owner,{'action':'clear_published_media'})
            self.assertEqual([self.asset],cleared['deleted_ids']);self.assertEqual(3,cleared['deleted_files'])
            self.assertFalse(any(p.exists() for p in (original,selected,uploaded)))
            self.assertFalse(uploaded.parent.exists())
            after=self.m.get(self.owner,ident)
            self.assertEqual(('completed',1),(after['status'],json.loads(after['result_json'])['published']))
            self.assertEqual(before['due_at'],after['due_at'])
            self.assertEqual([],self.m.snapshot(self.owner)['assets'])
            with self.assertRaises(ValidationError):self.m.media.get(self.owner,self.asset)
        asyncio.run(run())

    def test_other_queued_failed_and_uncertain_tasks_protect_reused_media(self):
        async def run():
            await self.published()
            other=(await self.start(key='other-task-request'))['job_ids'][0]
            for state in ['queued','running','paused','waiting_window','failed','needs_review']:
                self.m.update(other,status=state)
                result=await self.m.command(self.owner,{'action':'clear_published_media'})
                self.assertEqual([],result['deleted_ids']);self.assertEqual(1,len(result['skipped']))
                self.assertTrue(Path(self.m.media.get(self.owner,self.asset)['path']).is_file())
            await self.m.control(self.owner,other,'confirm_not_published')
            await self.m.control(self.owner,other,'retry')
            await self.m.control(self.owner,other,'cancel')
            self.assertEqual([self.asset],(await self.m.command(self.owner,{'action':'clear_published_media'}))['deleted_ids'])
        asyncio.run(run())

    def test_cleanup_removes_empty_task_directory_through_path_alias(self):
        alias=Path(self.tmp.name)/'Desktop-alias'
        try:alias.symlink_to(self.m.media.files.root,target_is_directory=True)
        except OSError:self.skipTest('Filesystem does not permit a directory symlink')
        self.m.media.files.root=alias
        self.test_clear_success_removes_original_selection_and_task_files_but_keeps_history()

    def test_task_still_releasing_window_protects_media(self):
        async def run():
            ident=await self.published()
            token=self.s.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id=ident,ttl_seconds=600)
            result=await self.m.command(self.owner,{'action':'clear_published_media'})
            self.assertEqual([],result['deleted_ids'])
            self.s.release_browser_lease('w1',token)
        asyncio.run(run())

    def test_pending_or_uncertain_job_cannot_be_cleared_as_published(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            for state in ('queued','failed','needs_review'):
                self.m.update(ident,status=state)
                with self.assertRaises(ConflictError):await self.m.command(self.owner,{'action':'clear_published_media','job_id':ident})
        asyncio.run(run())

    def test_ownership_and_new_task_after_delete_are_enforced(self):
        async def run():
            with self.assertRaises(NotFoundError):await self.m.command(self.other,{'action':'delete_asset','asset_id':self.asset})
            await self.m.command(self.owner,{'action':'delete_asset','asset_id':self.asset})
            with self.assertRaises(ValidationError):await self.start()
            self.assertEqual([],self.m.snapshot(self.owner)['jobs'])
        asyncio.run(run())

    def test_old_manual_selection_in_auto_template_is_not_treated_as_published(self):
        async def run():
            ident=await self.published()
            unused=self.m.media.store(self.owner,self.png,'未发布素材','manual')
            row=self.m.get(self.owner,ident);config=json.loads(row['config_json'])
            config.update(source='pexels',asset_ids=[unused])
            self.m.update(ident,config_json=json.dumps(config))
            result=await self.m.command(self.owner,{'action':'clear_published_media'})
            self.assertEqual([self.asset],result['deleted_ids'])
            self.assertTrue(Path(self.m.media.get(self.owner,unused)['path']).is_file())
        asyncio.run(run())

    def test_external_file_from_forged_task_metadata_is_not_deleted(self):
        async def run():
            ident=await self.published();outside=Path(self.tmp.name)/'personal.txt';outside.write_text('keep')
            row=self.m.get(self.owner,ident);result=json.loads(row['result_json'])
            result['prepared']['files'].append({'asset_id':self.asset,'path':str(outside)})
            self.m.update(ident,result_json=json.dumps(result))
            await self.m.command(self.owner,{'action':'clear_published_media'})
            self.assertEqual('keep',outside.read_text())
        asyncio.run(run())

    def test_pexels_tombstone_retains_source_url_for_automatic_deduplication(self):
        async def run():
            with self.db.write() as c:c.execute("UPDATE studio_assets SET source='pexels',source_url='https://www.pexels.com/photo/42/' WHERE id=?",(self.asset,))
            await self.m.command(self.owner,{'action':'delete_asset','asset_id':self.asset})
            with self.db.read() as c:
                row=c.execute('SELECT source_url,path FROM studio_assets WHERE id=?',(self.asset,)).fetchone()
                self.assertEqual('https://www.pexels.com/photo/42/',row['source_url']);self.assertEqual('',row['path'])
        asyncio.run(run())

    def test_shared_cloud_original_is_kept_until_last_asset_is_deleted(self):
        async def run():
            other=self.m.media.store(self.owner,self.png,'copy','manual')
            cloud=self.db.path.parent/'cloud-media'/self.owner/'hash.jpg';cloud.parent.mkdir(parents=True);cloud.write_bytes(self.png)
            with self.db.write() as c:c.execute('UPDATE studio_assets SET path=? WHERE owner_user_id=?',(str(cloud),self.owner))
            await self.m.command(self.owner,{'action':'delete_asset','asset_id':self.asset})
            self.assertTrue(cloud.exists())
            await self.m.command(self.owner,{'action':'delete_asset','asset_id':other})
            self.assertFalse(cloud.exists())
        asyncio.run(run())

    def test_busy_file_keeps_original_and_reports_skipped_for_retry(self):
        async def run():
            await self.published();original=Path(self.m.media.get(self.owner,self.asset)['path']);real=Path.unlink
            def unlink(path,*a,**kw):
                if path.is_relative_to(self.m.media.files.root):raise PermissionError('file in use')
                return real(path,*a,**kw)
            with patch.object(Path,'unlink',unlink):result=await self.m.command(self.owner,{'action':'clear_published_media'})
            self.assertEqual([],result['deleted_ids']);self.assertEqual(1,len(result['skipped']))
            self.assertTrue(original.exists())
            self.assertEqual([self.asset],(await self.m.command(self.owner,{'action':'clear_published_media'}))['deleted_ids'])
        asyncio.run(run())

    def test_manual_delete_releases_old_failed_material_and_disables_retry(self):
        async def run():
            ident=await self.published();row=self.m.get(self.owner,ident);result=json.loads(row['result_json'])
            result.pop('published',None)
            self.m.update(ident,status='failed',result_json=json.dumps(result),message='旧任务未找到发帖入口')
            original=Path(self.m.media.get(self.owner,self.asset)['path'])
            selected=Path(self.m.media.select(self.owner,self.asset)['desktop_path'])
            staged=Path(result['prepared']['files'][0]['path'])
            deleted=await self.m.command(self.owner,{'action':'delete_asset','asset_id':self.asset})
            self.assertEqual([self.asset],deleted['deleted_ids'])
            self.assertFalse(any(p.exists() for p in (original,selected,staged)))
            after=self.m.get(self.owner,ident)
            self.assertEqual('failed',after['status']);self.assertEqual(row['created_at'],after['created_at'])
            self.assertEqual('旧任务未找到发帖入口',after['message'])
            self.assertIn(self.asset,json.loads(after['result_json'])['cleaned_asset_ids'])
            with self.assertRaisesRegex(ValidationError,'原素材已删除'):await self.m.control(self.owner,ident,'retry')
            self.assertEqual('failed',self.m.get(self.owner,ident)['status'])
        asyncio.run(run())

    def test_manual_delete_lists_real_blocking_jobs_and_preserves_files(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            original=Path(self.m.media.get(self.owner,self.asset)['path'])
            for status,inflight in [('queued',0),('paused',0),('running',0),('waiting_window',0),('needs_review',1),('failed',1)]:
                self.m.update(ident,status=status,inflight=inflight)
                result=await self.m.command(self.owner,{'action':'delete_asset','asset_id':self.asset})
                self.assertEqual([],result['deleted_ids']);self.assertTrue(original.is_file())
                blocker=result['skipped'][0]['blockers'][0]
                self.assertEqual(ident,blocker['job_id']);self.assertEqual('w1',blocker['window_name'])
                self.assertEqual(status,blocker['status'])
                if inflight:self.assertIn('核验',blocker['reason'])
        asyncio.run(run())

    def test_failed_task_with_window_lease_still_blocks_manual_delete(self):
        async def run():
            ident=(await self.start())['job_ids'][0];self.m.update(ident,status='failed')
            token=self.s.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id=ident,ttl_seconds=600)
            result=await self.m.command(self.owner,{'action':'delete_asset','asset_id':self.asset})
            self.assertEqual([],result['deleted_ids']);self.assertIn('释放窗口',result['skipped'][0]['reason'])
            with self.db.read() as c:self.assertEqual(token,c.execute("SELECT lease_token FROM browser_operation_leases WHERE profile_id='w1'").fetchone()[0])
            self.s.release_browser_lease('w1',token)
            self.assertEqual([self.asset],(await self.m.command(self.owner,{'action':'delete_asset','asset_id':self.asset}))['deleted_ids'])
        asyncio.run(run())

    def test_cancelled_but_active_preparation_still_protects_material(self):
        async def run():
            ident=(await self.start())['job_ids'][0];self.m.update(ident,status='cancelled')
            release=asyncio.Event();task=asyncio.create_task(release.wait());self.m.tasks[ident]=task
            try:
                result=await self.m.command(self.owner,{'action':'delete_asset','asset_id':self.asset})
                self.assertEqual([],result['deleted_ids']);self.assertIn('正在执行',result['skipped'][0]['reason'])
            finally:release.set();await task;self.m.tasks.pop(ident)
            self.assertEqual([self.asset],(await self.m.command(self.owner,{'action':'delete_asset','asset_id':self.asset}))['deleted_ids'])
        asyncio.run(run())

    def test_repeated_delete_acknowledges_already_cleaned_card(self):
        async def run():
            await self.m.command(self.owner,{'action':'delete_asset','asset_id':self.asset})
            result=await self.m.command(self.owner,{'action':'delete_asset','asset_id':self.asset})
            self.assertEqual([self.asset],result['deleted_ids']);self.assertEqual(0,result['deleted_files'])
            self.assertEqual([],self.m.snapshot(self.owner)['assets'])
        asyncio.run(run())

    def test_manual_delete_busy_file_reports_retryable_error_and_preserves_asset(self):
        async def run():
            await self.published();original=Path(self.m.media.get(self.owner,self.asset)['path']);real=Path.unlink
            def unlink(path,*args,**kwargs):
                if path.is_relative_to(self.m.media.files.root):raise PermissionError('fixture file lock')
                return real(path,*args,**kwargs)
            with patch.object(Path,'unlink',unlink):
                with self.assertRaisesRegex(ValidationError,'占用'):await self.m.command(self.owner,{'action':'delete_asset','asset_id':self.asset})
            self.assertTrue(original.exists())
            self.assertEqual([self.asset],(await self.m.command(self.owner,{'action':'delete_asset','asset_id':self.asset}))['deleted_ids'])
        asyncio.run(run())
