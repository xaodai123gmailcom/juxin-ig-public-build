from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.account_exports import HEADERS, export_accounts, safe_csv_cell
from app.database import Database
from app.errors import ValidationError
from app.service import CoreService

PASSWORD = 'correct horse battery staple'


class AccountExportsR55Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / 'export.db'); self.db.initialize()
        self.service = CoreService(self.db, session_hours=1)
        self.owner = self.service.register_user('export-owner', PASSWORD)['id']

    def tearDown(self):
        self.temp.cleanup()

    def candidate(self, name, visibility='public', owner=None, approved=True, profile=None, screening=None):
        owner = owner or self.owner
        claim = self.service.claim_workbench_identity(owner, username=name, source='followers')
        item = self.service.create_workbench_candidate(owner, claim_id=claim['claim_id'], username=name,
            visibility=visibility, profile=profile or {'display_name': name}, screening=screening or {},
            review_cache={'avatar_preview': 'SECRET_PREVIEW_NOT_FOR_EXPORT'})
        if approved:
            self.service.decide_workbench_candidate(owner, candidate_id=item['id'], decision='approved')
        return item

    def export(self, **kwargs):
        return export_accounts(self.db, self.owner, visibility=kwargs.pop('visibility', 'public'),
            scope=kwargs.pop('scope', 'all'), **kwargs)

    def rows(self, result):
        self.assertTrue(result['csv'].startswith('\ufeff'))
        return list(csv.DictReader(io.StringIO(result['csv'].removeprefix('\ufeff'))))

    def test_owner_approved_visibility_and_selection_are_independent_fences(self):
        own = self.candidate('own.public')
        self.candidate('own.pending', approved=False)
        private = self.candidate('own.private', 'private')
        foreign_owner = self.service.register_user('foreign-owner', PASSWORD)['id']
        foreign = self.candidate('foreign.public', owner=foreign_owner)
        before = self.service.get_workbench_dedupe_stats(self.owner)
        result = self.export(scope='selected', candidate_ids=[own['id'], own['id'], private['id'], foreign['id'], 'missing'])
        self.assertEqual(1, result['row_count'])
        self.assertEqual(3, result['skipped_count'])
        self.assertEqual(['own.public'], [r['账号'] for r in self.rows(result)])
        self.assertEqual(['own.private'], [r['账号'] for r in self.rows(self.export(visibility='private'))])
        self.assertEqual(['own.public'], [r['账号'] for r in self.rows(self.export())])
        self.assertEqual(before, self.service.get_workbench_dedupe_stats(self.owner))

    def test_large_all_export_is_not_snapshot_capped_and_selected_chunks_are_complete(self):
        item = self.candidate('bulk.seed')
        ids = [item['id']]
        with self.db.write() as c:
            account = dict(c.execute('SELECT * FROM instagram_accounts WHERE id=?', (item['account_id'],)).fetchone())
            candidate = dict(c.execute('SELECT * FROM workbench_candidates WHERE id=?', (item['id'],)).fetchone())
            for index in range(1, 2103):
                a = {**account, 'id': f'export-account-{index}', 'current_username_norm': f'bulk.{index}',
                     'current_username_display': f'bulk.{index}'}
                c.execute(f"INSERT INTO instagram_accounts({','.join(a)}) VALUES({','.join('?' for _ in a)})", tuple(a.values()))
                row = {**candidate, 'id': f'export-candidate-{index}', 'account_id': a['id']}
                c.execute(f"INSERT INTO workbench_candidates({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
                ids.append(row['id'])
        all_result = self.export()
        self.assertEqual(2103, all_result['row_count'])
        self.assertEqual(2103, len(self.rows(all_result)))
        selected = self.export(scope='selected', candidate_ids=ids)
        self.assertEqual(2103, selected['row_count'])
        self.assertEqual(0, selected['skipped_count'])
        self.assertEqual({r['账号'] for r in self.rows(all_result)}, {r['账号'] for r in self.rows(selected)})

    def test_current_inventory_excludes_dismissed_and_successful_aliases_without_erasing_history(self):
        keep = self.candidate('still.approved')
        dismissed = self.candidate('already.dismissed')
        succeeded = self.candidate('old.success.name')
        wrong_operation = self.candidate('follow.only.public')
        self.service.dismiss_approved_candidate(self.owner, dismissed['id'])
        with self.db.write() as c:
            for name, operation in [('old.success.name', 'greet'), ('follow.only.public', 'follow')]:
                c.execute('''INSERT INTO action_success_ledger(owner_user_id,operation,username_norm,
                  username_display,campaign_id,target_id,attempt_id,completed_at)
                  VALUES(?,?,?,?,?,?,?,?)''', (self.owner, operation, name, name, 'campaign-'+name,
                  'target-'+name, 'attempt-'+name, '2026-09-24T00:00:00Z'))
            # A rename must not make a previously completed action reappear.
            c.execute('UPDATE instagram_accounts SET current_username_display=?,current_username_norm=? WHERE id=?',
                      ('new.success.name', 'new.success.name', succeeded['account_id']))
        before = self.service.get_workbench_dedupe_stats(self.owner)
        result = self.export()
        self.assertEqual({'still.approved', 'follow.only.public'}, {row['账号'] for row in self.rows(result)})
        self.assertEqual(2, self.service.get_workbench_snapshot(self.owner)['counts']['approved_public'])
        selected = self.export(scope='selected', candidate_ids=[keep['id'], dismissed['id'], succeeded['id'], wrong_operation['id']])
        self.assertEqual(2, selected['row_count']); self.assertEqual(2, selected['skipped_count'])
        self.assertEqual(before, self.service.get_workbench_dedupe_stats(self.owner))
        with self.db.read() as c:
            self.assertEqual(2, c.execute('SELECT COUNT(*) FROM action_success_ledger').fetchone()[0])
            self.assertEqual(1, c.execute('SELECT COUNT(*) FROM workbench_candidate_dismissals').fetchone()[0])

    def test_whitelisted_fields_csv_roundtrip_formula_safety_and_unknown_counts(self):
        item = self.candidate('safe.fields', profile={
            'display_name': '=HYPERLINK("https://bad.invalid","点击")\n第二行,姓名',
            'followers': None, 'following': 0, 'posts': 12,
            'recent_post_datetime': '2026-09-20T12:00:00+00:00',
            'post_activity_days': 3, 'post_activity_status': 'timestamp_read',
            'location_zh': '美国',
        })
        # Normal ingestion already rejects secrets. A legacy/custom database
        # must still be projected through the export's field whitelist.
        with self.db.write() as c:
            profile = json.loads(c.execute('SELECT profile_json FROM workbench_candidates WHERE id=?', (item['id'],)).fetchone()[0])
            profile.update({'cookies': 'SECRET_COOKIE', 'password': 'SECRET_PASSWORD',
                            'token': 'SECRET_TOKEN', 'nested': {'credential': 'SECRET_NESTED'}})
            c.execute('UPDATE workbench_candidates SET profile_json=? WHERE id=?', (json.dumps(profile), item['id']))
        result = self.export(); row = self.rows(result)[0]
        self.assertEqual(tuple(row), HEADERS)
        self.assertTrue(row['显示名称'].startswith("'=HYPERLINK"))
        self.assertIn('\n第二行,姓名', row['显示名称'])
        self.assertEqual('', row['粉丝数'])
        self.assertEqual('0', row['关注数'])
        self.assertEqual('12', row['帖子数'])
        self.assertEqual('3', row['距最近发帖天数'])
        self.assertEqual('美国', row['所在地'])
        self.assertEqual('已显示地区', row['地区信息状态'])
        self.assertTrue(row['采集时间']); self.assertTrue(row['审核通过时间'])
        for forbidden in ('SECRET_', 'review_cache', 'cookie', 'password', 'token'):
            self.assertNotIn(forbidden, result['csv'])
        self.assertTrue(result['csv'].encode('utf-8').startswith(b'\xef\xbb\xbf'))
        for value in ('=1+1', '+SUM(A1)', '-2+3', '@sum', ' \t=1', '\r=1', '\n@x', '\ufeff=1', '\u00a0=1'):
            self.assertTrue(safe_csv_cell(value).startswith("'"), value)

    def test_numeric_usernames_keep_leading_zeros_and_full_digits_as_excel_text(self):
        names = ['001234', '123456789012345678901234567890', '1e10', '12.34']
        for name in names:
            self.candidate(name, profile={'display_name': '007', 'followers': 1234, 'following': 0})
        rows = self.rows(self.export())
        self.assertEqual({"'" + name for name in names}, {row['账号'] for row in rows})
        for row in rows:
            self.assertEqual("'007", row['显示名称'])
            self.assertEqual('1234', row['粉丝数'])
            self.assertEqual('0', row['关注数'])
            self.assertNotIn("'", row['主页链接'])
            self.assertFalse(row['账号'].startswith('='))

    def test_private_post_age_unknown_and_location_states_do_not_fabricate_values(self):
        self.candidate('private.hidden', 'private', profile={
            'location_status': 'not_disclosed', 'activity_days': 0,
            'post_activity_days': 5, 'post_activity_status': 'timestamp_read',
            'recent_post_datetime': '2026-09-20T12:00:00+00:00',
        })
        self.candidate('private.absent', 'private')
        rows = {r['账号']: r for r in self.rows(self.export(visibility='private'))}
        self.assertEqual('地区不公开', rows['private.hidden']['地区信息状态'])
        self.assertEqual('无地区信息', rows['private.absent']['地区信息状态'])
        for row in rows.values():
            self.assertEqual('', row['距最近发帖天数'])
            self.assertEqual('', row['最近发帖时间'])
        self.candidate('story.only', profile={'activity_days': 0, 'activity_status': 'story_today'})
        self.assertEqual('', self.rows(self.export())[0]['距最近发帖天数'])

    def test_invalid_ranges_and_ids_fail_without_falling_back_to_all(self):
        for kwargs in ({'scope': 'selected'}, {'scope': 'selected', 'candidate_ids': []},
                       {'scope': 'all', 'candidate_ids': ['one']}, {'visibility': 'unknown'},
                       {'scope': 'selected', 'candidate_ids': ['']},
                       {'scope': 'selected', 'candidate_ids': ['x' * 101]},
                       {'scope': 'selected', 'candidate_ids': ['x'] * 5001}):
            with self.assertRaises(ValidationError): self.export(**kwargs)
        self.assertEqual(0, self.export()['row_count'])

    def test_http_export_requires_session_and_uses_authenticated_owner_only(self):
        from fastapi.testclient import TestClient
        from app.config import Settings
        from app.main import create_app
        from test_workbench_newgen import EmptyBitBrowser
        self.candidate('http.export')
        settings = Settings(startup_token='test-account-export-startup-token-123456', database_path=self.db.path, data_dir=self.root)
        app = create_app(settings, database=self.db, bitbrowser=EmptyBitBrowser())
        token = self.service.login('export-owner', PASSWORD)['token']
        headers = {'X-Startup-Token': settings.startup_token, 'Authorization': f'Bearer {token}'}
        body = {'visibility': 'public', 'scope': 'all'}
        with TestClient(app) as client:
            path = '/api/workbench/accounts/export'
            result = client.post(path, headers=headers, json=body)
            self.assertEqual(200, result.status_code)
            self.assertEqual(['http.export'], [r['账号'] for r in self.rows(result.json())])
            self.assertEqual(401, client.post(path, headers={'X-Startup-Token': settings.startup_token}, json=body).status_code)
            self.assertEqual(422, client.post(path, headers=headers, json={**body, 'owner_user_id': self.owner}).status_code)
            self.assertEqual(405, client.get(path, headers=headers).status_code)


if __name__ == '__main__':
    unittest.main()
