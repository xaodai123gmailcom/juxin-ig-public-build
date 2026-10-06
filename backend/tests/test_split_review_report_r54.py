from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.errors import ValidationError
from app.service import CoreService
from app.work_reports import split_review_report

NOW = datetime(2026, 9, 23, 8, tzinfo=timezone.utc)
PASSWORD = 'correct horse battery staple'


class SplitReviewReportR54Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / 'collector.db'); self.db.initialize()
        self.service = CoreService(self.db, session_hours=1)
        self.owner = self.service.register_user('split-review-owner', PASSWORD)['id']
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    def direct(self, name, completed_at, owner=None, status='completed'):
        self.serial += 1
        owner = owner or self.owner
        task_id, target_id = f'task-{self.serial}', f'target-{self.serial}'
        with self.db.write() as c:
            c.execute("INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) VALUES(?,?,?,'queued','[\"followers\"]','{}',?,?)",
                      (task_id, owner, name, completed_at, completed_at))
            c.execute("INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,created_at,updated_at) VALUES(?,?,?,?,1,'pending',?,?)",
                      (target_id, task_id, name, name, completed_at, completed_at))
            c.execute('UPDATE task_targets SET status=?,updated_at=? WHERE id=?', (status, completed_at, target_id))
        return target_id, task_id

    def history(self, name, completed_at, target=None, task=None, owner=None, category=None):
        self.serial += 1
        ident = f'history-{self.serial}'
        with self.db.write() as c:
            c.execute("""INSERT INTO split_candidate_history(
              id,owner_user_id,username_norm,username_display,source_target_id,source_task_id,
              source_status,profile_json,completed_at,manual_category_override,created_at,updated_at)
              VALUES(?,?,?,?,?,?,'completed',?,?,?,?,?)""",
              (ident, owner or self.owner, name, name, target, task, json.dumps({'display_name': name}),
               completed_at, category, completed_at, completed_at))
        return ident

    def report(self, start='2026-09-17T00:00:00+08:00', end='2026-09-24T00:00:00+08:00', **kwargs):
        return split_review_report(self.db, self.owner, start, end, now=kwargs.pop('now', NOW),
                                   utc_offset_minutes=kwargs.pop('utc_offset_minutes', 480), **kwargs)

    def test_actual_completed_split_and_direct_target_are_included_once(self):
        target, task = self.direct('split.finished', '2026-09-23T01:00:00+00:00')
        self.history('split.finished', '2026-09-23T01:00:00+00:00', target, task, category='waiting')
        self.direct('direct.finished', '2026-09-22T23:00:00+00:00')
        rows = self.report()['items']
        self.assertEqual(['split.finished', 'direct.finished'], [r['username'] for r in rows])
        self.assertEqual('2026-09-23T01:00:00+00:00', rows[0]['completed_at'])
        self.assertEqual(target, rows[0]['source_target_id'])
        self.assertEqual({'display_name': 'split.finished'}, rows[0]['profile'])
        self.assertEqual({}, rows[1]['profile'])

    def test_manual_completed_label_failure_and_other_owner_never_count(self):
        waiting = self.service.upsert_manual_split_candidates(self.owner, [{'username': 'not.executed'}])[0]
        self.service.set_split_candidate_manual_category(self.owner, [waiting['id']], 'completed')
        self.direct('failed.target', '2026-09-23T01:00:00+00:00', status='failed')
        self.direct('running.target', '2026-09-23T01:00:00+00:00', status='running')
        other = self.service.register_user('other-split-owner', PASSWORD)['id']
        self.direct('foreign.target', '2026-09-23T01:00:00+00:00', owner=other)
        self.history('foreign.history', '2026-09-23T01:00:00+00:00', owner=other)
        self.assertEqual(0, self.report()['total'])

    def test_seven_local_days_boundaries_and_dedup_history_are_retained(self):
        # UTC is still Sept16 at the first retained +08 local midnight.
        inside = '2026-09-16T16:00:00.000+00:00'
        outside = '2026-09-16T15:59:59.999+00:00'
        self.direct('earliest.retained', inside)
        old_target, _ = self.direct('old.completed', outside)
        self.history('old.history', outside)
        self.service.claim_workbench_identity(self.owner, username='old.identity', source='followers')
        before = self.service.get_workbench_dedupe_stats(self.owner)
        self.assertEqual(['earliest.retained'], [r['username'] for r in self.report()['items']])
        with self.db.read() as c:
            self.assertEqual('completed', c.execute('SELECT status FROM task_targets WHERE id=?', (old_target,)).fetchone()[0])
            self.assertEqual(1, c.execute('SELECT COUNT(*) FROM split_candidate_history').fetchone()[0])
        self.assertTrue(self.service.check_global_dedupe('old.identity')['seen'])
        self.assertEqual(before, self.service.get_workbench_dedupe_stats(self.owner))
        self.assertEqual(1, self.service.check_completed_targets(self.owner, ['old.completed'])['completed_count'])

    def test_calendar_date_uses_explicit_client_zone_not_core_utc_date(self):
        self.direct('local.today', '2026-09-23T23:30:00+00:00')
        near_midnight = datetime(2026, 9, 23, 23, 59, tzinfo=timezone.utc)
        page = self.report('2026-09-24T00:00:00+08:00', '2026-09-25T00:00:00+08:00', now=near_midnight)
        self.assertEqual(1, page['total'])
        self.direct('west.today', '2026-09-24T01:00:00+00:00')
        west = self.report('2026-09-23T00:00:00-07:00', '2026-09-24T00:00:00-07:00',
                           now=datetime(2026, 9, 24, 1, 30, tzinfo=timezone.utc), utc_offset_minutes=-420)
        self.assertEqual(2, west['total'])

    def test_dst_seven_calendar_days_can_be_169_hours(self):
        self.direct('dst.oldest', '2026-10-27T04:00:00+00:00')
        result = self.report('2026-10-27T00:00:00-04:00', '2026-11-03T00:00:00-05:00',
                             now=datetime(2026, 11, 2, 20, tzinfo=timezone.utc), utc_offset_minutes=-300)
        self.assertEqual(1, result['total'])

    def test_retention_uses_current_offset_when_selected_day_was_before_dst_change(self):
        self.direct('dst.seventh.day', '2026-10-27T07:00:00+00:00')
        result = self.report('2026-10-27T00:00:00-07:00', '2026-10-28T00:00:00-07:00',
                             now=datetime.fromisoformat('2026-11-02T23:30:00-08:00'),
                             utc_offset_minutes=-480)
        self.assertEqual(1, result['total'])

    def test_pagination_is_bounded_and_frozen_end_excludes_new_completion(self):
        for number in range(5):
            self.direct(f'page.{number}', f'2026-09-23T0{number}:00:00+00:00')
        options = {'start': '2026-09-23T00:00:00+08:00', 'end': '2026-09-23T13:00:00+08:00', 'limit': 2}
        first = self.report(**options)
        self.direct('new.after.refresh', '2026-09-23T06:00:00+00:00')
        second = self.report(**options, offset=2)
        third = self.report(**options, offset=4)
        self.assertEqual(5, second['total'])
        self.assertTrue(first['has_more']); self.assertTrue(second['has_more']); self.assertFalse(third['has_more'])
        self.assertEqual(5, len({row['id'] for page in (first, second, third) for row in page['items']}))
        self.assertNotIn('new.after.refresh', [r['username'] for r in first['items'] + second['items'] + third['items']])

    def test_repeated_successful_generations_remain_separate_completions(self):
        self.direct('repeat.name', '2026-09-22T01:00:00+00:00')
        self.direct('repeat.name', '2026-09-23T01:00:00+00:00')
        self.assertEqual(2, self.report()['total'])

    def test_stale_future_naive_and_oversized_ranges_are_rejected(self):
        bad = [
            ('2026-09-16T00:00:00+08:00', '2026-09-24T00:00:00+08:00'),
            ('2026-09-15T00:00:00+08:00', '2026-09-16T00:00:00+08:00'),
            ('2026-09-24T00:00:00+08:00', '2026-09-25T00:00:00+08:00'),
            ('2026-09-23T00:00:00', '2026-09-24T00:00:00'),
            ('2026-09-23T01:00:00+08:00', '2026-09-24T00:00:00+08:00'),
        ]
        for start, end in bad:
            with self.subTest(start=start), self.assertRaises(ValidationError):
                self.report(start, end)
        with self.assertRaises(ValidationError):
            self.report(limit=501)


if __name__ == '__main__':
    unittest.main()
