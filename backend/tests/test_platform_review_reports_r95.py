"""Pure-IG review/export/report regressions replace retired FB workspace assertions."""
import csv
import io
import unittest
from datetime import datetime, timezone

from app.account_exports import HEADERS, export_accounts
from app.errors import ConflictError, ValidationError
from app.review_layers import query_review_queue, move_review_stage
from app.work_reports import history_totals, work_report, split_review_report
from test_platform_scope import _PureIGFixture


class PureIGReviewReportTests(_PureIGFixture, unittest.TestCase):
    def setUp(self):self.setup_database()

    def test_queue_counts_offsets_and_bulk_mutation_exclude_old_fb(self):
        ig=self.candidate('visible');fb=self.candidate('dormant',dormant=True)
        queue=query_review_queue(self.service,self.owner,visibility='public',review_stage=1,limit=1)
        self.assertEqual(1,queue['total']);self.assertEqual(ig['id'],queue['items'][0]['id'])
        with self.assertRaises(ConflictError):move_review_stage(self.service,self.owner,candidate_ids=[ig['id'],fb['id']],visibility='public',from_stage=1,to_stage=2)
        self.assertEqual(1,query_review_queue(self.service,self.owner,visibility='public',review_stage=1)['total'])

    def test_all_and_selected_exports_cannot_surface_old_fb(self):
        ig=self.candidate('visible',approved=True);fb=self.candidate('dormant',dormant=True,approved=True)
        for platform in [None,'instagram']:
            result=export_accounts(self.db,self.owner,visibility='public',scope='all',platform=platform)
            rows=list(csv.DictReader(io.StringIO(result['csv'].removeprefix('\ufeff'))))
            self.assertEqual(['visible'],[r['账号'] for r in rows]);self.assertNotIn('好友数',HEADERS)
            with self.assertRaises(ConflictError):export_accounts(self.db,self.owner,visibility='public',scope='selected',candidate_ids=[ig['id'],fb['id']],platform=platform)
        with self.assertRaises(ValidationError):export_accounts(self.db,self.owner,visibility='public',scope='all',platform='facebook')

    def test_collection_report_and_history_only_count_ig(self):
        self.candidate('visible',approved=True);self.candidate('dormant',dormant=True,approved=True)
        bounds=('2020-01-01T00:00:00Z','2030-01-01T00:00:00Z')
        for platform in [None,'instagram']:
            counts=history_totals(self.db,self.owner,*bounds,platform=platform)
            self.assertEqual(1,counts['total_collected']);self.assertEqual(1,counts['approved']);self.assertEqual(1,counts['global_dedupe'])
            report=work_report(self.db,self.owner,*bounds,platform=platform)
            self.assertEqual(1,report['totals']['collection']);self.assertEqual(1,report['totals']['approved'])
        with self.assertRaises(ValidationError):history_totals(self.db,self.owner,*bounds,platform='facebook')

    def test_split_history_hidden_before_page_and_daily_count(self):
        with self.db.write() as c:
            for n,name in enumerate(['visible','fb:dormant']):
                c.execute("INSERT INTO split_candidate_history(id,owner_user_id,username_norm,username_display,source_status,completed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(f'history-{n}',self.owner,name,name,'completed','2026-09-24T08:00:00Z','2026-09-24T08:00:00Z','2026-09-24T08:00:00Z'))
        rows=self.service.list_split_candidates(self.owner,row_limit=1,completed_limit=1)
        self.assertEqual(['visible'],[r['username'] for r in rows])
        report=split_review_report(self.db,self.owner,'2026-09-24T00:00:00Z','2026-09-25T00:00:00Z',utc_offset_minutes=0,limit=1,now=datetime(2026,9,24,12,tzinfo=timezone.utc))
        self.assertEqual(1,report['total']);self.assertEqual(1,report['daily_counts']['2026-09-24'])
        with self.assertRaises(ConflictError):self.service.guard_platform_command(self.owner,'split_failure_delete',{'candidate_id':'history-1'},None)

    def test_old_backup_restore_reapplies_purge_after_migration_marker(self):
        from pathlib import Path
        from app.cloud_workspace import export_workspace, import_workspace
        from app.database import Database
        from app.service import CoreService
        ig=self.candidate('visible',approved=True);self.candidate('dormant',dormant=True,approved=True)
        self.task('visible.source');self.task('dormant.source',dormant=True)
        payload,_=export_workspace(self.db,self.owner,Path(self.temp.name))
        target=Database(Path(self.temp.name)/'restored.sqlite3');target.initialize()
        service=CoreService(target);owner=service.register_user('restored-owner','integration test password')['id']
        import_workspace(target,owner,Path(self.temp.name)/'media',payload)
        self.assertEqual(1,service.count_tasks(owner))
        result=export_accounts(target,owner,visibility='public',scope='all')
        self.assertEqual(1,result['row_count']);self.assertIn('visible',result['csv'])
        with target.read() as c:
            self.assertEqual(0,c.execute("SELECT COUNT(*) FROM tasks WHERE json_extract(settings_json,'$.platform')='facebook'").fetchone()[0])
            self.assertEqual(0,c.execute("SELECT COUNT(*) FROM instagram_accounts WHERE current_username_norm GLOB 'fb:*'").fetchone()[0])
            self.assertEqual(1,c.execute('SELECT COUNT(*) FROM workbench_candidates').fetchone()[0])
            self.assertEqual([],c.execute('PRAGMA foreign_key_check').fetchall())


    def test_cloud_backup_keeps_retired_profile_fence_without_fb_content(self):
        from pathlib import Path
        from app.cloud_workspace import export_workspace, import_workspace, decode_workspace
        from app.database import Database
        from app.service import CoreService
        from app.account_platforms import platform_for_profile
        with self.db.write() as c:c.execute('INSERT INTO retired_account_profiles(owner_user_id,profile_id) VALUES(?,?)',(self.owner,'retired-window'))
        payload,_=export_workspace(self.db,self.owner,Path(self.temp.name))
        row=decode_workspace(payload)['tables']['retired_account_profiles'][0]
        self.assertEqual({'owner_user_id','profile_id'},set(row))
        target=Database(Path(self.temp.name)/'fence-restored.sqlite3');target.initialize()
        service=CoreService(target);owner=service.register_user('fence-restored-owner','integration test password')['id']
        import_workspace(target,owner,Path(self.temp.name)/'fence-media',payload)
        with target.read() as c:
            self.assertEqual('unknown',platform_for_profile(c,'retired-window',owner_user_id=owner))
            self.assertEqual(owner,c.execute('SELECT owner_user_id FROM retired_account_profiles').fetchone()[0])
            self.assertEqual([],c.execute('PRAGMA foreign_key_check').fetchall())

    def test_cloud_restore_rebuilds_exact_counts_and_progress_before_commit(self):
        from pathlib import Path
        from unittest.mock import patch
        from app.cloud_workspace import export_workspace, import_workspace
        from app.database import Database
        from app.service import CoreService
        task=self.task('restore.source');target_id=task['targets'][0]['id']
        claim=self.service.claim_workbench_identity(self.owner,username='restored.person',source='followers',source_target=target_id)
        candidate=self.service.create_workbench_candidate(self.owner,claim_id=claim['claim_id'],username='restored.person',visibility='public',profile={},screening={},review_cache={},source_mode='followers',source_target=target_id)
        self.service.record_result(self.owner,task['id'],target_id,username='restored.person',instagram_user_id='12345',source_mode='followers',visibility='public',profile={},screening={},qualified=True,dedupe_claim_id=claim['claim_id'])
        self.service.decide_workbench_candidate(self.owner,candidate_id=candidate['id'],decision='approved')
        before=self.service.get_workbench_snapshot(self.owner,maintain=False)['counts']
        progress=self.service.get_task(self.owner,task['id'])['targets'][0]['mode_progress']['followers']
        self.assertEqual(1,progress['qualified_for_review'])
        payload,_=export_workspace(self.db,self.owner,Path(self.temp.name))
        destination=Database(Path(self.temp.name)/'aggregate-restored.sqlite3');destination.initialize()
        service=CoreService(destination);owner=service.register_user('aggregate-restored-owner','integration test password')['id']
        # A failed projection repair must roll back the imported business graph.
        with patch('app.workbench_progress_aggregates.rebuild_workbench_progress_aggregates',side_effect=RuntimeError('inert repair failure')):
            with self.assertRaises(RuntimeError):import_workspace(destination,owner,Path(self.temp.name)/'aggregate-media',payload)
        with destination.read() as c:
            self.assertEqual(0,c.execute('SELECT COUNT(*) FROM task_results').fetchone()[0])
            self.assertEqual(0,c.execute('SELECT COUNT(*) FROM workbench_candidates').fetchone()[0])
        import_workspace(destination,owner,Path(self.temp.name)/'aggregate-media',payload)
        self.assertEqual(before,service.get_workbench_snapshot(owner,maintain=False)['counts'])
        self.assertEqual(progress,service.get_task(owner,task['id'])['targets'][0]['mode_progress']['followers'])
        with destination.read() as c:self.assertEqual([],c.execute('PRAGMA foreign_key_check').fetchall())
