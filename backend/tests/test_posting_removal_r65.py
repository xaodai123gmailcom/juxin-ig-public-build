"""Retired routes, nonposting state and verified old-cloud-content archival."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch
import zlib

from app.cloud_workspace import decode_workspace, export_workspace, import_workspace
from app.config import Settings
from app.database import Database
from app.errors import ValidationError, ConflictError
from app.service import CoreService, isoformat
from app.studio import StudioManager
from app.work_reports import work_report


def packed(data):
    raw=json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    return {'format':1,'encoding':'zlib+base64','sha256':hashlib.sha256(raw).hexdigest(),
            'data':base64.b64encode(zlib.compress(raw)).decode()}


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.db=Database(self.root/'source.sqlite3');self.db.initialize()
        self.service=CoreService(self.db);self.owner=self.service.register_user('removal-owner','fixture-password-123')['id']
        self.manager=StudioManager(self.service,object())

    def seed_legacy(self):
        now=isoformat()
        media=self.root/'old-material.jpg';media.write_bytes(b'exact historical material')
        with self.db.write() as c:
            for kind in ('posting','material'):
                c.execute('''INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,status,
                    config_json,due_at,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?,?)''',
                    (kind,self.owner,kind,kind,'old-'+kind,json.dumps({'caption':'exact archived caption','asset_ids':['old-asset']}),now,now,now))
                c.execute('INSERT INTO studio_templates VALUES(?,?,?,?)',(self.owner,kind,'{"caption":"legacy template"}',now))
            c.execute('INSERT INTO studio_assets VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('old-asset',self.owner,'manual','old material',str(media),'photo','','','',now))
        return media

    def test_snapshot_reports_and_commands_only_expose_nurture(self):
        self.seed_legacy()
        async def exercise():
            result=await self.manager.command(self.owner,{'action':'start','kind':'nurture','request_id':'nurture-remains-available','profile_ids':['free-profile'],'config':{'minutes':1}})
            self.assertEqual(1,len(result['job_ids']))
            for action in ('generate','start_drafts','set_draft_targets','search','select_asset','import_pexels','upload','delete_asset','clear_published_media','delete_unfinished'):
                with self.subTest(action=action),self.assertRaises(ValidationError):
                    await self.manager.command(self.owner,{'action':action,'kind':'posting','job_id':'posting'})
            for operation in ('resume','retry','cancel','confirm_published','confirm_not_published'):
                with self.assertRaises(ValidationError):await self.manager.control(self.owner,'posting',operation)
        asyncio.run(exercise())
        data=self.manager.snapshot(self.owner)
        self.assertEqual({'nurture'},{row['kind'] for row in data['jobs']})
        self.assertFalse({'assets','credentials','assigned_draft_ids'} & data.keys())
        self.assertNotIn('exact archived caption',json.dumps(data))
        for summary in (False,True):
            report=work_report(self.db,self.owner,'2026-01-01T00:00:00Z','2028-01-01T00:00:00Z',summary_only=summary)
            self.assertNotIn('posting',report['totals']);self.assertNotIn('confirmed_posting',report['totals'])

    def test_scheduler_cannot_execute_legacy_queue(self):
        self.seed_legacy()
        async def exercise():
            with patch.object(self.manager,'_execute') as execute:
                self.manager._schedule_ready();await asyncio.sleep(0);execute.assert_not_called()
        asyncio.run(exercise())

    def test_removed_api_routes_and_config_are_absent(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        settings=Settings(startup_token='removal-offline-startup-token-12345',database_path=self.db.path,data_dir=self.root)
        app=create_app(settings,database=self.db,bitbrowser=object())
        client=TestClient(app);self.addCleanup(client.close)
        token=self.service.login('removal-owner','fixture-password-123')['token']
        headers={'X-Startup-Token':settings.startup_token,'Authorization':'Bearer '+token}
        for method,path in [('get','/api/posting/snapshot'),('post','/api/posting/command'),('post','/api/internal/integrations/pexels')]:
            self.assertEqual(404,getattr(client,method)(path,headers=headers).status_code,path)
        self.assertFalse(hasattr(app.state,'posting'))
        self.assertFalse(hasattr(settings,'pexels_api_key'))

    def test_deprecated_provider_secret_is_discarded_before_browser_children(self):
        import os
        from app.main import create_app
        settings=Settings(startup_token='removal-offline-startup-token-12345',database_path=self.db.path,data_dir=self.root)
        with patch.dict(os.environ,{'IGAC_PEXELS_API_KEY':'test-only-obsolete-secret','PEXELS_API_KEY':'test-only-obsolete-fallback'}):
            create_app(settings,database=self.db,bitbrowser=object())
            self.assertNotIn('IGAC_PEXELS_API_KEY',os.environ)
            self.assertNotIn('PEXELS_API_KEY',os.environ)

    def test_cloud_export_excludes_all_retired_rows_and_files(self):
        media=self.seed_legacy()
        payload,_=export_workspace(self.db,self.owner,self.root)
        data=decode_workspace(payload)
        self.assertEqual([],data['tables']['studio_jobs'])
        self.assertEqual([],data['tables']['studio_templates'])
        self.assertEqual([],data['tables']['studio_assets'])
        self.assertEqual({},data['assets'])
        self.assertEqual(b'exact historical material',media.read_bytes())
        with self.db.read() as c:self.assertEqual(2,c.execute('SELECT COUNT(*) FROM studio_jobs').fetchone()[0])

    def legacy_cloud_payload(self):
        media=self.seed_legacy()
        payload,_=export_workspace(self.db,self.owner,self.root)
        data=decode_workspace(payload)
        with self.db.read() as c:
            for table in ('studio_jobs','studio_templates','studio_assets'):
                data['tables'][table]=[dict(r) for r in c.execute('SELECT * FROM '+table)]
        for row in data['tables']['studio_assets']:row['path']=''
        data['assets']={'old-asset':base64.b64encode(media.read_bytes()).decode()}
        return packed(data)

    def destination(self):
        db=Database(self.root/'destination.sqlite3');db.initialize()
        owner=CoreService(db).register_user('destination-owner','fixture-password-123')['id']
        return db,owner

    def test_old_cloud_snapshot_archives_verbatim_then_restores_nonposting_only(self):
        payload=self.legacy_cloud_payload();db,owner=self.destination()
        import_workspace(db,owner,self.root/'destination',payload)
        archives=list(Path(str(db.path)+'.posting-retirement/cloud-snapshots').glob('*.json'))
        self.assertEqual(1,len(archives))
        recovered=json.loads(archives[0].read_bytes())
        self.assertEqual(payload,recovered)
        old=decode_workspace(recovered)
        self.assertEqual(b'exact historical material',base64.b64decode(old['assets']['old-asset']))
        self.assertEqual('exact archived caption',json.loads(old['tables']['studio_jobs'][0]['config_json'])['caption'])
        with db.read() as c:
            for table in ('studio_jobs','studio_templates','studio_assets'):
                self.assertEqual(0,c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0])
        import_workspace(db,owner,self.root/'destination',payload)
        self.assertEqual(archives,list(Path(str(db.path)+'.posting-retirement/cloud-snapshots').glob('*.json')))

    def test_archive_failure_or_corruption_prevents_partial_cloud_restore(self):
        payload=self.legacy_cloud_payload();db,owner=self.destination()
        with patch('app.cloud_workspace.os.replace',side_effect=OSError('synthetic archive failure')):
            with self.assertRaises(ValidationError):import_workspace(db,owner,self.root/'destination',payload)
        with db.read() as c:
            self.assertEqual(0,c.execute('SELECT COUNT(*) FROM studio_jobs').fetchone()[0])
        import_workspace(db,owner,self.root/'destination',payload)
        archive=next(Path(str(db.path)+'.posting-retirement/cloud-snapshots').glob('*.json'))
        archive.write_bytes(b'corrupted archive')
        with self.assertRaises(ValidationError):import_workspace(db,owner,self.root/'destination',payload)
        self.assertEqual(b'corrupted archive',archive.read_bytes())

    def test_cloud_archive_symlink_is_rejected_before_active_restore(self):
        payload=self.legacy_cloud_payload();db,owner=self.destination()
        outside=self.root/'unexpected-archive';outside.mkdir()
        Path(str(db.path)+'.posting-retirement').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValidationError):import_workspace(db,owner,self.root/'destination',payload)
        self.assertEqual([],list(outside.iterdir()))
        with db.read() as c:
            self.assertEqual(0,c.execute('SELECT COUNT(*) FROM studio_jobs').fetchone()[0])

    def test_active_local_lease_blocks_cloud_restore_without_clearing_owner(self):
        payload=self.legacy_cloud_payload();db,owner=self.destination()
        service=CoreService(db);token=service.acquire_browser_lease(owner,'owned-window',operation_type='account',entity_id='active-owner')
        with self.assertRaises(ConflictError):import_workspace(db,owner,self.root/'destination',payload)
        with db.read() as c:self.assertEqual(token,c.execute('SELECT lease_token FROM browser_operation_leases').fetchone()[0])


    def api(self, browser):
        from fastapi.testclient import TestClient
        from app.main import create_app
        settings=Settings(startup_token='removal-offline-startup-token-12345',database_path=self.db.path,data_dir=self.root)
        app=create_app(settings,database=self.db,bitbrowser=browser)
        client=TestClient(app);self.addCleanup(client.close)
        token=self.service.login('removal-owner','fixture-password-123')['token']
        return app,client,{'X-Startup-Token':settings.startup_token,'Authorization':'Bearer '+token}

    def test_authenticated_generic_closed_window_action_preserves_outcome_in_archive(self):
        from contextlib import contextmanager
        from test_account_workspace import Browser
        from app.posting_schema import initialize_posting_schema
        from app.errors import UpstreamUnavailableError
        class ClosedProvider(Browser):
            is_closed=False
            @contextmanager
            def closed_profile_guard(self,profile,owner):
                if not self.is_closed:raise UpstreamUnavailableError('窗口未确认关闭')
                yield {'closed':True,'profile_id':profile,'owner_user_id':owner,'verification':'desktop-absence-v1'}
            def open_profile(self,*args):raise AssertionError('must not open browser')
            def close_profile(self,*args):raise AssertionError('must not close browser')
        provider=ClosedProvider();app,client,headers=self.api(provider)
        ident=app.state.accounts.save(self.owner,{'name':'retired occupied window','profile_id':'w1'})['id']
        with self.db.write() as c:
            initialize_posting_schema(c)
            c.execute("""INSERT INTO posting_jobs(id,owner_user_id,request_key,theme,caption,profile_id,status,lease_token,attempt_id,submitted_at,created_at,updated_at)
                VALUES('unknown-job',?,'unknown-job','fixture','exact unknown caption','w1','needs_review','exact-old-token','unknown-attempt','2026-01-01','2026-01-01','2026-01-01')""",(self.owner,))
            c.execute("""INSERT INTO browser_operation_leases VALUES('w1',?,'posting','unknown-job','exact-old-token','2026-01-01','2026-01-01','2026-01-01')""",(self.owner,))
            original=dict(c.execute("SELECT * FROM posting_jobs WHERE id='unknown-job'").fetchone())
        locks=app.state.accounts.snapshot(self.owner)['locks']
        self.assertEqual({'profile_id':'w1','operation_type':'account','state':'occupied','entity_id':None,'can_reconcile_window_state':True},locks['w1'])
        body={'action':'reconcile_window_state','id':ident}
        self.assertEqual(401,client.post('/api/accounts/command',headers={'X-Startup-Token':headers['X-Startup-Token']},json=body).status_code)
        other=self.service.register_user('other-removal','fixture-password-123')['id']
        foreign=dict(headers,Authorization='Bearer '+self.service.login('other-removal','fixture-password-123')['token'])
        self.assertEqual(404,client.post('/api/accounts/command',headers=foreign,json=body).status_code)
        self.assertNotEqual(200,client.post('/api/accounts/command',headers=headers,json=body).status_code)
        with self.db.read() as c:self.assertEqual('exact-old-token',c.execute('SELECT lease_token FROM browser_operation_leases').fetchone()[0])
        provider.is_closed=True
        response=client.post('/api/accounts/command',headers=headers,json=body)
        self.assertEqual(200,response.status_code,response.text);self.assertTrue(response.json()['reconciled'])
        with self.db.read() as c:
            self.assertEqual(0,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
            self.assertEqual(0,c.execute('SELECT count(*) FROM posting_jobs').fetchone()[0])
        import sqlite3
        with closing(sqlite3.connect(Path(str(self.db.path)+'.posting-retirement')/'archive.sqlite3')) as c:
            saved=[json.loads(r[0]) for r in c.execute("SELECT row_json FROM archived_rows WHERE source_table='posting_jobs'")]
        self.assertIn(original,saved)
        self.assertEqual('needs_review',saved[0]['status'])
        self.assertFalse(client.post('/api/accounts/command',headers=headers,json=body).json()['reconciled'])

    def test_failed_startup_archive_keeps_nonposting_service_available_and_original_rows(self):
        from test_account_workspace import Browser
        from fastapi.testclient import TestClient
        media=self.seed_legacy();media.unlink()
        before={}
        with self.db.read() as c:
            for table in ('studio_jobs','studio_assets','studio_templates'):
                before[table]=[dict(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid')]
        app,_,headers=self.api(Browser())
        with TestClient(app) as client:
            self.assertTrue(app.state.retirement_archive_pending)
            self.assertEqual(200,client.get('/api/studio/snapshot',headers=headers).status_code)
            self.assertEqual([],client.get('/api/studio/snapshot',headers=headers).json()['jobs'])
            with self.db.read() as c:
                for table,expected in before.items():
                    self.assertEqual(expected,[dict(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid')])
            # Disabled queued work cannot reserve this unused window forever.
            result=client.post('/api/studio/command',headers=headers,json={'action':'start','kind':'nurture',
                'request_id':'after-archive-failure','profile_ids':['old-posting'],'config':{'minutes':1}})
            self.assertEqual(200,result.status_code,result.text)


if __name__=='__main__':unittest.main()
