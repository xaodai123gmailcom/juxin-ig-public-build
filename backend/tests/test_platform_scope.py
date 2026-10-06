"""Pure-IG boundary replaces deliberately removed mixed-platform feature tests.

Legacy FB rows are injected AFTER startup to exercise defense in depth independently
of the authorized startup deletion migration. No live accounts or network are used.
"""
import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError as ModelValidationError

from app.database import Database
from app.errors import ConflictError, ValidationError
from app.platform_scope import account_platform_sql, global_seen_platform_total, validate_platform
from app.schemas import TaskSettingsRequest, DesktopTaskCreateRequest
from app.service import CoreService, normalize_instagram_username, validate_task_settings


class _PureIGFixture:
    def setup_database(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'pure-ig.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('pure-ig-owner', 'integration test password')['id']

    def candidate(self, name, *, dormant=False, approved=False):
        claim = self.service.claim_workbench_identity(self.owner, username=name, source='followers')
        item = self.service.create_workbench_candidate(self.owner, claim_id=claim['claim_id'], username=name,
            visibility='public', profile={}, screening={}, review_cache={})
        if approved:
            self.service.decide_workbench_candidate(self.owner, candidate_id=item['id'], decision='approved')
        if dormant:
            with self.db.write() as c:
                c.execute('UPDATE instagram_accounts SET current_username_norm=?,current_username_display=? WHERE id=?',
                          ('fb:'+name,'fb:'+name,item['account_id']))
                c.execute('UPDATE instagram_username_aliases SET username_norm=? WHERE account_id=?',('fb:'+name,item['account_id']))
        return item

    def task(self, name='source', *, dormant=False):
        item = self.service.create_task(self.owner, name=name, modes=['followers'], targets=[name],
                                       window_ids=['window-ig'], settings={})
        if dormant:
            settings={**item['settings'], 'platform':'facebook'}
            with self.db.write() as c:
                c.execute('UPDATE tasks SET settings_json=? WHERE id=?',(json.dumps(settings),item['id']))
                c.execute("UPDATE task_targets SET username_norm='fb:'||username_norm,username_display='fb:'||username_display WHERE task_id=?",(item['id'],))
        return item


class PureIGScopeTests(_PureIGFixture, unittest.TestCase):
    def setUp(self): self.setup_database()

    def test_only_instagram_platform_and_identity_inputs_are_accepted(self):
        self.assertEqual(('person','Person'), normalize_instagram_username('https://www.instagram.com/Person/'))
        for value in ['fb:person', ' FB:person ', 'https://www.facebook.com/person', 'facebook.com/person', 'https://m.facebook.com/profile.php?id=123']:
            with self.subTest(value=value), self.assertRaises(ValidationError): normalize_instagram_username(value)
        for value in ['facebook','Facebook','all','',False]:
            with self.subTest(value=value), self.assertRaises(ValidationError): validate_platform(value)
        with self.assertRaises(ModelValidationError): TaskSettingsRequest(platform='facebook')
        with self.assertRaises(ModelValidationError): TaskSettingsRequest(facebook_relation_strategy='auto')
        with self.assertRaises(ModelValidationError): DesktopTaskCreateRequest(platform='facebook',window_ids=['w'],modes=['followers'])
        with self.assertRaises(ValidationError): validate_task_settings({'facebook_relation_strategy':'auto'})
        with self.assertRaises(ValidationError): self.service.create_task(self.owner,name='blocked',modes=['followers'],targets=['fb:source'],settings={})
        with self.assertRaises(ValidationError): self.service.upsert_manual_split_candidates(self.owner,[{'username':'fb:source'}])

    def test_omitted_scope_snapshot_dedupe_and_review_are_ig_only(self):
        ig=self.candidate('visible');self.candidate('hidden',dormant=True)
        for platform in [None,'instagram']:
            snapshot=self.service.get_workbench_snapshot(self.owner,limit=1,history_limit=1,maintain=False,platform=platform)
            self.assertEqual(1,snapshot['counts']['pending_public'])
            self.assertEqual([ig['id']],[r['id'] for r in snapshot['pending_public_accounts']])
            self.assertEqual(1,snapshot['global_dedupe_count'])
            self.assertEqual(1,self.service.get_workbench_dedupe_stats(self.owner,platform=platform)['total'])
        with self.db.read() as c:
            self.assertEqual(2,c.execute('SELECT COUNT(*) FROM global_seen').fetchone()[0])
            self.assertEqual(1,global_seen_platform_total(c,None))
        self.assertEqual(2,self.service.claim_workbench_identity(self.owner,username='new.ig',source='followers')['global_dedupe_count'])

    def test_legacy_tasks_without_platform_remain_visible_dormant_tasks_cannot_be_read_or_controlled(self):
        ig=self.task('visible.source');fb=self.task('dormant.source',dormant=True)
        with self.db.write() as c:
            settings=dict(ig['settings']);settings.pop('platform',None)
            c.execute('UPDATE tasks SET settings_json=? WHERE id=?',(json.dumps(settings),ig['id']))
        for platform in [None,'instagram']:
            self.assertEqual(1,self.service.count_tasks(self.owner,platform=platform))
            self.assertEqual([ig['id']],[r['id'] for r in self.service.list_tasks_with_details(self.owner,limit=1,platform=platform)])
            self.assertEqual(1,self.service.count_history(self.owner,platform=platform))
            self.assertEqual(ig['id'],self.service.list_history(self.owner,limit=1,platform=platform)[0]['task_id'])
        for operation in [lambda:self.service.get_task(self.owner,fb['id']),
                          lambda:self.service.get_task_status(self.owner,fb['id']),
                          lambda:self.service.get_task_execution_spec(self.owner,fb['id']),
                          lambda:self.service.guard_platform_command(self.owner,'task_control',{'task_id':fb['id']},None),
                          lambda:self.service.control_task(self.owner,fb['id'],'start')]:
            with self.assertRaises(ValidationError): operation()
        with self.db.read() as c:self.assertEqual(2,c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])

    def test_candidate_mutations_reject_legacy_fb_ids_without_platform(self):
        ig=self.candidate('pending');fb=self.candidate('dormant',dormant=True)
        for platform in [None,'instagram']:
            with self.assertRaises(ConflictError):self.service.decide_workbench_candidate(self.owner,candidate_id=fb['id'],decision='approved',platform=platform)
            with self.assertRaises(ConflictError):self.service.guard_platform_command(self.owner,'review_decision',{'candidate_id':fb['id']},platform)
            with self.assertRaises(ConflictError):self.service.guard_platform_command(self.owner,'dedupe_claim',{'username':'FB:legacy'},platform)
        self.service.decide_workbench_candidate(self.owner,candidate_id=ig['id'],decision='approved')
        with self.assertRaises(ValidationError):self.service.dismiss_approved_candidate(self.owner,ig['id'],mark_used=True)

    def test_stable_id_cannot_revive_or_relabel_dormant_identity(self):
        fb=self.candidate('dormant',dormant=True)
        with self.db.write() as c:c.execute('UPDATE instagram_accounts SET instagram_user_id=? WHERE id=?',('123456',fb['account_id']))
        for stable in ['fb:123456','fbid:123456',' FBID:123456 ','123456']:
            with self.assertRaises((ValidationError,ConflictError)):
                self.service.claim_workbench_identity(self.owner,username='new.ig',instagram_user_id=stable,source='followers')
        with self.db.read() as c:self.assertEqual('fb:dormant',c.execute('SELECT current_username_norm FROM instagram_accounts WHERE id=?',(fb['account_id'],)).fetchone()[0])

    def test_scope_uses_covering_identity_index_without_profile_json(self):
        self.candidate('ig');self.candidate('dormant',dormant=True)
        sql="SELECT status,visibility,COUNT(*) FROM workbench_candidates candidate WHERE owner_user_id=?"+account_platform_sql(None,'candidate.account_id')+" GROUP BY status,visibility"
        with self.db.read() as c:plan=[row[3] for row in c.execute('EXPLAIN QUERY PLAN '+sql,(self.owner,))]
        self.assertTrue(any('COVERING INDEX idx_accounts_platform_scope' in row for row in plan),plan)

    def test_old_fb_rows_do_not_crowd_pagination(self):
        seed=self.candidate('dormant',dormant=True)
        with self.db.write() as c:
            account=dict(c.execute('SELECT * FROM instagram_accounts WHERE id=?',(seed['account_id'],)).fetchone())
            candidate=dict(c.execute('SELECT * FROM workbench_candidates WHERE id=?',(seed['id'],)).fetchone())
            for n in range(510):
                a={**account,'id':f'a-{n}','current_username_norm':f'fb:bulk.{n}','current_username_display':f'fb:bulk.{n}'}
                c.execute(f"INSERT INTO instagram_accounts({','.join(a)}) VALUES({','.join('?' for _ in a)})",tuple(a.values()))
                row={**candidate,'id':f'c-{n}','account_id':a['id'],'created_at':'2000-01-01T00:00:00Z'}
                c.execute(f"INSERT INTO workbench_candidates({','.join(row)}) VALUES({','.join('?' for _ in row)})",tuple(row.values()))
        ig=self.candidate('visible')
        result=self.service.get_workbench_snapshot(self.owner,limit=1,history_limit=1,maintain=False)
        self.assertEqual(1,result['counts']['pending_public'])
        self.assertEqual([ig['id']],[r['id'] for r in result['pending_public_accounts']])

    def test_account_destinations_and_cookie_import_reject_fb(self):
        from app.account_platforms import PLATFORMS,platform_config,parse_cookies
        self.assertNotIn('facebook',PLATFORMS);self.assertNotIn('messenger',PLATFORMS)
        for destination in ['facebook','messenger',{'id':'custom','url':'https://www.facebook.com/'},{'id':'custom','url':'https://www.messenger.com/'}]:
            with self.assertRaises(ValidationError):platform_config(destination)
            with self.assertRaises(ValidationError):parse_cookies('sessionid=inert',destination)

    def test_mixed_result_rows_do_not_surface_as_instagram(self):
        task=self.task('source')
        results=[]
        for name in ['visible.result','dormant.result']:
            claim=self.service.claim_workbench_identity(self.owner,username=name,source='followers')
            results.append(self.service.record_result(self.owner,task['id'],task['targets'][0]['id'],
                username=name,instagram_user_id=None,source_mode='followers',visibility='public',
                profile={},screening={},qualified=True,dedupe_claim_id=claim['claim_id']))
        dormant=results[1]
        with self.db.write() as c:
            c.execute("UPDATE instagram_accounts SET current_username_norm='fb:dormant.result',current_username_display='fb:dormant.result' WHERE id=?",(dormant['account_id'],))
        self.assertEqual([results[0]['id']],[r['id'] for r in self.service.list_results(self.owner,task['id'])])
        self.assertEqual([results[0]['id']],[r['id'] for r in self.service.list_all_results(self.owner,limit=1)])
        self.assertEqual(1,self.service.count_all_results(self.owner))
        self.assertEqual(1,self.service.get_workbench_snapshot(self.owner,maintain=False)['counts']['total_collected'])

    def test_saved_fb_account_plan_is_hidden_and_never_opened(self):
        from app.account_workspace import AccountWorkspace
        from app.errors import NotFoundError
        from test_account_platforms import Browser
        from unittest.mock import Mock
        opener=Mock()
        workspace=AccountWorkspace(self.service,Browser(),opener=opener)
        ident=workspace.save(self.owner,{'name':'legacy plan','platform':'instagram','profile_id':'w1'})['id']
        with self.db.write() as c:c.execute('UPDATE account_window_plans SET environment_json=? WHERE id=?',(json.dumps({'platform':'facebook'}),ident))
        self.assertEqual([],workspace.snapshot(self.owner)['plans'])
        with self.assertRaises(NotFoundError):workspace.command(self.owner,{'action':'open','id':ident})
        with self.assertRaises(ValidationError):workspace.control_profile(self.owner,'w1','open')
        opener.assert_not_called()


    def test_legacy_fb_target_under_shared_ig_task_is_hidden_from_history(self):
        task=self.service.create_task(self.owner,name='mixed legacy',modes=['followers'],targets=['visible','dormant'],window_ids=['w'],settings={})
        hidden=next(t for t in task['targets'] if t['username']=='dormant')
        with self.db.write() as c:c.execute("UPDATE task_targets SET username_norm='fb:dormant',username_display='fb:dormant' WHERE id=?",(hidden['id'],))
        self.assertEqual(['visible'],[r['username'] for r in self.service.get_task(self.owner,task['id'])['targets']])
        self.assertEqual(['visible'],[r['username'] for r in self.service.list_tasks_with_details(self.owner,detail_limit=1)[0]['targets']])
        self.assertEqual(1,self.service.count_history(self.owner))
        self.assertEqual(1,len(self.service.list_history(self.owner,limit=1)))
        with self.assertRaises(ConflictError):self.service.append_task_mode_candidates(self.owner,task['id'],hidden['id'],'followers',['new.person'])


    def test_retired_profile_never_defaults_to_ig_without_explicit_new_setup(self):
        from app.account_platforms import platform_for_profile, profile_platforms
        from app.account_workspace import AccountWorkspace
        from test_account_platforms import Browser
        from unittest.mock import Mock
        opener=Mock();workspace=AccountWorkspace(self.service,Browser(),opener=opener)
        with self.db.write() as c:c.execute('INSERT INTO retired_account_profiles(owner_user_id,profile_id) VALUES(?,?)',(self.owner,'w1'))
        with self.db.read() as c:
            self.assertEqual('unknown',platform_for_profile(c,'w1',owner_user_id=self.owner))
            self.assertEqual('unknown',profile_platforms(c,owner_user_id=self.owner)['w1'])
        snapshot=workspace.snapshot(self.owner,window_listing={'windows':[{'id':'w1','name':'retired','is_open':False}]})
        self.assertEqual([],snapshot['windows'])
        with self.assertRaises(ValidationError):self.service.acquire_browser_lease(self.owner,'w1',operation_type='collection',entity_id='inert')
        with self.assertRaises(ValidationError):workspace.control_profile(self.owner,'w1','open')
        with self.assertRaises(ValidationError):workspace.save(self.owner,{'name':'implicit','profile_id':'w1'})
        opener.assert_not_called()
        workspace.save(self.owner,{'name':'explicit IG','profile_id':'w1','platform':'instagram'})
        with self.db.read() as c:
            self.assertEqual(0,c.execute('SELECT COUNT(*) FROM retired_account_profiles WHERE owner_user_id=?',(self.owner,)).fetchone()[0])
            self.assertEqual('instagram',platform_for_profile(c,'w1',owner_user_id=self.owner))


    def test_all_stable_id_entrypoints_reject_legacy_fbid_before_mutation(self):
        task=self.task('ig.source')
        claim=self.service.claim_workbench_identity(self.owner,username='ig.person',source='followers')
        for stable in ['fbid:123456','FBID:987654','  fbid:123456  ']:
            for method in [
                lambda:self.service.claim_workbench_identity(self.owner,username='new.person',instagram_user_id=stable,source='followers'),
                lambda:self.service.confirm_workbench_identity(self.owner,claim_id=claim['claim_id'],username='ig.person',instagram_user_id=stable),
                lambda:self.service.record_result(self.owner,task['id'],task['targets'][0]['id'],username='ig.person',instagram_user_id=stable,source_mode='followers',visibility='public',profile={},screening={},qualified=True,dedupe_claim_id=claim['claim_id']),
            ]:
                with self.subTest(stable=stable),self.assertRaises(ValidationError):method()
        with self.db.read() as c:
            self.assertIsNone(c.execute('SELECT instagram_user_id FROM instagram_accounts WHERE id=?',(claim['claim_id'],)).fetchone()[0])
            self.assertEqual(0,c.execute("SELECT COUNT(*) FROM instagram_accounts WHERE lower(instagram_user_id) GLOB 'fbid:*'").fetchone()[0])
            self.assertEqual(0,c.execute('SELECT COUNT(*) FROM task_results').fetchone()[0])


    def test_malformed_and_nonobject_task_settings_stay_preserved_but_inactive(self):
        from app.cloud_workspace import export_workspace, decode_workspace
        from app.account_exports import export_accounts
        from app.work_reports import history_totals
        invalid=['{','[]','null','123','"scalar"','true','{"platform":null}']
        hidden=[]
        for n,raw in enumerate(invalid):
            task=self.task('ambiguous.'+str(n))
            self.service.record_result(self.owner,task['id'],task['targets'][0]['id'],username='held.result.'+str(n),instagram_user_id=None,source_mode='followers',visibility='public',profile={},screening={},qualified=False)
            with self.db.write() as c:c.execute('UPDATE tasks SET settings_json=? WHERE id=?',(raw,task['id']))
            hidden.append((task['id'],raw))
        visible=self.task('legacy.valid')
        with self.db.write() as c:c.execute("UPDATE tasks SET settings_json='{}' WHERE id=?",(visible['id'],))
        self.assertEqual([visible['id']],[t['id'] for t in self.service.list_tasks(self.owner)])
        self.assertEqual([visible['id']],[t['id'] for t in self.service.list_tasks_with_details(self.owner)])
        self.assertEqual(1,self.service.count_tasks(self.owner))
        self.assertEqual(1,self.service.count_history(self.owner))
        self.assertEqual([visible['id']],[r['task_id'] for r in self.service.list_history(self.owner)])
        self.assertEqual([],self.service.list_all_results(self.owner))
        self.assertEqual(0,self.service.count_all_results(self.owner))
        self.assertEqual(0,self.service.get_workbench_snapshot(self.owner,maintain=False)['counts']['total_collected'])
        with self.db.read() as c:self.assertEqual(len(invalid),c.execute('SELECT COUNT(*) FROM task_results').fetchone()[0])
        self.assertEqual(0,history_totals(self.db,self.owner,'2020-01-01T00:00:00Z','2030-01-01T00:00:00Z')['total_collected'])
        self.assertEqual(0,export_accounts(self.db,self.owner,visibility='public',scope='all')['row_count'])
        for task_id,raw in hidden:
            for action in [lambda:self.service.get_task(self.owner,task_id),
                           lambda:self.service.get_task_status(self.owner,task_id),
                           lambda:self.service.acquire_browser_lease(self.owner,'window-ig',operation_type='collection',entity_id=task_id)]:
                with self.subTest(raw=raw),self.assertRaises(ValidationError):action()
        # Backup keeps ambiguous bytes; read guards do not delete or repair them.
        payload,_=export_workspace(self.db,self.owner,Path(self.temp.name))
        preserved={row['id']:row['settings_json'] for row in decode_workspace(payload)['tables']['tasks']}
        self.assertEqual(dict(hidden),{task_id:preserved[task_id] for task_id,_ in hidden})
        self.assertEqual('instagram',self.service.get_task(self.owner,visible['id'])['settings']['platform'])


class PureIGHTTPTests(_PureIGFixture, unittest.TestCase):
    def setUp(self): self.setup_database()

    def test_legacy_routes_reject_fb_platform_and_explicit_dormant_ids(self):
        from fastapi.testclient import TestClient
        from app.config import Settings
        from app.main import create_app
        from test_core import FakeBitBrowserClient
        settings=Settings(startup_token='pure-ig-test-startup-token-long-enough', database_path=self.db.path, data_dir=Path(self.temp.name))
        app=create_app(settings,database=self.db,bitbrowser=FakeBitBrowserClient())
        login=self.service.login('pure-ig-owner','integration test password')
        headers={'X-Startup-Token':settings.startup_token,'Authorization':f"Bearer {login['token']}"}
        with TestClient(app) as client:
            # Simulate an old writer/backup AFTER application startup migration.
            ig=self.task('visible');fb=self.task('dormant',dormant=True)
            candidate=self.candidate('hidden',dormant=True)
            self.assertEqual([ig['id']],[r['id'] for r in client.get('/api/tasks',headers=headers).json()['tasks']])
            self.assertEqual(422,client.get('/api/tasks?platform=facebook',headers=headers).status_code)
            self.assertGreaterEqual(client.get('/api/tasks/'+fb['id'],headers=headers).status_code,400)
            for payload in [
                {'command':'task_control','payload':{'task_id':fb['id'],'action':'start'}},
                {'command':'review_decision','payload':{'candidate_id':candidate['id'],'decision':'approved'}},
                {'command':'dedupe_claim','payload':{'username':'fb:blocked','source':'followers'}},
                {'command':'dedupe_claim','payload':{'username':'visible','source':'followers','platform':'facebook'}},
            ]:
                with self.subTest(command=payload):
                    self.assertGreaterEqual(client.post('/api/workbench/commands',headers=headers,json=payload).status_code,400)
            self.assertEqual(422,client.post('/api/tasks',headers=headers,json={'platform':'facebook','targets':['fb:x'],'window_ids':['window-a'],'modes':['followers']}).status_code)
            self.assertEqual(422,client.post('/api/workbench/accounts/export',headers=headers,json={'visibility':'public','scope':'all','platform':'facebook'}).status_code)
            with self.db.read() as c:self.assertEqual(0,c.execute('SELECT COUNT(*) FROM browser_operation_leases').fetchone()[0])
