"""Saved draft assignment, repeat protection and atomic multi-window planning."""
import asyncio,io,json,unittest
from PIL import Image
import test_studio as fixtures
from app.errors import ConflictError,NotFoundError,ValidationError

class DraftTests(unittest.TestCase):
    setUp=fixtures.StudioTests.setUp
    tearDown=fixtures.StudioTests.tearDown
    start=fixtures.StudioTests.start
    execute=fixtures.StudioTests.execute

    async def draft(self,n,asset=None):
        if not asset:
            b=io.BytesIO();Image.new('RGB',(30,30),(n*70,20,30)).save(b,format='PNG')
            asset=self.m.media.store(self.owner,b.getvalue(),f'media {n}','manual')
        ident=(await self.start('material',key=f'draft-number-{n}',config={'asset_ids':[asset],'caption':f'caption {n}','location':f'place {n}'}))['job_ids'][0]
        await self.execute(ident)
        return ident

    def test_targets_persist_and_two_drafts_publish_to_distinct_windows_once(self):
        async def run():
            drafts=[await self.draft(n) for n in (1,2)]
            pairs=[{'draft_id':d,'profile_id':f'w{i}'} for i,d in enumerate(drafts)]
            await self.m.command(self.owner,{'action':'set_draft_targets','assignments':pairs})
            self.db.initialize()
            self.assertEqual('w0',self.m.get(self.owner,drafts[0])['draft_target_profile_id'])
            body={'action':'start_drafts','assignments':pairs,'request_id':'draft-publish-request','interval_seconds':7}
            first=await self.m.command(self.owner,body)
            self.assertEqual(first,await self.m.command(self.owner,body))
            jobs=[self.m.get(self.owner,x) for x in first['job_ids']]
            self.assertEqual(['w0','w1'],[j['profile_id'] for j in jobs])
            self.assertEqual(['caption 1','caption 2'],[json.loads(j['result_json'])['prepared']['caption'] for j in jobs])
            self.assertEqual(set(drafts),set(self.m.snapshot(self.owner)['assigned_draft_ids']))
            with self.assertRaises(ConflictError):await self.m.command(self.owner,{**body,'request_id':'another-draft-request'})
            await self.m.command(self.owner,{'action':'delete_unfinished','job_id':jobs[0]['id']})
            with self.assertRaises(ConflictError):await self.m.command(self.owner,{**body,'request_id':'deleted-repeat-request'})
            self.assertEqual([],self.browser.closed)
        asyncio.run(run())

    def test_repeat_content_or_window_and_busy_window_roll_back_entire_batch(self):
        async def run():
            # Distinct asset IDs containing the same image must still be rejected.
            copy=self.m.media.store(self.owner,self.png,'another-name','manual')
            drafts=[await self.draft(1,self.asset),await self.draft(2,copy)]
            pairs=[{'draft_id':d,'profile_id':f'w{i}'} for i,d in enumerate(drafts)]
            await self.m.command(self.owner,{'action':'set_draft_targets','assignments':pairs})
            body={'action':'start_drafts','assignments':pairs,'request_id':'duplicate-content-request'}
            with self.assertRaisesRegex(ConflictError,'重复素材'):await self.m.command(self.owner,body)
            self.assertEqual([],self.m.snapshot(self.owner)['assigned_draft_ids'])
            with self.assertRaisesRegex(ValidationError,'每个窗口'):
                await self.m.command(self.owner,{**body,'assignments':[{**p,'profile_id':'w0'} for p in pairs]})
            self.s.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id='other',ttl_seconds=600)
            with self.assertRaisesRegex(ConflictError,'占用'):await self.m.command(self.owner,body)
            self.assertEqual([],self.m.snapshot(self.owner)['assigned_draft_ids'])
        asyncio.run(run())

    def test_missing_media_other_owner_and_changed_target_do_not_enqueue(self):
        async def run():
            draft=await self.draft(1)
            body={'action':'start_drafts','assignments':[{'draft_id':draft,'profile_id':'w1'}],'request_id':'draft-safety-request'}
            with self.assertRaises(NotFoundError):await self.m.command(self.other,body)
            with self.assertRaisesRegex(ConflictError,'指定窗口'):await self.m.command(self.owner,body)
            await self.m.command(self.owner,{'action':'set_draft_targets','assignments':body['assignments']})
            asset=json.loads(self.m.get(self.owner,draft)['result_json'])['prepared']['asset_ids'][0]
            await self.m.command(self.owner,{'action':'delete_asset','asset_id':asset})
            with self.assertRaisesRegex(ValidationError,'素材'):await self.m.command(self.owner,body)
            self.assertEqual([],self.m.snapshot(self.owner)['assigned_draft_ids'])
        asyncio.run(run())
