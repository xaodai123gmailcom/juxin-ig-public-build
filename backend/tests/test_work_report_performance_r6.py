"""R6 report summaries remain exact, date-indexed, payload-free and live."""
import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.database import Database
from app.service import CoreService
from app.work_reports import work_report, history_totals, _collection_period_sql, _split_work_report_sql, _report_time

DAY = ('2026-10-02T00:00:00Z', '2026-10-03T00:00:00Z')
OLD, NOW = '2026-09-01T12:00:00+00:00', '2026-10-02T12:00:00+00:00'


class WorkReportPerformanceR6Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'reports.db'); self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('report-r6-owner', 'report r6 test password')['id']
        self.other = self.service.register_user('report-r6-other', 'report r6 test password')['id']
        with self.db.write() as c:
            for owner, task, settings in [(self.owner, 'task', '{}'), (self.other, 'foreign', '{}'), (self.owner, 'retired', '{"platform":"facebook"}')]:
                c.execute("INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) VALUES(?,?,?,'queued','[\"followers\"]',?,?,?)", (task, owner, task, settings, OLD, OLD))
                c.execute("INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,created_at,updated_at) VALUES(?,?,?,?,1,'pending',?,?)", (task, task, task, task, OLD, OLD))

    def tearDown(self): self.temp.cleanup()

    def record(self, ident, source='candidate', when=NOW, owner=None, task='task', actor='actor'):
        owner = owner or self.owner
        profile = json.dumps({'executor': {'profile_id': 'window', 'username': actor, 'window_name': 'Captured window'}, 'padding': 'x' * 4096})
        with self.db.write() as c:
            c.execute('INSERT OR IGNORE INTO instagram_accounts(id,current_username_norm,current_username_display,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)', (ident, ident, ident, when, when))
            if source == 'candidate':
                c.execute("INSERT INTO workbench_candidates(id,owner_user_id,account_id,visibility,profile_json,created_at,updated_at) VALUES(?,?,?,'private',?,?,?)", (ident, owner, ident, profile, when, when))
            elif source == 'exclusion':
                c.execute("INSERT INTO workbench_collection_exclusions(id,account_id,owner_user_id,username_display,reason_code,reason,profile_snapshot_json,excluded_at) VALUES(?,?,?,?,'not_us','fixture',?,?)", (ident, ident, owner, ident, profile, when))
            else:
                c.execute("INSERT INTO task_results(id,task_id,target_id,account_id,sources_json,visibility,profile_json,screening_json,created_at,updated_at) VALUES(?,?,?,?,'[]','private',?,'{}',?,?)", (task + ident, task, task, ident, profile, when, when))

    def summary(self, *bounds): return work_report(self.db, self.owner, *(bounds or DAY), platform='instagram', summary_only=True)

    def test_lifetime_first_observation_not_later_copies_and_owner_scope(self):
        for old, later, ident in [('candidate', 'task', 'old-candidate'), ('task', 'candidate', 'old-task'), ('exclusion', 'task', 'old-exclusion')]:
            self.record(ident, old, OLD); self.record(ident, later)
        self.record('new-duplicate', 'task'); self.record('new-duplicate', 'candidate')
        self.record('new-exclusion', 'exclusion')
        self.record('foreign-owner', 'candidate', owner=self.other)
        self.record('opposite-task', 'task', OLD, task='retired'); self.record('opposite-task', 'candidate')
        self.record('foreign-first', 'task', OLD, task='foreign'); self.record('foreign-first', 'candidate')
        self.record('fb:retired', 'candidate')
        summary = self.summary(); full = work_report(self.db, self.owner, *DAY, platform='instagram')
        self.assertEqual(4, summary['totals']['collection'])
        self.assertEqual({k: full['totals'][k] for k in summary['totals']}, summary['totals'])
        self.assertEqual({'platform', 'start', 'end', 'totals'}, set(summary))
        self.assertEqual(4, sum(row['collection'] for row in full['rows']))
        totals = history_totals(self.db, self.owner, *DAY, platform='instagram')
        self.assertEqual(7, totals['total_collected']); self.assertEqual(4, totals['today_collected'])

    def test_equivalent_offsets_fractional_before_midnight_and_half_open_end(self):
        bounds = ('2026-10-03T00:00:00+08:00', '2026-10-04T00:00:00+08:00')
        for index, (when, included) in enumerate([
            ('2026-10-02T15:59:59.999+00:00', False),
            ('2026-10-03T00:00:00+08:00', True),
            ('2026-10-02T16:00:00.000Z', True),
            ('2026-10-03T15:59:59.999999+00:00', True),
            ('2026-10-04T00:00:00+08:00', False),
        ]):
            for source in ['candidate', 'task', 'exclusion']:
                self.record(f'{source}-{index}', source, when)
        summary = self.summary(*bounds); full = work_report(self.db, self.owner, *bounds)
        self.assertEqual(9, summary['totals']['collection'])
        self.assertEqual(9, full['totals']['collection'])
        # Earlier textual date is actually a later instant: UTC ordering wins.
        self.record('offset-first', 'task', '2026-10-03T01:00:00+09:00')
        self.record('offset-first', 'candidate', '2026-10-02T16:30:00Z')
        self.assertEqual(10, self.summary(*bounds)['totals']['collection'])

    def test_microsecond_fringe_keeps_earlier_first_evidence_out_of_next_day(self):
        edge = '2026-10-01T23:59:59.999999Z'
        self.record('earlier-fringe', 'task', edge)
        self.record('earlier-fringe', 'candidate', NOW)
        with self.db.write() as c:
            c.execute("INSERT INTO split_completed_targets(target_id,owner_user_id,username_norm,username_display,source_task_id,completed_at) VALUES('micro-split',?,'source','source','task',?)", (self.owner, edge))
            c.execute("INSERT INTO split_candidate_history(id,owner_user_id,username_norm,username_display,source_target_id,source_status,completed_at,created_at,updated_at) VALUES('later-copy',?,'source','source','micro-split','completed',?,?,?)", (self.owner, NOW, NOW, NOW))
        self.assertEqual(0, self.summary()['totals']['collection'])
        self.assertEqual(0, self.summary()['totals']['split'])
        previous = self.summary('2026-10-01T00:00:00Z', '2026-10-02T00:00:00Z')
        self.assertEqual(1, previous['totals']['collection']); self.assertEqual(1, previous['totals']['split'])
        self.assertEqual(0, history_totals(self.db, self.owner, *DAY)['today_collected'])

    def test_four_positive_metrics_match_full_export_and_ignore_non_successes(self):
        self.record('collection')
        with self.db.write() as c:
            for i in range(2):
                c.execute("INSERT INTO split_completed_targets(target_id,owner_user_id,username_norm,username_display,source_task_id,completed_at) VALUES(?,?,'source','source','task',?)", (f'split-{i}', self.owner, NOW))
            for i, added in enumerate([0, 3, 4]):
                c.execute("INSERT INTO follow_monitor_rounds(owner_user_id,batch_id,profile_id,owner_username,actual_count,first_read_count,added_count,repeat_count,unfollow_count,checked_at) VALUES(?,?,'window','actor',20,20,?,0,0,?)", (self.owner, str(i), added, NOW))
        for operation in ['follow', 'greet']:
            campaign = self.service.create_action_campaign(self.owner, operation=operation, execution_type='campaign', profile_id='window', targets=[operation + '.' + status for status in ['confirmed', 'already_done', 'failed']], message='fixture', interval_min_seconds=0, interval_max_seconds=0, limit_count=3)
            for target, status in zip(campaign['targets'], ['confirmed', 'already_done', 'failed']):
                attempt = self.service.start_action_attempt(self.owner, campaign['id'], target['id'], details={'executor': {'username': 'actor'}})
                self.service.finish_action_attempt(self.owner, campaign['id'], attempt, status=status, details={})
        bounds = ('2000-01-01T00:00:00Z', '2100-01-01T00:00:00Z')
        summary = self.summary(*bounds); full = work_report(self.db, self.owner, *bounds)
        self.assertEqual({'collection': 1, 'follow': 1, 'split': 2, 'added': 7}, summary['totals'])
        self.assertEqual({k: full['totals'][k] for k in summary['totals']}, summary['totals'])
        self.assertEqual(1, full['totals']['greet']); self.assertEqual(3, full['totals']['check'])

    def test_summary_never_reads_profile_or_review_payloads(self):
        for source in ['candidate', 'task', 'exclusion']: self.record(source, source)
        read = self.db.read
        forbidden = {'profile_json', 'profile_snapshot_json', 'review_cache_json', 'screening_json', 'details_json', 'result_json'}
        @contextmanager
        def guarded():
            with read() as c:
                def authorize(action, table, column, *_):
                    return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and column in forbidden else sqlite3.SQLITE_OK
                c.set_authorizer(authorize)
                yield c
        self.db.read = guarded
        self.assertEqual(3, self.summary()['totals']['collection'])

    def test_summary_and_full_export_have_no_record_cap(self):
        with self.db.write() as c:
            for i in range(2103):
                ident = f'account-{i}'
                c.execute('INSERT INTO instagram_accounts VALUES(?,NULL,?,?,?,?)', (ident, ident, ident, NOW, NOW))
                c.execute("INSERT INTO workbench_candidates(id,owner_user_id,account_id,visibility,profile_json,created_at,updated_at) VALUES(?,?,?,'private',?,?,?)", (ident, self.owner, ident, json.dumps({'executor': {'username': ident}}), NOW, NOW))
        with patch('app.work_reports._report_time', wraps=_report_time) as normalize:
            self.assertEqual(2103, self.summary()['totals']['collection'])
            # Interior timestamps do not cross the SQLite/Python boundary per
            # evidence row; only the two request bounds need normalization.
            self.assertEqual(2, normalize.call_count)
        full = work_report(self.db, self.owner, *DAY)
        self.assertEqual(2103, len(full['rows'])); self.assertEqual(2103, full['totals']['collection'])

    def test_refresh_has_no_stale_cache_and_all_metrics_share_a_wal_snapshot(self):
        read = self.db.read
        fired = False
        def write_new_evidence():
            self.record('concurrent')
            with self.db.write() as c:
                c.execute("INSERT INTO follow_monitor_rounds(owner_user_id,batch_id,profile_id,owner_username,actual_count,first_read_count,added_count,repeat_count,unfollow_count,checked_at) VALUES(?,'new','window','actor',10,10,3,0,0,?)", (self.owner, NOW))
        @contextmanager
        def interleaved():
            with read() as c:
                class Connection:
                    def create_function(_self, *args, **kwargs): return c.create_function(*args, **kwargs)
                    def execute(_self, sql, *args):
                        nonlocal fired
                        result = c.execute(sql, *args)
                        if not fired and 'SELECT COUNT(*) FROM eligible' in sql:
                            fired = True
                            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                                pool.submit(write_new_evidence).result(timeout=5)
                        return result
                yield Connection()
        self.db.read = interleaved
        first = self.summary()
        self.assertTrue(fired)
        self.assertEqual(0, first['totals']['collection']); self.assertEqual(0, first['totals']['added'])
        second = self.summary()
        self.assertEqual(1, second['totals']['collection']); self.assertEqual(3, second['totals']['added'])
        self.db.read = read
        self.db.initialize()
        self.assertEqual(second, self.summary())

    def test_reports_are_read_only_and_use_period_and_identity_indexes(self):
        self.record('kept', 'candidate')
        with self.db.read() as c:
            before = tuple(c.iterdump())
            c.create_function('report_time', 1, _report_time, deterministic=True)
            parameters = {'owner': self.owner, 'start': DAY[0], 'end': DAY[1], 'exact_start': _report_time(DAY[0]), 'exact_end': _report_time(DAY[1])}
            plan = '\n'.join(row[3] for row in c.execute('EXPLAIN QUERY PLAN ' + _collection_period_sql('instagram', identities_only=True) + 'SELECT COUNT(*) FROM eligible', parameters))
            for index in ['idx_results_report_period', 'idx_results_report_identity', 'idx_candidates_report_period', 'idx_exclusions_report_period']:
                self.assertIn(index, plan)
            split_plan = '\n'.join(row[3] for row in c.execute('EXPLAIN QUERY PLAN ' + _split_work_report_sql('instagram') + 'SELECT COUNT(*) FROM first_completions WHERE rank=1', parameters))
            for index in ['idx_split_history_report_period', 'idx_split_history_report_identity', 'idx_split_completed_targets_report_period']:
                self.assertIn(index, split_plan)
        self.summary(); work_report(self.db, self.owner, *DAY); history_totals(self.db, self.owner, *DAY)
        with self.db.read() as c: self.assertEqual(before, tuple(c.iterdump()))

    def test_summary_http_validation_auth_and_owner_injection(self):
        from fastapi.testclient import TestClient
        from app.config import Settings
        from app.main import create_app
        app = create_app(Settings('r6-report-fixture-token', self.db.path, Path(self.temp.name)), database=self.db)
        self.record('mine'); self.record('foreign', owner=self.other)
        headers = {'X-Startup-Token': 'r6-report-fixture-token'}
        body = {'kind': 'activity', 'start': DAY[0], 'end': DAY[1], 'platform': 'instagram', 'summary_only': True, 'owner_user_id': self.other}
        with TestClient(app) as client:
            self.assertEqual(401, client.post('/api/reports/query', json=body, headers=headers).status_code)
            headers['Authorization'] = 'Bearer ' + self.service.login('report-r6-owner', 'report r6 test password')['token']
            response = client.post('/api/reports/query', json=body, headers=headers)
            self.assertEqual(200, response.status_code); self.assertEqual(1, response.json()['totals']['collection'])
            self.assertNotIn('rows', response.json())
            for bad in ['true', 1, None, {}, []]:
                self.assertGreaterEqual(client.post('/api/reports/query', json={**body, 'summary_only': bad}, headers=headers).status_code, 400)
            self.assertGreaterEqual(client.post('/api/reports/query', json={**body, 'kind': 'history'}, headers=headers).status_code, 400)
            self.assertGreaterEqual(client.post('/api/reports/query', json={**body, 'platform': 'facebook'}, headers=headers).status_code, 400)

if __name__ == '__main__': unittest.main()
