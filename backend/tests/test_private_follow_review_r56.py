"""Private follow audits use durable confirmed evidence and local date totals."""
import base64
import hashlib
import json
import sqlite3
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.cloud_workspace import decode_workspace, export_workspace, import_workspace
from app.database import Database
from app.errors import ValidationError
from app.private_follow_reviews import capture_private_follow_completions
from app.service import CoreService
from app.work_reports import private_follow_review_report, split_review_report

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
PASSWORD = 'private follow review test password'


class PrivateFollowReviewR56Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / 'private.db')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('private-report-owner', PASSWORD)['id']
        self.other = self.service.register_user('private-report-other', PASSWORD)['id']

    def tearDown(self):
        self.temp.cleanup()

    def follow(self, name, when='2026-09-24T02:00:00Z', *, owner=None, visibility='private',
               status='confirmed', confirmation='requested', profile=None, executor=None):
        owner = owner or self.owner
        candidate = None
        if visibility is not None:
            claim = self.service.claim_workbench_identity(owner, username=name, source='followers')
            candidate = self.service.create_workbench_candidate(owner, claim_id=claim['claim_id'],
                username=name, visibility=visibility, profile=profile or {}, screening={}, review_cache={})
        campaign = self.service.create_action_campaign(owner, operation='follow', execution_type='campaign',
            profile_id='follow-window', targets=[name], message=None, interval_min_seconds=0,
            interval_max_seconds=0, limit_count=1)
        attempt = self.service.start_action_attempt(owner, campaign['id'], campaign['targets'][0]['id'],
            details={'executor': executor or {'profile_id': 'follow-window', 'window_name': 'Saved window', 'username': 'executor'}})
        if status is not None:
            with patch('app.service.isoformat', return_value=when):
                self.service.finish_action_attempt(owner, campaign['id'], attempt,
                    status=status, details={'confirmation': confirmation})
        return campaign, attempt, candidate

    def report(self, start='2026-09-18T00:00:00+08:00', end='2026-09-25T00:00:00+08:00', **options):
        return private_follow_review_report(self.db, options.pop('owner', self.owner), start, end,
            utc_offset_minutes=options.pop('utc_offset_minutes', 480), now=options.pop('now', NOW), **options)

    def test_only_confirmed_private_successes_count_not_already_done_or_pending(self):
        self.follow('requested.private', profile={'followers': 12, 'following': 0, 'posts': None})
        self.follow('following.private', confirmation='已关注')
        for status in ('already_done', 'failed', 'unknown', None):
            self.follow('state.' + (status or 'pending'), status=status)
        self.follow('confirmed.public', visibility='public')
        self.follow('unknown.privacy', visibility=None)
        self.follow('foreign.private', owner=self.other)
        report = self.report()
        self.assertEqual(2, report['total'])
        self.assertEqual({'requested', 'following'}, {row['follow_state'] for row in report['items']})
        self.assertTrue(all(row['status'] == 'confirmed' for row in report['items']))
        requested = next(row for row in report['items'] if row['username'] == 'requested.private')
        self.assertEqual((12, 0, None), tuple(requested[key] for key in ('followers', 'following', 'posts')))
        self.assertNotIn('processed_count', requested)
        self.assertEqual(('follow-window', 'Saved window', 'executor'),
            (requested['source_window_id'], requested['window_name'], requested['executor_username']))
        self.assertEqual(1, self.report(owner=self.other)['total'])

    def test_seven_local_days_boundaries_and_selected_day_counts_are_unpaginated(self):
        self.follow('before.seven', '2026-09-17T15:59:59.999Z')
        self.follow('first.seven', '2026-09-18T00:00:00+08:00')
        self.follow('first.today', '2026-09-23T16:00:00.000Z')
        self.follow('middle.today', '2026-09-24T10:00:00Z')
        self.follow('end.today', '2026-09-25T00:00:00+08:00')
        first = self.report('2026-09-24T00:00:00+08:00', limit=1)
        second = self.report('2026-09-24T00:00:00+08:00', limit=1, offset=1)
        self.assertEqual(2, first['total'])
        self.assertTrue(first['has_more']); self.assertFalse(second['has_more'])
        self.assertEqual(2, len({first['items'][0]['id'], second['items'][0]['id']}))
        self.assertEqual({'2026-09-18': 1, '2026-09-19': 0, '2026-09-20': 0,
            '2026-09-21': 0, '2026-09-22': 0, '2026-09-23': 0, '2026-09-24': 2}, first['daily_counts'])

    def test_frozen_today_end_applies_to_list_and_daily_total(self):
        self.follow('before.freeze', '2026-09-24T09:59:59.999Z')
        self.follow('at.freeze', '2026-09-24T10:00:00Z')
        result = self.report('2026-09-24T00:00:00+08:00', '2026-09-24T18:00:00+08:00')
        self.assertEqual(1, result['total'])
        self.assertEqual(1, result['daily_counts']['2026-09-24'])

    def test_yesterday_selection_keeps_today_counts_with_newest_first_bounds(self):
        self.follow('yesterday.private', '2026-09-23T02:00:00Z')
        self.follow('today.private', '2026-09-24T02:00:00Z')
        start = datetime.fromisoformat('2026-09-18T00:00:00+08:00')
        bounds = [{'key': (start + timedelta(days=index)).date().isoformat(),
            'start': (start + timedelta(days=index)).isoformat(),
            'end': (start + timedelta(days=index + 1)).isoformat()} for index in range(7)]
        bounds[-1]['end'] = '2026-09-24T20:00:00+08:00'
        for explicit in (None, list(reversed(bounds))):
            result = self.report('2026-09-23T00:00:00+08:00', '2026-09-24T00:00:00+08:00', daily_bounds=explicit)
            self.assertEqual(1, result['total'])
            self.assertEqual(1, result['daily_counts']['2026-09-24'])

    def test_start_snapshot_survives_profile_removal_and_campaign_deletion(self):
        campaign, attempt, candidate = self.follow('durable.private', status=None,
            profile={'followers': 2, 'following': 3, 'posts': 4})
        with self.db.write() as c:
            c.execute('DELETE FROM workbench_candidates WHERE id=?', (candidate['id'],))
        with patch('app.service.isoformat', return_value='2026-09-24T02:00:00Z'):
            self.service.finish_action_attempt(self.owner, campaign['id'], attempt,
                status='confirmed', details={'confirmation': '已发送'})
        expected = self.report()['items']
        with self.db.write() as c:
            c.execute('DELETE FROM action_campaigns WHERE id=?', (campaign['id'],))
        self.db.initialize()
        self.assertEqual(expected, self.report()['items'])
        self.assertEqual((2, 3, 4), tuple(expected[0][key] for key in ('followers', 'following', 'posts')))

    def test_unknown_counts_and_manual_confirmation_do_not_claim_acceptance(self):
        campaign, _, _ = self.follow('manual.private', status='unknown', confirmation='',
            profile={'followers': True, 'following': -1, 'posts': 'unknown'})
        with patch('app.service.isoformat', return_value='2026-09-24T02:00:00Z'):
            self.service.resolve_unknown_action(self.owner, campaign['id'], campaign['targets'][0]['id'], outcome='completed')
        item = self.report()['items'][0]
        self.assertEqual('unknown', item['follow_state'])
        self.assertEqual((None, None, None), tuple(item[key] for key in ('followers', 'following', 'posts')))

    def test_legacy_backfill_is_idempotent_and_keeps_saved_first_date(self):
        self.follow('legacy.private', '2026-09-20T04:00:00Z')
        self.follow('legacy.preexisting', status='already_done')
        with self.db.write() as c:
            c.execute('DROP TRIGGER trg_private_follow_completion_no_delete')
            c.execute('DELETE FROM private_follow_completions')
            c.execute('DELETE FROM schema_migrations WHERE version=36')
        self.db.initialize(); self.db.initialize()
        result = self.report()
        self.assertEqual(1, result['total'])
        self.assertEqual('2026-09-20T04:00:00Z', result['items'][0]['completed_at'])

    def test_current_and_legacy_cloud_backup_preserve_evidence(self):
        self.follow('cloud.private', profile={'followers': 1, 'following': 0, 'posts': 3})
        payload, _ = export_workspace(self.db, self.owner, self.root)
        for legacy in (False, True):
            imported = payload
            if legacy:
                data = decode_workspace(payload)
                data['tables'].pop('private_follow_completions')
                raw = json.dumps(data).encode()
                imported = {'format': 1, 'encoding': 'zlib+base64', 'sha256': hashlib.sha256(raw).hexdigest(),
                    'data': base64.b64encode(zlib.compress(raw)).decode()}
            destination = Database(self.root / f'cloud-{legacy}.db'); destination.initialize()
            owner = CoreService(destination).register_user(f'cloud-{legacy}', PASSWORD)['id']
            import_workspace(destination, owner, self.root / f'assets-{legacy}', imported)
            result = private_follow_review_report(destination, owner, '2026-09-18T00:00:00+08:00',
                '2026-09-25T00:00:00+08:00', utc_offset_minutes=480, now=NOW)
            self.assertEqual(self.report()['items'], result['items'])

    def test_daily_bounds_support_dst_and_match_split_review_counts(self):
        zone = ZoneInfo('America/New_York')
        start = datetime(2026, 10, 28, tzinfo=zone)
        bounds = [{'key': (start + timedelta(days=index)).date().isoformat(),
            'start': (start + timedelta(days=index)).isoformat(),
            'end': (start + timedelta(days=index + 1)).isoformat()} for index in range(7)]
        when = '2026-11-01T04:30:00Z'  # 00:30 before offset changed, belongs to November 1.
        self.follow('dst.private', when)
        task = self.service.create_task(self.owner, name='dst split', modes=['followers'], targets=['dst.source'], settings={})
        with patch('app.service.isoformat', return_value=when):
            self.service.set_target_runtime_status(self.owner, task['id'], task['targets'][0]['id'], 'completed')
        options = {'utc_offset_minutes': -300, 'now': datetime(2026, 11, 3, 18, tzinfo=timezone.utc), 'daily_bounds': bounds}
        first, end = bounds[0]['start'], bounds[-1]['end']
        private = self.report(first, end, **options)
        split = split_review_report(self.db, self.owner, first, end, **options)
        self.assertEqual(1, private['daily_counts']['2026-11-01'])
        self.assertEqual(0, private['daily_counts']['2026-10-31'])
        self.assertEqual(private['daily_counts'], split['daily_counts'])
        invalid = [dict(row) for row in bounds]; invalid[2]['end'] = invalid[3]['end']
        with self.assertRaises(ValidationError):
            self.report(first, end, **{**options, 'daily_bounds': invalid})

    def test_report_rejects_stale_naive_future_and_bad_pagination_without_mutation(self):
        self.follow('readonly.private')
        with self.db.read() as c:
            before = tuple(c.iterdump())
        self.assertEqual(self.report(), self.report())
        for start, end in (('2026-09-17T00:00:00+08:00', '2026-09-25T00:00:00+08:00'),
                           ('2026-09-24T00:00:00', '2026-09-25T00:00:00'),
                           ('2026-09-25T00:00:00+08:00', '2026-09-26T00:00:00+08:00')):
            with self.assertRaises(ValidationError): self.report(start, end)
        with self.assertRaises(ValidationError): self.report(limit=501)
        with self.db.read() as c:
            self.assertEqual(before, tuple(c.iterdump()))

    def test_owner_cleanup_can_cascade_but_report_fact_cannot_be_deleted_directly(self):
        with self.db.write() as c:
            c.execute("INSERT INTO private_follow_completions(owner_user_id,username_norm,username_display,"
                "campaign_id,target_id,attempt_id,completed_at) VALUES(?,'standalone','standalone','c','t','a',?)", (self.other, NOW.isoformat()))
        with self.assertRaises(sqlite3.IntegrityError), self.db.write() as c:
            c.execute('DELETE FROM private_follow_completions WHERE owner_user_id=?', (self.other,))
        with self.db.write() as c:
            c.execute('DELETE FROM app_users WHERE id=?', (self.other,))
        with self.db.read() as c:
            self.assertEqual(0, c.execute('SELECT COUNT(*) FROM private_follow_completions').fetchone()[0])

    def test_http_endpoint_requires_auth_and_ignores_no_foreign_owner_injection(self):
        from fastapi.testclient import TestClient
        from app.main import create_app
        from app.config import Settings
        self.follow('api.private', datetime.now(timezone.utc).isoformat())
        now = datetime.now(timezone.utc); start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        body = {'start': start.isoformat(), 'end': (start + timedelta(days=1)).isoformat(), 'utc_offset_minutes': 0}
        app = create_app(Settings('private-review-test-startup', self.root / 'private.db', self.root), database=self.db)
        with TestClient(app) as client:
            headers = {'X-Startup-Token': 'private-review-test-startup'}
            self.assertEqual(401, client.post('/api/reports/private-follow-review', headers=headers, json=body).status_code)
            headers['Authorization'] = 'Bearer ' + self.service.login('private-report-owner', PASSWORD)['token']
            response = client.post('/api/reports/private-follow-review', headers=headers, json=body)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(1, response.json()['total'])
            self.assertEqual(7, len(response.json()['daily_counts']))
            self.assertGreaterEqual(client.post('/api/reports/private-follow-review', headers=headers,
                json={**body, 'owner_user_id': self.other}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
