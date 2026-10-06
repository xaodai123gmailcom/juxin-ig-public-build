from __future__ import annotations
import base64
import hashlib
import json
import sys
import tempfile
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.service import CoreService
from app.errors import ConflictError
from app.split_admissions import backfill_split_admissions
from app.cloud_workspace import export_workspace, import_workspace, decode_workspace
from app.work_reports import split_review_report

PASSWORD = 'r55 split counts test password'

class SplitCountR55Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.db = Database(self.root / 'r55.db'); self.db.initialize()
        self.service = CoreService(self.db); self.owner = self.service.register_user('r55-count-owner', PASSWORD)['id']

    def tearDown(self): self.temp.cleanup()

    def add(self, name, owner=None, **options):
        return self.service.upsert_manual_split_candidates(owner or self.owner, [{'username': name, 'queued': True}], include_outcome=True, **options)

    def totals(self, name, owner=None):
        with self.db.read() as c:
            row = c.execute('SELECT * FROM split_admission_totals WHERE owner_user_id=? AND username_norm=?', (owner or self.owner, name)).fetchone()
            return dict(row) if row else None

    def direct(self, name, **options):
        return self.service.create_task(self.owner, name='direct', modes=['followers'], targets=[name], settings={'location_enabled': False}, **options)

    def live(self, name, *, window_id='w1'):
        added = self.add(name)
        task = self.service.create_task(self.owner, name='live', modes=['followers'], targets=[], settings={'live_queue_enabled': True, 'location_enabled': False}, window_ids=[window_id])
        self.service.set_task_runtime_status(self.owner, task['id'], 'running')
        target = self.service.claim_next_split_candidate(self.owner, task['id'], window_id)
        return added, task, target

    def report(self):
        now = datetime.now(timezone.utc); start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return split_review_report(self.db, self.owner, start.isoformat(), (start+timedelta(days=1)).isoformat(), utc_offset_minutes=0, now=now)

    def test_first_add_normalized_replays_and_concurrent_duplicates_count_once(self):
        self.assertEqual(1, self.add('@Example.Name')['accepted_count'])
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _: self.add('example.name', allow_completed=True), range(12)))
        self.assertEqual(0, sum(r['accepted_count'] for r in results))
        row=self.totals('example.name'); self.assertEqual(1,row['successful_adds']); self.assertEqual(1,row['history_complete'])
        self.db.initialize(); self.assertEqual(1,self.totals('example.name')['successful_adds'])

    def test_available_promotion_and_queue_replay_do_not_increment(self):
        row=self.service.upsert_manual_split_candidates(self.owner,[{'username':'saved.first'}],include_outcome=True)
        self.assertEqual(1,row['accepted_count'])
        self.assertEqual(1,self.add('saved.first')['accepted_count'])
        self.service.mark_split_candidates_queued(self.owner,row['accepted_ids'])
        self.assertEqual(1,self.totals('saved.first')['successful_adds'])

    def test_delete_unexecuted_waiting_then_explicit_add_increments_and_keeps_dedup(self):
        first=self.add('never.executed'); before=self.service.get_workbench_dedupe_stats(self.owner)
        self.service.delete_waiting_split_candidate(self.owner,first['accepted_ids'][0])
        self.assertEqual(1,self.add('never.executed')['accepted_count'])
        self.assertEqual(2,self.totals('never.executed')['successful_adds'])
        self.assertEqual(before,self.service.get_workbench_dedupe_stats(self.owner))

    def test_claim_completion_requires_confirmation_and_preserves_report(self):
        _,task,target=self.live('done.source')
        self.assertEqual(1,self.totals('done.source')['successful_adds'])
        self.service.set_target_runtime_status(self.owner,task['id'],target['id'],'completed')
        entry=next(x for x in self.report()['items'] if x['username']=='done.source')
        self.assertEqual(1,entry['split_count']); self.assertTrue(entry['split_count_complete'])
        outcome=self.add('done.source')
        self.assertEqual(0,outcome['accepted_count'])
        self.assertEqual('split_already_executed',outcome['duplicates'][0]['reason'])
        self.assertEqual(1,self.add('done.source',allow_completed=True)['accepted_count'])
        self.assertEqual(0,self.add('done.source',allow_completed=True)['accepted_count'])
        with self.assertRaises(ConflictError): self.direct('done.source',allow_completed_targets=True)
        self.assertEqual(2,self.totals('done.source')['successful_adds'])
        self.assertEqual(1,self.report()['total'])

    def test_failed_retry_and_return_reuse_admission(self):
        _,task,target=self.live('retry.source')
        self.service.set_target_runtime_status(self.owner,task['id'],target['id'],'failed')
        self.assertEqual(0,self.add('retry.source',allow_completed=True)['accepted_count'])
        recovery=next(x for x in self.service.list_split_candidates(self.owner) if x.get('kind')=='failure')
        first=self.service.requeue_split_candidate(self.owner,recovery['id'])
        replay=self.service.requeue_split_candidate(self.owner,recovery['id'])
        self.assertEqual(first['id'],replay['id'])
        claimed=self.service.claim_next_split_candidate(self.owner,task['id'],'w1')
        self.assertIsNotNone(claimed)
        self.assertEqual(1,self.totals('retry.source')['successful_adds'])

    def test_direct_target_and_append_count_once_and_cannot_bypass(self):
        first=self.direct('direct.first')
        self.assertEqual(1,self.totals('direct.first')['successful_adds'])
        self.assertEqual(0,self.service.add_targets(self.owner,first['id'],['direct.first'])['added'])
        self.service.add_targets(self.owner,first['id'],['direct.second'])
        self.assertEqual(1,self.totals('direct.second')['successful_adds'])
        self.service.set_target_runtime_status(self.owner,first['id'],first['targets'][0]['id'],'completed')
        other=self.direct('different')
        with self.assertRaises(ConflictError): self.service.add_targets(self.owner,other['id'],['direct.first'])
        with self.assertRaises(ConflictError): self.direct('direct.second')
        self.assertEqual(0,self.add('direct.second')['accepted_count'])

    def test_owner_isolation_and_collection_candidate_dedup_is_not_source_history(self):
        self.service.claim_workbench_identity(self.owner,username='candidate.only',source='followers')
        self.assertEqual(1,self.add('candidate.only')['accepted_count'])
        first=self.direct('owned.source');self.service.set_target_runtime_status(self.owner,first['id'],first['targets'][0]['id'],'completed')
        other=self.service.register_user('r55-other-owner',PASSWORD)['id']
        self.assertEqual(1,self.add('owned.source',owner=other)['accepted_count'])
        self.assertEqual(1,self.totals('owned.source',owner=other)['successful_adds'])

    def test_failed_admission_transaction_rolls_back_count(self):
        with self.db.write() as c:
            c.execute("CREATE TRIGGER reject_split BEFORE INSERT ON split_candidates BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(Exception): self.add('rollback.source')
        self.assertIsNone(self.totals('rollback.source'))

    def test_executed_marker_survives_business_row_deletion_and_restart(self):
        task=self.direct('old.source');self.service.set_target_runtime_status(self.owner,task['id'],task['targets'][0]['id'],'completed')
        # Exercise independent marker, even if a legacy database has lost the history rows.
        with self.db.write() as c:
            c.execute('DELETE FROM task_targets WHERE id=?',(task['targets'][0]['id'],))
            c.execute('DELETE FROM event_log WHERE owner_user_id=?',(self.owner,))
        self.db.initialize()
        self.assertEqual(0,self.add('old.source')['accepted_count'])
        self.assertEqual(1,self.add('old.source',allow_completed=True)['accepted_count'])
        self.assertTrue(self.service.check_global_dedupe('old.source')['seen'])

    def test_legacy_migration_reports_lower_bound_across_seven_day_boundary(self):
        now=datetime.now(timezone.utc); first=self.direct('legacy.repeat')
        self.service.set_target_runtime_status(self.owner,first['id'],first['targets'][0]['id'],'completed')
        with self.db.write() as c:
            c.execute("UPDATE task_targets SET updated_at=? WHERE id=?",((now-timedelta(days=20)).isoformat(),first['targets'][0]['id']))
            for i,stamp in enumerate(((now-timedelta(days=10)).isoformat(),now.isoformat())):
                c.execute("""INSERT INTO split_candidate_history(id,owner_user_id,username_norm,username_display,source_status,completed_at,created_at,updated_at)
                    VALUES(?,?,'legacy.repeat','legacy.repeat','completed',?,?,?)""",(f'old-{i}',self.owner,stamp,stamp,stamp))
            c.execute('DELETE FROM split_admission_totals WHERE owner_user_id=?',(self.owner,))
            c.execute('DELETE FROM schema_migrations WHERE version=33')
        self.db.initialize(); row=self.totals('legacy.repeat')
        self.assertEqual(3,row['successful_adds']);self.assertEqual(0,row['history_complete'])
        entry=self.report()['items'][0]
        self.assertIsNone(entry['split_count']);self.assertEqual(3,entry['split_count_recorded']);self.assertFalse(entry['split_count_complete'])
        self.db.initialize();self.assertEqual(3,self.totals('legacy.repeat')['successful_adds'])

    def test_new_cloud_backup_preserves_exact_counts_owner_and_execution_fence(self):
        task=self.direct('cloud.exact');self.service.set_target_runtime_status(self.owner,task['id'],task['targets'][0]['id'],'completed')
        payload,_=export_workspace(self.db,self.owner,self.root)
        destination=Database(self.root/'new.db');destination.initialize();service=CoreService(destination)
        owner=service.register_user('cloud-destination',PASSWORD)['id']
        import_workspace(destination,owner,self.root/'assets',payload)
        with destination.read() as c:
            row=c.execute('SELECT * FROM split_admission_totals WHERE owner_user_id=?',(owner,)).fetchone()
            self.assertEqual(1,row['successful_adds']);self.assertEqual(1,row['history_complete']);self.assertEqual(1,row['has_executed'])
        self.assertEqual(0,service.upsert_manual_split_candidates(owner,[{'username':'cloud.exact','queued':True}],include_outcome=True)['accepted_count'])
        self.assertEqual(1,service.upsert_manual_split_candidates(owner,[{'username':'cloud.exact','queued':True}],include_outcome=True,allow_completed=True)['accepted_count'])

    def test_old_cloud_backup_gets_lower_bound_instead_of_fabricated_exact_count(self):
        task=self.direct('cloud.legacy');self.service.set_target_runtime_status(self.owner,task['id'],task['targets'][0]['id'],'completed')
        payload,_=export_workspace(self.db,self.owner,self.root);data=decode_workspace(payload);data['tables'].pop('split_admission_totals')
        raw=json.dumps(data).encode();old={'format':1,'encoding':'zlib+base64','sha256':hashlib.sha256(raw).hexdigest(),'data':base64.b64encode(zlib.compress(raw)).decode()}
        destination=Database(self.root/'old.db');destination.initialize();service=CoreService(destination);owner=service.register_user('old-destination',PASSWORD)['id']
        import_workspace(destination,owner,self.root/'old-assets',old)
        with destination.read() as c:
            row=c.execute('SELECT * FROM split_admission_totals WHERE owner_user_id=?',(owner,)).fetchone()
            self.assertEqual(1,row['successful_adds']);self.assertEqual(0,row['history_complete'])

    def test_restart_keeps_completed_targets_and_checkpoints_immutable(self):
        task=self.direct('restart.done'); target=task['targets'][0]
        self.service.upsert_checkpoint(self.owner,task['id'],target['id'],mode='followers',stage='mode_completed',cursor={},counters={'saved':2},recoverable=False)
        self.service.set_target_runtime_status(self.owner,task['id'],target['id'],'completed')
        self.service.set_task_runtime_status(self.owner,task['id'],'completed')
        self.service.control_task(self.owner,task['id'],'restart')
        result=self.service.get_task(self.owner,task['id'])
        self.assertEqual('completed',result['targets'][0]['status'])
        self.assertEqual('mode_completed',self.service.get_checkpoint(self.owner,task['id'],target['id'],'followers')['stage'])
        self.assertEqual(1,self.totals('restart.done')['successful_adds'])
        self.assertEqual(1,self.report()['total'])

    def test_direct_delete_unstarted_target_does_not_offer_impossible_recovery(self):
        task=self.direct('deleted.unstarted'); target=task['targets'][0]
        self.service.delete_task_target(self.owner,task['id'],target['id'],dismiss=True)
        result=self.add('deleted.unstarted',allow_completed=True)
        self.assertEqual(1,result['accepted_count'])
        with self.assertRaisesRegex(ConflictError,'无法恢复'):
            self.service.retry_task_target(self.owner,task['id'],target['id'])
        self.assertEqual(2,self.totals('deleted.unstarted')['successful_adds'])
        self.assertTrue(self.service.check_global_dedupe('deleted.unstarted')['seen'])

    def test_http_force_payload_and_report_fields_are_enforced(self):
        from fastapi.testclient import TestClient
        from app.config import Settings
        from app.main import create_app
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from test_workbench_newgen import EmptyBitBrowser
        task=self.direct('http.finished');self.service.set_target_runtime_status(self.owner,task['id'],task['targets'][0]['id'],'completed')
        settings=Settings(startup_token='r55-api-startup-token-123456',database_path=self.db.path,data_dir=self.root)
        app=create_app(settings,database=self.db,bitbrowser=EmptyBitBrowser())
        headers={'X-Startup-Token':settings.startup_token,'Authorization':'Bearer '+self.service.login('r55-count-owner',PASSWORD)['token']}
        with TestClient(app) as client:
            for force in (False,True):
                response=client.post('/api/workbench/commands',headers=headers,json={'type':'split_waiting_add','payload':{'targets':['HTTP.FINISHED'],'allow_completed_targets':force}})
                self.assertEqual(200,response.status_code,response.text)
                self.assertEqual(int(force),response.json()['result']['accepted_count'])
                if not force:self.assertEqual('split_already_executed',response.json()['result']['duplicates'][0]['reason'])
            now=datetime.now(timezone.utc);start=now.replace(hour=0,minute=0,second=0,microsecond=0)
            response=client.post('/api/reports/split-review',headers=headers,json={'start':start.isoformat(),'end':(start+timedelta(days=1)).isoformat(),'utc_offset_minutes':0})
            self.assertEqual(200,response.status_code,response.text)
            row=response.json()['items'][0]
            self.assertEqual(2,row['split_count']);self.assertEqual(2,row['split_count_recorded']);self.assertTrue(row['split_count_complete'])

    def test_completion_date_and_window_survive_archive_late_callbacks_and_checkpoint(self):
        task=self.direct('stable.date'); target=task['targets'][0]
        self.service.set_target_runtime_status(self.owner,task['id'],target['id'],'completed',window_id='finished-window')
        initial=self.report()['items'][0]
        self.service.archive_completed_task_target_from_list(self.owner,task['id'],target['id'])
        self.service.set_target_runtime_status(self.owner,task['id'],target['id'],'completed',window_id='late-window')
        self.service.upsert_checkpoint(self.owner,task['id'],target['id'],mode='followers',stage='mode_completed',cursor={},counters={'saved':0},recoverable=False)
        with self.db.write() as c:
            c.execute("UPDATE task_targets SET updated_at='2099-01-01T00:00:00+00:00' WHERE id=?",(target['id'],))
        final=self.report()['items'][0]
        self.assertEqual(initial['completed_at'],final['completed_at'])
        self.assertEqual('finished-window',final['source_window_id'])
        self.assertEqual(1,final['split_count'])

    def test_completion_fact_survives_restart_and_does_not_reappear_after_seven_days(self):
        task=self.direct('old.fact'); target=task['targets'][0]
        old=(datetime.now(timezone.utc)-timedelta(days=20)).isoformat()
        with self.db.write() as c:
            c.execute("UPDATE task_targets SET status='completed',updated_at=? WHERE id=?",(old,target['id']))
        self.service.archive_completed_task_target_from_list(self.owner,task['id'],target['id'])
        self.db.initialize()
        self.assertEqual(0,self.report()['total'])
        with self.db.read() as c:
            self.assertEqual(old,c.execute('SELECT completed_at FROM split_completed_targets WHERE target_id=?',(target['id'],)).fetchone()[0])
        self.assertEqual(0,self.add('old.fact')['accepted_count'])
        self.assertEqual(1,self.add('old.fact',allow_completed=True)['accepted_count'])

    def test_old_cloud_completion_prefers_immutable_history_over_changed_updated_at(self):
        _,task,target=self.live('cloud.history.date',window_id='original-window')
        with self.assertRaises(ConflictError) as rejected:
            self.service.set_target_runtime_status(self.owner,task['id'],target['id'],'completed',window_id='foreign-window')
        self.assertEqual('target_owned_by_other_window',rejected.exception.details['reason'])
        with self.db.read() as c:
            source=c.execute('SELECT status,current_window_id FROM task_targets WHERE id=?',(target['id'],)).fetchone()
            self.assertEqual(('running','original-window'),tuple(source))
            self.assertIsNone(c.execute('SELECT completed_at FROM split_candidate_history WHERE source_target_id=?',(target['id'],)).fetchone())
        self.service.set_target_runtime_status(self.owner,task['id'],target['id'],'completed',window_id='original-window')
        with self.db.read() as c:
            expected=c.execute('SELECT completed_at FROM split_candidate_history WHERE source_target_id=?',(target['id'],)).fetchone()[0]
        self.service.archive_completed_task_target_from_list(self.owner,task['id'],target['id'])
        payload,_=export_workspace(self.db,self.owner,self.root);data=decode_workspace(payload)
        data['tables'].pop('split_completed_targets'); data['tables'].pop('split_admission_totals')
        for row in data['tables']['task_targets']:
            row['updated_at']='2099-01-01T00:00:00+00:00'
        raw=json.dumps(data).encode();old={'format':1,'encoding':'zlib+base64','sha256':hashlib.sha256(raw).hexdigest(),'data':base64.b64encode(zlib.compress(raw)).decode()}
        destination=Database(self.root/'old-completion.db');destination.initialize();service=CoreService(destination)
        owner=service.register_user('old-date-destination',PASSWORD)['id'];import_workspace(destination,owner,self.root/'date-assets',old)
        with destination.read() as c:
            row=c.execute('SELECT * FROM split_completed_targets WHERE target_id=?',(target['id'],)).fetchone()
            self.assertEqual(expected,row['completed_at']);self.assertEqual('original-window',row['source_window_id'])
            count=c.execute('SELECT successful_adds FROM split_admission_totals WHERE owner_user_id=?',(owner,)).fetchone()[0]
            self.assertEqual(1,count)  # history+target+fact represent one generation

if __name__=='__main__': unittest.main()
