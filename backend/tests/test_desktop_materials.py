"""Desktop selection, pre-start readiness, retries and real file IO."""
import asyncio
import base64
import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import test_studio as fixtures
from test_studio import Worker
from app.errors import NotFoundError, ValidationError
from app.studio_files import StudioFiles, desktop_root


class DesktopMaterialTests(unittest.TestCase):
    setUp=fixtures.StudioTests.setUp
    tearDown=fixtures.StudioTests.tearDown
    start=fixtures.StudioTests.start
    execute=fixtures.StudioTests.execute

    def test_upload_is_on_desktop_before_start_and_keeps_original(self):
        async def run():
            result=await self.m.command(self.owner,{'action':'upload','name':'本地照片.png','data':base64.b64encode(self.png).decode()})
            original=Path(self.m.media.get(self.owner,result['asset_id'])['path'])
            selected=Path(result['desktop_path'])
            self.assertTrue(selected.is_relative_to(self.m.media.files.root))
            self.assertEqual(original.read_bytes(),selected.read_bytes())
            self.assertEqual('.jpg',selected.suffix)
            self.assertEqual([],self.m.snapshot(self.owner)['jobs'])
            self.assertEqual([],self.browser.closed)
        asyncio.run(run())

    def test_select_repairs_deleted_desktop_copy_without_download(self):
        async def run():
            first=await self.m.command(self.owner,{'action':'select_asset','asset_id':self.asset})
            selected=Path(first['desktop_path']);selected.unlink()
            with patch.object(self.m.media,'import_pexels') as download:
                again=await self.m.command(self.owner,{'action':'select_asset','asset_id':self.asset})
                self.assertEqual(first,again);self.assertTrue(selected.is_file());download.assert_not_called()
            with self.assertRaises(NotFoundError):
                await self.m.command(self.other,{'action':'select_asset','asset_id':self.asset})
        asyncio.run(run())

    def test_failed_desktop_save_does_not_report_import_success(self):
        with self.db.read() as c:before=c.execute('SELECT count(*) FROM studio_assets').fetchone()[0]
        with patch.object(self.m.media.files,'select',side_effect=ValidationError('desktop full')):
            with self.assertRaises(ValidationError):self.m.media.store(self.owner,self.png,'new','manual')
        with self.db.read() as c:self.assertEqual(before,c.execute('SELECT count(*) FROM studio_assets').fetchone()[0])
        self.assertEqual(1,len(list((self.m.media.root/self.owner).glob('*.jpg'))))

    def test_future_posting_schedule_does_not_delay_draft_download(self):
        from datetime import datetime,timezone
        async def run():
            ident=(await self.start('material',config={'asset_ids':[self.asset],'scheduled_at':'2099-01-01T00:00:00Z'}))['job_ids'][0]
            row=self.m.get(self.owner,ident)
            self.assertLess(datetime.fromisoformat(row['due_at']),datetime.now(timezone.utc))
        asyncio.run(run())

    def test_material_draft_downloads_but_never_connects_or_publishes(self):
        async def run():
            ident=(await self.start('material'))['job_ids'][0]
            with patch('app.studio.PlaywrightWorker') as worker:
                await self.execute(ident);worker.assert_not_called()
            row=self.m.get(self.owner,ident);prepared=json.loads(row['result_json'])['prepared']
            self.assertEqual('completed',row['status'])
            self.assertIn('素材备稿',prepared['folder'])
            self.assertTrue(Path(prepared['files'][0]['path']).is_file())
            with self.db.read() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
        asyncio.run(run())

    def test_publish_uses_task_desktop_files_and_retry_repairs_same_files(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            paths=[]
            async def publish(assets,*_):
                path=Path(assets[0]['path']);paths.append(path)
                self.assertTrue(path.is_relative_to(self.m.media.files.root))
                self.assertIn('发帖任务',str(path))
                self.assertEqual(Path(self.m.media.get(self.owner,self.asset)['path']).read_bytes(),path.read_bytes())
                if len(paths)==1:raise ValidationError('before submit failure')
                return {'published':1}
            real=self.m.media.files.prepare
            def prepare(*args):
                with self.db.read() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
                return real(*args)
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',AsyncMock(side_effect=publish)),patch.object(self.m.media.files,'prepare',side_effect=prepare):
                await self.execute(ident)
                self.assertEqual('failed',self.m.get(self.owner,ident)['status'])
                paths[0].unlink()
                await self.m.control(self.owner,ident,'retry')
                with patch.object(self.m.media,'prepare') as generate:
                    await self.execute(ident);generate.assert_not_called()
            self.assertEqual([paths[0],paths[0]],paths)
            self.assertEqual('completed',self.m.get(self.owner,ident)['status'])
        asyncio.run(run())

    def test_windows_names_and_accounts_cannot_collide_or_escape_folder(self):
        asset=self.m.media.get(self.owner,self.asset);asset['name']='../CON:?*.png'
        files=self.m.media.files
        first,_=files.prepare(self.owner,'../../job','account/one','../CON',[asset])
        second,_=files.prepare(self.owner,'../../job','account\\one','../CON',[asset])
        self.assertNotEqual(first['folder'],second['folder'])
        for result in (first,second):
            path=Path(result['files'][0]['path'])
            self.assertTrue(path.resolve().is_relative_to(files.root.resolve()))
            for part in path.relative_to(files.root).parts:self.assertFalse(any(c in part for c in '<>:"\\|?*'))

    def test_redirected_desktop_path_is_used(self):
        redirected=Path(self.tmp.name)/'OneDrive'/'桌面'
        with patch.dict(os.environ,{'IGAC_DESKTOP_DIR':str(redirected)}):
            self.assertEqual(redirected/'聚鑫国际素材',desktop_root())
            self.assertEqual(desktop_root(),StudioFiles().root)

    def test_more_search_uses_provider_page_and_end_does_not_wrap(self):
        def response(path):
            from urllib.parse import urlparse,parse_qs
            page=int(parse_qs(urlparse(path).query)['page'][0])
            item={'id':page,'src':{'medium':'https://images.pexels.com/test.jpg'},'url':f'https://www.pexels.com/{page}'}
            return {'photos':[item],'total_results':13,**({'next_page':'https://api.pexels.com/v1/search?page=2'} if page==1 else {})}
        with patch.object(self.m.media,'_json',side_effect=response):
            first=self.m.media.search('beach',page=1);second=self.m.media.search('beach',page=2)
            self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
            self.assertTrue(first['has_more']);self.assertFalse(second['has_more'])
            for invalid in (0,-1,1001,'bad',None):
                with self.assertRaises(ValidationError):self.m.media.search('beach',page=invalid)
