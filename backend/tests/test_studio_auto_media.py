"""Automatic multi-image selection and optional AI; all providers are mocked."""
import asyncio,json,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
import test_studio as fixtures
from app.studio import config_for
from app.errors import ValidationError

class AutoMediaTests(unittest.TestCase):
    setUp=fixtures.StudioTests.setUp
    tearDown=fixtures.StudioTests.tearDown
    start=fixtures.StudioTests.start
    execute=fixtures.StudioTests.execute

    def config(self,**kw):return config_for('posting',{'source':'pexels','query':'food',**kw})
    def rows(self,n):return [{'id':str(i),'url':f'https://www.pexels.com/photo/{i}/'} for i in range(n)]
    def download(self,owner,ident,kind):
        return self.m.media.store(owner,self.png,ident,'pexels',source_url=f'https://www.pexels.com/photo/{ident}/')

    def test_default_three_and_custom_one_ten_download_to_desktop(self):
        for count in (3,1,10):
            with self.subTest(count=count),patch.object(self.m.media,'search',return_value={'items':self.rows(30)}),patch.object(self.m.media,'import_pexels',side_effect=self.download),patch.object(self.m.media,'caption') as ai:
                prepared=self.m.media.prepare(self.owner,self.config(**({} if count==3 else {'image_count':count})))
                self.assertEqual(count,len(set(prepared['asset_ids'])))
                for ident in prepared['asset_ids']:
                    self.assertTrue(Path(self.m.media.select(self.owner,ident)['desktop_path']).is_file())
                ai.assert_not_called()

    def test_across_pages_excludes_used_tombstones_and_duplicate_urls(self):
        used=self.download(self.owner,'0','photo')
        with self.db.write() as c:c.execute("UPDATE studio_assets SET path='' WHERE id=?",(used,))
        rows=self.rows(5)
        pages=[{'items':[rows[0],rows[1],rows[1]],'has_more':True},{'items':[rows[1],rows[2],rows[3]],'has_more':False}]
        with patch.object(self.m.media,'search',side_effect=pages),patch.object(self.m.media,'import_pexels',side_effect=self.download) as download:
            self.m.media.prepare(self.owner,self.config())
        self.assertEqual({'1','2','3'},{c.args[1] for c in download.call_args_list})

    def test_insufficient_does_not_download_or_fall_back_to_single(self):
        with patch.object(self.m.media,'search',return_value={'items':self.rows(1),'has_more':False}),patch.object(self.m.media,'import_pexels') as download:
            with self.assertRaisesRegex(ValidationError,'需要 3 个'):self.m.media.prepare(self.owner,self.config())
            download.assert_not_called()

    def test_manual_selection_preserved_and_video_remains_one(self):
        with patch.object(self.m.media,'generate') as generate:
            prepared=self.m.media.prepare(self.owner,self.config(source='manual',asset_ids=[self.asset],image_count=10))
            self.assertEqual([self.asset],prepared['asset_ids']);generate.assert_not_called()
        with patch.object(self.m.media,'search',return_value={'items':self.rows(12)}),patch.object(self.m.media,'import_pexels',return_value=self.asset) as download:
            self.m.media.prepare(self.owner,self.config(media_type='video',image_count=10))
            self.assertEqual(1,download.call_count);self.assertEqual('video',download.call_args.args[2])

    def test_invalid_counts_rejected(self):
        for count in (0,11,2.5):
            with self.subTest(count=count),self.assertRaises(ValidationError):self.config(image_count=count)

    def test_ai_image_model_count_caption_switch_and_location_separation(self):
        cfg=self.config(source='ai',image_count=2,image_model='gpt-image-1',auto_caption=True,caption_model='chosen-vision-model',caption_instructions='轻松简短',location='SF')
        with patch.object(self.m.media,'generate',side_effect=lambda *a:self.m.media.store(self.owner,self.png,'AI','ai')) as generate,patch.object(self.m.media,'caption',return_value='午餐 #美食') as caption:
            prepared=self.m.media.prepare(self.owner,cfg)
        self.assertEqual(2,len(set(prepared['asset_ids'])));self.assertEqual(2,generate.call_count)
        self.assertEqual('gpt-image-1',generate.call_args.args[2])
        self.assertEqual('chosen-vision-model',caption.call_args.args[0]['caption_model'])
        self.assertEqual('午餐 #美食',prepared['caption']);self.assertEqual('SF',prepared['location'])

    def test_custom_models_and_writing_preferences_reach_api_without_live_call(self):
        from PIL import Image
        import base64
        fake=Mock();fake.images.generate.return_value=SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(self.png).decode())])
        fake.responses.create.return_value=SimpleNamespace(output_text='prepared caption')
        # This offline contract mocks the entire client, so it must not depend on
        # an installed SDK merely to forward model names and writing preferences.
        sdk = SimpleNamespace(OpenAI=Mock(return_value=fake))
        with patch.dict('sys.modules', {'openai': sdk}),patch.object(self.m.media,'ai_key',return_value='fixture-only'):
            self.m.media.generate(self.owner,'food','custom-image-model')
            cfg=self.config(caption_model='custom-vision-model',caption_instructions='用英文，轻松语气')
            self.m.media.caption(cfg,[self.m.media.get(self.owner,self.asset)])
        self.assertEqual('custom-image-model',fake.images.generate.call_args.kwargs['model'])
        call=fake.responses.create.call_args.kwargs
        self.assertEqual('custom-vision-model',call['model']);self.assertFalse(call['store'])
        self.assertIn('用英文，轻松语气',call['input'][0]['content'][0]['text'])

    def test_multiple_windows_prepare_distinct_sets_before_connecting(self):
        async def run():
            cfg=self.config(auto_caption=True,image_count=3,interval_seconds=60,concurrency=1)
            with patch.object(self.m.media,'ai_key',return_value='fixture-only'):
                jobs=(await self.start(profiles=['w1','w2'],config=cfg))['job_ids']
            records=[]
            async def publish(browser,assets,caption,location):
                self.assertEqual(3,len(assets));self.assertTrue(all(Path(a['path']).is_file() for a in assets))
                records.append({a['id'] for a in assets});return {'published':1}
            with patch.object(self.m.media,'search',return_value={'items':self.rows(12)}),patch.object(self.m.media,'import_pexels',side_effect=self.download),patch.object(self.m.media,'caption',return_value='AI caption') as caption,patch('app.studio.PlaywrightWorker',fixtures.Worker),patch('app.studio.StudioBrowser.publish',publish):
                for job in jobs:await self.execute(job)
            self.assertEqual(2,caption.call_count);self.assertFalse(records[0]&records[1])
            self.assertEqual(['w1','w2'],self.browser.closed)
            for job in jobs:self.assertEqual('completed',self.m.get(self.owner,job)['status'])
        asyncio.run(run())
