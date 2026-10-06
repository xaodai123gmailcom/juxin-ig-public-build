"""Work-report split counts are durable completed generations, not admissions."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.service import CoreService
from app.work_reports import METRICS, work_report


PASSWORD = 'r56 work report split test password'
MONTH = ('2026-09-01T00:00:00+08:00', '2026-10-01T00:00:00+08:00')
DAY = ('2026-09-21T00:00:00+08:00', '2026-09-22T00:00:00+08:00')


class WorkReportSplitR56Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / 'reports.db')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('r56-report-owner', PASSWORD)['id']
        self.other = self.service.register_user('r56-report-other', PASSWORD)['id']
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    def direct(self, name, when, *, owner=None, window=None, status='completed'):
        """Use the production completion trigger to create the permanent fact."""
        self.serial += 1
        task, target = f'task-{self.serial}', f'target-{self.serial}'
        with self.db.write() as c:
            c.execute("""INSERT INTO tasks(
                id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at)
                VALUES(?,?,?,'queued','["followers"]','{}',?,?)""",
                (task, owner or self.owner, name, when, when))
            c.execute("""INSERT INTO task_targets(
                id,task_id,username_norm,username_display,queue_order,status,
                current_window_id,created_at,updated_at)
                VALUES(?,?,?,?,1,'pending',?,?,?)""",
                (target, task, name, name, window, when, when))
            c.execute('UPDATE task_targets SET status=?,updated_at=? WHERE id=?',
                      (status, when, target))
        return task, target

    def history(self, name, when, *, target=None, owner=None, window=None,
                status='completed', profile=None, category=None):
        self.serial += 1
        ident = f'history-{self.serial}'
        with self.db.write() as c:
            c.execute("""INSERT INTO split_candidate_history(
                id,owner_user_id,username_norm,username_display,source_target_id,
                source_status,source_window_id,profile_json,completed_at,
                manual_category_override,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ident, owner or self.owner, name, name, target, status, window,
                 json.dumps(profile or {}), when, category, when, when))
        return ident

    def report(self, bounds=DAY, *, owner=None):
        result = work_report(self.db, owner or self.owner, *bounds)
        self.assertEqual(result['totals']['split'], sum(row['split'] for row in result['rows']))
        return result

    def test_empty_report_has_split_and_retains_every_existing_metric(self):
        report = self.report()
        self.assertEqual({key: 0 for key in METRICS}, report['totals'])
        self.assertIn('greet', report['totals'])
        self.assertIn('split', report['totals'])
        self.assertEqual([], report['rows'])
        self.assertEqual(0, report['unattributed'])

    def test_day_week_month_share_local_half_open_bounds_without_seven_day_retention(self):
        times = [
            '2026-08-31T15:59:59.999+00:00',  # before September locally
            '2026-08-31T16:00:00.000Z',       # September 1, older than seven days
            '2026-09-20T15:59:59.999Z',       # before selected day/week
            '2026-09-21T00:00:00+08:00',     # inclusive local start
            '2026-09-21T15:59:59.999+00:00',  # last instant of selected day
            '2026-09-22T00:00:00+08:00',     # excluded from selected day
            '2026-09-27T15:59:59.999Z',       # last instant of selected week
            '2026-09-28T00:00:00+08:00',     # excluded from selected week
            '2026-09-30T16:00:00.000Z',      # excluded from September
        ]
        for index, when in enumerate(times):
            if index % 2:
                self.history(f'source.{index}', when)
            else:
                self.direct(f'source.{index}', when)
        self.assertEqual(2, self.report()['totals']['split'])
        self.assertEqual(4, self.report((DAY[0], '2026-09-28T00:00:00+08:00'))['totals']['split'])
        self.assertEqual(7, self.report(MONTH)['totals']['split'])

    def test_western_offset_and_dst_boundary_use_elapsed_instants(self):
        for name, when in (
            ('before', '2026-11-01T06:59:59.999Z'),
            ('start', '2026-11-01T00:00:00-07:00'),
            ('last', '2026-11-02T07:59:59.999Z'),
            ('end', '2026-11-02T00:00:00-08:00'),
        ):
            self.direct(name, when)
        result = self.report(('2026-11-01T00:00:00-07:00', '2026-11-02T00:00:00-08:00'))
        self.assertEqual(2, result['totals']['split'])

    def test_counts_completed_generations_once_across_facts_and_duplicate_history(self):
        when = '2026-09-21T03:00:00Z'
        _, target = self.direct('same.source', when, window='first-window')
        self.history('same.source', when, target=target, window='first-window', category='waiting')
        self.history('same.source', '2026-09-21T04:00:00Z', target=target, window='retry-window')
        self.direct('same.source', when, window='first-window')  # distinct generation
        self.history('same.source', when, target=None)
        self.history('same.source', when, target='')  # unknown targets remain separate
        report = self.report()
        self.assertEqual(4, report['totals']['split'])
        self.assertEqual({'first-window': 2, '': 2}, {row['profile_id']: row['split'] for row in report['rows']})

    def test_first_completion_is_resolved_before_date_filter_and_late_callbacks(self):
        first = '2026-09-01T02:00:00Z'
        _, target = self.direct('first.fact', first, window='original-window')
        self.history('first.fact', '2026-09-21T03:00:00Z', target=target, window='late-window')
        # Legacy history-only duplicate also must not appear again in a later day.
        self.history('history.only', first, target='deleted-target', window='history-window')
        self.history('history.only', '2026-09-21T03:00:00Z', target='deleted-target', window='late-window')
        with self.db.write() as c:
            c.execute("UPDATE task_targets SET status='completed',current_window_id='late-window',updated_at='2026-09-21T04:00:00Z' WHERE id=?", (target,))
            c.execute("UPDATE task_targets SET updated_at='2099-01-01T00:00:00Z' WHERE id=?", (target,))
        self.assertEqual(0, self.report()['totals']['split'])
        report = self.report(('2026-09-01T00:00:00+08:00', '2026-09-02T00:00:00+08:00'))
        self.assertEqual(2, report['totals']['split'])
        self.assertEqual({'original-window', 'history-window'}, {row['profile_id'] for row in report['rows']})
        self.assertEqual(2, self.report(MONTH)['totals']['split'])

    def test_admissions_manual_labels_running_failed_and_candidate_results_are_not_splits(self):
        when = '2026-09-21T03:00:00Z'
        candidate = self.service.upsert_manual_split_candidates(self.owner, [{'username': 'manual.label'}])[0]
        self.service.set_split_candidate_manual_category(self.owner, [candidate['id']], 'completed')
        for status in ('pending', 'running', 'failed'):
            self.direct(f'{status}.target', when, status=status)
            self.history(f'{status}.history', when, status=status, category='completed')
        task, target = self.direct('finished.source', when, status='running')
        with self.db.write() as c:
            c.execute('UPDATE split_admission_totals SET successful_adds=99 WHERE owner_user_id=?', (self.owner,))
        self.service.record_result(self.owner, task, target, username='collected.result',
            instagram_user_id=None, source_mode='followers', visibility='private', profile={},
            screening={}, qualified=None)
        # Persist the observation before completing its source, as the runtime
        # does. A closed source must reject new late observations.
        self.service.set_target_runtime_status(self.owner, task, target, 'completed')
        result = self.report(('2000-01-01T00:00:00Z', '2100-01-01T00:00:00Z'))
        self.assertEqual(1, result['totals']['split'])
        self.assertEqual(1, result['totals']['collection'])

    def test_only_preserved_completion_window_is_used_and_unknown_actors_are_unattributed(self):
        when = '2026-09-21T03:00:00Z'
        _, target = self.direct('known.source', when, window='completion-window')
        self.direct('unknown.source', when)
        self.history('legacy.source', when, window='removed-window', profile={
            'username': 'source.account',
            'executor': {'profile_id': 'old-collector-window', 'username': 'old.collector',
                         'window_name': 'Old collector', 'instagram_user_id': '123'},
        })
        self.history('unknown.history', when, profile={'executor': {'profile_id': 'unproven', 'username': 'unproven'}})
        with self.db.write() as c:
            c.execute("UPDATE task_targets SET current_window_id='new-binding' WHERE id=?", (target,))
        report = self.report()
        self.assertEqual({'completion-window': 1, 'removed-window': 1, '': 2},
                         {row['profile_id']: row['split'] for row in report['rows']})
        self.assertEqual(4, report['unattributed'])
        for row in report['rows']:
            self.assertEqual(('', '', ''), (row['username'], row['window_name'], row['instagram_user_id']))

    def test_owner_filter_precedes_generation_deduplication(self):
        when = '2026-09-21T03:00:00Z'
        _, target = self.direct('own.source', when, window='own-window')
        self.history('own.history', when, window='own-window')
        self.direct('other.source', when, owner=self.other, window='other-window')
        # Even an imported foreign row referencing our id cannot suppress it.
        self.history('foreign.link', '2026-09-01T03:00:00Z', target=target,
                     owner=self.other, window='other-window')
        own = self.report(MONTH)
        other = self.report(MONTH, owner=self.other)
        self.assertEqual(2, own['totals']['split'])
        self.assertEqual(2, other['totals']['split'])
        self.assertEqual({'own-window'}, {row['profile_id'] for row in own['rows']})
        self.assertEqual({'other-window'}, {row['profile_id'] for row in other['rows']})

    def test_archiving_deleting_tasks_and_restart_preserve_split_totals(self):
        when = '2026-09-21T03:00:00Z'
        task, target = self.direct('archived.source', when, window='durable-window')
        self.history('archived.source', when, target=target, window='durable-window')
        baseline = self.report()
        self.service.archive_completed_task_target_from_list(self.owner, task, target)
        self.assertEqual(baseline, self.report())
        with self.db.write() as c:
            c.execute('DELETE FROM tasks WHERE id=?', (task,))
        self.db.initialize()
        self.assertEqual(baseline, self.report())
        with self.db.read() as c:
            self.assertEqual(0, c.execute('SELECT COUNT(*) FROM task_targets').fetchone()[0])
            self.assertEqual(1, c.execute('SELECT COUNT(*) FROM split_completed_targets').fetchone()[0])

    def test_refresh_and_period_changes_do_not_mutate_completion_or_global_dedup_records(self):
        self.service.upsert_manual_split_candidates(self.owner, [{'username': 'queued.identity'}])
        self.service.claim_workbench_identity(self.owner, username='collected.identity', source='followers')
        self.direct('completed.source', '2026-09-21T03:00:00Z')
        self.history('old.history', '2026-09-01T03:00:00Z')
        before_stats = self.service.get_workbench_dedupe_stats(self.owner)
        with self.db.read() as c:
            before = tuple(c.iterdump())
        first = self.report()
        self.assertEqual(2, self.report(MONTH)['totals']['split'])
        self.assertEqual(first, self.report())
        with self.db.read() as c:
            self.assertEqual(before, tuple(c.iterdump()))
        self.assertEqual(before_stats, self.service.get_workbench_dedupe_stats(self.owner))
        self.assertTrue(self.service.check_global_dedupe('queued.identity')['seen'])
        self.assertTrue(self.service.check_global_dedupe('collected.identity')['seen'])

    def test_greeting_success_remains_in_report_detail_with_split_metric(self):
        self.direct('completed.source', '2026-09-21T03:00:00Z')
        campaign = self.service.create_action_campaign(self.owner, operation='greet',
            execution_type='campaign', profile_id='greeting-window', targets=['greet.target'],
            message='hello', interval_min_seconds=0, interval_max_seconds=0, limit_count=1)
        attempt = self.service.start_action_attempt(self.owner, campaign['id'], campaign['targets'][0]['id'],
            details={'executor': {'username': 'greeting.actor', 'window_name': 'Greeting window'}})
        self.service.finish_action_attempt(self.owner, campaign['id'], attempt, status='confirmed', details={})
        report = self.report(('2000-01-01T00:00:00Z', '2100-01-01T00:00:00Z'))
        self.assertEqual(1, report['totals']['split'])
        self.assertEqual(1, report['totals']['greet'])
        row = next(row for row in report['rows'] if row['username'] == 'greeting.actor')
        self.assertEqual(1, row['greet'])
        self.assertEqual(0, row['split'])


if __name__ == '__main__':
    unittest.main()
