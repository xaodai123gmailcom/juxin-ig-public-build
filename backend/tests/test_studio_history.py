"""Deletion cannot erase uncertain receipts or race the owning window cleanup."""
import asyncio,json,unittest
from unittest.mock import AsyncMock,patch
import test_studio as fixtures
from app.errors import NotFoundError

class HistoryTests(unittest.TestCase):
    setUp=fixtures.StudioTests.setUp
    tearDown=fixtures.StudioTests.tearDown
    start=fixtures.StudioTests.start
    execute=fixtures.StudioTests.execute

    def test_bulk_delete_preserves_confirmed_uncertain_other_owner_and_idempotency(self):
        async def run():
            ids={}
            for status in ('queued','paused','failed','cancelled','completed','needs_review'):
                key=(await self.start(key='history-request-'+status))['job_ids'][0];ids[status]=key
                self.m.update(key,status=status,inflight=int(status=='needs_review'),result_json=json.dumps({'published':1} if status=='completed' else {}))
            with self.assertRaises(NotFoundError):await self.m.command(self.other,{'action':'delete_unfinished','job_id':ids['failed']})
            result=await self.m.command(self.owner,{'action':'delete_unfinished'})
            self.assertEqual({ids[x] for x in ('queued','paused','failed','cancelled')},set(result['deleted_ids']))
            self.assertEqual(ids['needs_review'],result['skipped'][0]['job_id'])
            self.assertEqual({ids['completed'],ids['needs_review']},{j['id'] for j in self.m.snapshot(self.owner)['jobs']})
            self.assertEqual(1,self.m.snapshot(self.owner)['window_stats'][0]['published_count'])
            with self.assertRaises(NotFoundError):await self.m.control(self.owner,ids['failed'],'retry')
            self.m.recover();self.assertEqual(2,len(self.m.snapshot(self.owner)['jobs']))
            replay=await self.start(key='history-request-queued')
            self.assertEqual([ids['queued']],replay['job_ids']);self.assertEqual(2,len(self.m.snapshot(self.owner)['jobs']))
            self.assertEqual([], (await self.m.command(self.owner,{'action':'delete_unfinished'}))['deleted_ids'])
        asyncio.run(run())

    def test_running_deletion_waits_for_owned_cleanup_and_leaves_other_lease(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            entered=asyncio.Event();cleaning=asyncio.Event();release=asyncio.Event()
            async def publish(*_):entered.set();await asyncio.Event().wait()
            async def disconnect():cleaning.set();await release.wait()
            class Worker(fixtures.Worker):
                def __init__(self,*args):super().__init__(*args);self.disconnect=disconnect
            lease=self.s.acquire_browser_lease(self.owner,'other-window',operation_type='studio',entity_id='other-task',ttl_seconds=600)
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',publish):
                task=asyncio.create_task(self.execute(ident));self.m.tasks[ident]=task
                await asyncio.wait_for(entered.wait(),2)
                deletion=asyncio.create_task(self.m.command(self.owner,{'action':'delete_unfinished','job_id':ident}))
                await asyncio.wait_for(cleaning.wait(),2)
                self.assertFalse(deletion.done());self.assertEqual(1,len(self.m.snapshot(self.owner)['jobs']))
                with self.db.read() as c:self.assertEqual(2,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
                release.set();result=await asyncio.wait_for(deletion,3)
                self.assertEqual([ident],result['deleted_ids']);self.assertEqual([],self.m.snapshot(self.owner)['jobs'])
                self.assertEqual([],self.browser.closed)
                with self.db.read() as c:self.assertEqual(lease,c.execute('SELECT lease_token FROM browser_operation_leases').fetchone()[0])
        asyncio.run(run())

    def test_submitted_job_is_not_cancelled_and_completed_draft_is_not_removed(self):
        async def run():
            ident=(await self.start())['job_ids'][0];self.m.update(ident,status='running',inflight=1)
            with patch.object(self.m,'control',new=AsyncMock()) as control:
                result=await self.m.command(self.owner,{'action':'delete_unfinished','job_id':ident})
                self.assertEqual([],result['deleted_ids']);self.assertIn('核验',result['skipped'][0]['reason']);control.assert_not_awaited()
            draft=(await self.start(kind='material',key='saved-draft-request'))['job_ids'][0]
            await self.execute(draft)
            result=await self.m.command(self.owner,{'action':'delete_unfinished','job_id':draft})
            self.assertEqual([],result['deleted_ids']);self.assertEqual('completed',self.m.get(self.owner,draft)['status'])
        asyncio.run(run())

    def test_multiple_drafts_survive_reload_and_do_not_open_a_window(self):
        async def run():
            for n in range(2):
                ident=(await self.start(kind='material',key=f'multiple-drafts-{n}',config={'asset_ids':[self.asset],'caption':f'draft {n}'}))['job_ids'][0]
                await self.execute(ident)
            self.db.initialize()
            jobs=self.m.snapshot(self.owner)['jobs']
            self.assertEqual({'draft 0','draft 1'},{j['result']['prepared']['caption'] for j in jobs})
            self.assertTrue(all(j['status']=='completed' for j in jobs));self.assertEqual([],self.browser.closed)
            with self.db.read() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
        asyncio.run(run())

    def test_delete_waits_for_download_thread_before_removing_record(self):
        import threading
        async def run():
            ident=(await self.start(kind='material'))['job_ids'][0]
            entered=asyncio.Event();release=threading.Event();loop=asyncio.get_running_loop()
            original=self.m.media.prepare
            def prepare(*args):
                loop.call_soon_threadsafe(entered.set)
                release.wait(timeout=3)
                return original(*args)
            with patch.object(self.m.media,'prepare',side_effect=prepare):
                task=asyncio.create_task(self.execute(ident));self.m.tasks[ident]=task
                await asyncio.wait_for(entered.wait(),1)
                deletion=asyncio.create_task(self.m.command(self.owner,{'action':'delete_unfinished','job_id':ident}))
                await asyncio.sleep(.05)
                self.assertFalse(deletion.done());self.assertIn(ident,self.m.active_ids())
                self.assertEqual(1,len(self.m.snapshot(self.owner)['jobs']))
                release.set()
                result=await asyncio.wait_for(deletion,3)
                self.assertEqual([ident],result['deleted_ids']);self.assertEqual([],self.m.snapshot(self.owner)['jobs'])
        asyncio.run(run())
