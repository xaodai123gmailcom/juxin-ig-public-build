from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Database
from app.service import CoreService
from app.errors import ValidationError
from app.cloud_workspace import export_workspace, import_workspace, decode_workspace

PASSWORD = 'correct horse battery staple'


class ReviewLayersR54Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / 'collector.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db, session_hours=1)
        self.owner = self.service.register_user('review-layers-owner', PASSWORD)['id']

    def tearDown(self):
        self.temp.cleanup()

    def candidate(self, username, visibility='public', owner=None):
        owner = owner or self.owner
        claim = self.service.claim_workbench_identity(owner, username=username, source='followers')
        return self.service.create_workbench_candidate(
            owner, claim_id=claim['claim_id'], username=username, visibility=visibility,
            profile={'display_name': username}, screening={'location_country': 'United States'},
            review_cache={'avatar_preview': 'data:image/png;base64,YQ=='},
        )

    def queue(self, stage, visibility='public', **kwargs):
        return self.service.query_review_queue(self.owner, visibility=visibility, review_stage=stage, **kwargs)

    def move(self, ids, source=1, dest=2, visibility='public'):
        return self.service.move_review_stage(
            self.owner, candidate_ids=ids, visibility=visibility, from_stage=source, to_stage=dest,
        )

    def bulk(self, count):
        """Large valid queue in one transaction; every row has its own identity."""
        first = self.candidate('bulk.seed')
        ids = [first['id']]
        with self.db.write() as c:
            account = dict(c.execute('SELECT * FROM instagram_accounts WHERE id=?', (first['account_id'],)).fetchone())
            candidate = dict(c.execute('SELECT * FROM workbench_candidates WHERE id=?', (first['id'],)).fetchone())
            for number in range(1, count):
                a = {**account, 'id': f'bulk-account-{number}', 'current_username_norm': f'bulk.{number}',
                     'current_username_display': f'bulk.{number}'}
                c.execute(f"INSERT INTO instagram_accounts({','.join(a)}) VALUES({','.join('?' for _ in a)})", tuple(a.values()))
                item = {**candidate, 'id': f'bulk-candidate-{number}', 'account_id': a['id']}
                c.execute(f"INSERT INTO workbench_candidates({','.join(item)}) VALUES({','.join('?' for _ in item)})", tuple(item.values()))
                ids.append(item['id'])
        return ids

    def test_selection_snapshot_excludes_new_incoming_and_preserves_evidence_and_dedup(self):
        selected = [self.candidate('selected.a'), self.candidate('selected.b')]
        newcomer = self.candidate('arrived.after.selection')
        before = self.service.get_workbench_dedupe_stats(self.owner)
        result = self.move([item['id'] for item in selected])
        self.assertEqual(2, result['moved_count'])
        self.assertEqual([newcomer['id']], [item['id'] for item in self.queue(1)['items']])
        layer = self.queue(2)
        self.assertEqual(2, layer['total'])
        self.assertEqual({'stage1': 1, 'stage2': 2}, layer['counts']['public'])
        for item in layer['items']:
            self.assertEqual('pending', item['status'])
            self.assertEqual(2, item['review_stage'])
            self.assertIsNotNone(item['review_transferred_at'])
            self.assertTrue(item['review_cache']['avatar_preview'])
        self.assertEqual(before, self.service.get_workbench_dedupe_stats(self.owner))
        with self.db.read() as c:
            self.assertEqual(0, c.execute('SELECT COUNT(*) FROM workbench_review_decisions').fetchone()[0])
        for name in ('selected.a', 'selected.b', 'arrived.after.selection'):
            self.assertTrue(self.service.check_global_dedupe(name)['seen'])

    def test_owner_visibility_stage_and_pending_fences_skip_stale_ids(self):
        one = self.candidate('keep.one')
        private = self.candidate('private.one', 'private')
        other = self.service.register_user('another-owner', PASSWORD)['id']
        foreign = self.candidate('other.owner', owner=other)
        decided = self.candidate('decided.one')
        self.service.decide_workbench_candidate(self.owner, candidate_id=decided['id'], decision='approved')
        ids = [one['id'], private['id'], foreign['id'], decided['id'], 'not-found', one['id']]
        result = self.move(ids)
        self.assertEqual([one['id']], result['moved_ids'])
        self.assertEqual(4, len(result['skipped_ids']))
        self.assertEqual(0, self.move([one['id']])['moved_count'])
        self.assertEqual(1, self.queue(1, 'private')['total'])
        self.assertEqual(1, self.service.query_review_queue(other, visibility='public', review_stage=1)['total'])

    def test_private_manual_stage_survives_restart_without_retired_private_tier(self):
        item = self.candidate('private.batch', 'private')
        self.move([item['id']], visibility='private')
        self.db.initialize()
        row = self.queue(2, 'private')['items'][0]
        self.assertEqual('primary', row['screening']['review_tier'])
        self.assertEqual(item['id'], row['id'])
        self.move([item['id']], source=2, dest=1, visibility='private')
        self.assertEqual(1, self.queue(1, 'private')['total'])
        self.assertEqual(0, self.queue(2, 'private')['total'])

    def test_stage_two_pagination_is_independent_of_snapshot_limit(self):
        ids = self.bulk(2003)
        self.move(ids[-3:])
        self.assertEqual(2000, self.queue(1)['total'])
        first = self.queue(2, limit=2)
        second = self.queue(2, offset=2, limit=2)
        self.assertEqual(3, first['total'])
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        self.assertEqual(set(ids[-3:]), {r['id'] for r in first['items'] + second['items']})

    def test_batch_is_atomic_even_when_second_sql_chunk_fails(self):
        ids = self.bulk(401)
        with self.db.write() as c:
            c.execute(f"""CREATE TRIGGER reject_last_move BEFORE UPDATE OF review_stage ON workbench_candidates
            WHEN NEW.id='{ids[-1]}' BEGIN SELECT RAISE(ABORT,'injected failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.move(ids)
        self.assertEqual(401, self.queue(1)['total'])
        self.assertEqual(0, self.queue(2)['total'])

    def test_concurrent_decision_and_move_cannot_resurrect_decided_candidate(self):
        item = self.candidate('race.one')
        with ThreadPoolExecutor(max_workers=2) as pool:
            moved = pool.submit(self.move, [item['id']])
            decided = pool.submit(self.service.decide_workbench_candidate, self.owner,
                                  candidate_id=item['id'], decision='rejected')
            moved.result(); decided.result()
        self.assertEqual(0, self.queue(1)['total'])
        self.assertEqual(0, self.queue(2)['total'])
        self.assertTrue(self.service.check_global_dedupe('race.one')['seen'])
        with self.db.read() as c:
            self.assertEqual('rejected', c.execute('SELECT status FROM workbench_candidates WHERE id=?', (item['id'],)).fetchone()[0])
            self.assertEqual(1, c.execute('SELECT COUNT(*) FROM workbench_review_decisions').fetchone()[0])

    def test_old_database_migration_defaults_existing_rows_to_first_layer(self):
        item = self.candidate('legacy.row')
        with self.db.write() as c:
            c.execute('DROP INDEX idx_workbench_review_stage')
            c.execute('DROP INDEX idx_candidates_platform_review_stage')
            c.execute('ALTER TABLE workbench_candidates DROP COLUMN review_stage')
            c.execute('ALTER TABLE workbench_candidates DROP COLUMN review_transferred_at')
        self.db.initialize()
        row = self.queue(1)['items'][0]
        self.assertEqual(item['id'], row['id'])
        self.assertEqual(1, row['review_stage'])
        self.assertIsNone(row['review_transferred_at'])
        self.assertTrue(self.service.check_global_dedupe('legacy.row')['seen'])
        with self.db.read() as c:
            columns = [column['name'] for column in c.execute(
                'PRAGMA index_info(idx_candidates_platform_review_stage)'
            )]
        self.assertEqual(
            ['owner_user_id', 'status', 'visibility', 'review_stage', 'created_at', 'id', 'account_id'],
            columns,
        )

    def test_cloud_round_trip_preserves_stage_and_old_backup_defaults(self):
        item = self.candidate('cloud.batch')
        self.move([item['id']])
        payload, _ = export_workspace(self.db, self.owner, self.root)
        data = decode_workspace(payload)
        for old in (False, True):
            db = Database(self.root / f'destination-{old}.db'); db.initialize()
            service = CoreService(db, session_hours=1)
            owner = service.register_user(f'destination-{old}', PASSWORD)['id']
            imported = payload
            if old:
                for row in data['tables']['workbench_candidates']:
                    row.pop('review_stage'); row.pop('review_transferred_at')
                raw = json.dumps(data).encode()
                imported = {'format': 1, 'encoding': 'zlib+base64', 'sha256': hashlib.sha256(raw).hexdigest(),
                            'data': base64.b64encode(zlib.compress(raw)).decode()}
            import_workspace(db, owner, self.root / f'assets-{old}', imported)
            page = service.query_review_queue(owner, visibility='public', review_stage=1 if old else 2)
            self.assertEqual(1, page['total'])
            self.assertEqual(item['id'], page['items'][0]['id'])
            self.assertTrue(service.check_global_dedupe('cloud.batch')['seen'])

    def test_api_queue_move_and_validation(self):
        from fastapi.testclient import TestClient
        from app.config import Settings
        from app.main import create_app
        from test_workbench_newgen import EmptyBitBrowser
        item = self.candidate('http.batch')
        settings = Settings(startup_token='test-review-layers-startup-token-123456', database_path=self.db.path, data_dir=self.root)
        app = create_app(settings, database=self.db, bitbrowser=EmptyBitBrowser())
        token = self.service.login('review-layers-owner', PASSWORD)['token']
        headers = {'X-Startup-Token': settings.startup_token, 'Authorization': f'Bearer {token}'}
        with TestClient(app) as client:
            query = client.post('/api/workbench/review/query', headers=headers, json={'visibility': 'public', 'review_stage': 1})
            self.assertEqual(200, query.status_code, query.text)
            self.assertEqual(1, query.json()['total'])
            result = client.post('/api/workbench/commands', headers=headers, json={'command': 'review_stage_move', 'payload': {
                'candidate_ids': [item['id']], 'visibility': 'public', 'from_stage': 1, 'to_stage': 2,
            }})
            self.assertEqual(200, result.status_code, result.text)
            self.assertEqual(1, result.json()['result']['moved_count'])
            self.assertEqual('review_stage_move', result.json()['command'])
            self.assertEqual(result.json()['result']['snapshot_seq'], result.json()['snapshot_seq'])
            self.assertGreater(result.json()['snapshot_seq'], query.json()['snapshot_seq'])
            self.assertEqual(401, client.post('/api/workbench/review/query',
                headers={'X-Startup-Token': settings.startup_token},
                json={'visibility': 'public', 'review_stage': 2}).status_code)
            other = self.service.register_user('api-other-owner', PASSWORD)['id']
            other_token = self.service.login('api-other-owner', PASSWORD)['token']
            other_headers = {**headers, 'Authorization': f'Bearer {other_token}'}
            foreign_page = client.post('/api/workbench/review/query', headers=other_headers,
                json={'visibility': 'public', 'review_stage': 2})
            self.assertEqual(0, foreign_page.json()['total'])
            foreign_move = client.post('/api/workbench/commands', headers=other_headers,
                json={'command': 'review_stage_move', 'payload': {
                    'candidate_ids': [item['id']], 'visibility': 'public', 'from_stage': 2, 'to_stage': 1}})
            self.assertEqual(200, foreign_move.status_code, foreign_move.text)
            self.assertEqual([item['id']], foreign_move.json()['result']['skipped_ids'])
            from datetime import datetime, timezone, timedelta
            midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            report_body = {'start': midnight.isoformat(), 'end': (midnight + timedelta(days=1)).isoformat(),
                           'utc_offset_minutes': 0}
            report = client.post('/api/reports/split-review', headers=headers, json=report_body)
            self.assertEqual(200, report.status_code, report.text)
            self.assertEqual([], report.json()['items'])
            self.assertEqual(7, report.json()['retention_days'])
            self.assertEqual(401, client.post('/api/reports/split-review',
                headers={'X-Startup-Token': settings.startup_token}, json=report_body).status_code)
            self.assertEqual(422, client.post('/api/reports/split-review', headers=headers,
                json={**report_body, 'utc_offset_minutes': 841}).status_code)
            self.assertEqual(422, client.post('/api/reports/split-review', headers=headers,
                json={**report_body, 'utc_offset_minutes': True}).status_code)
            self.assertEqual(422, client.post('/api/reports/split-review', headers=headers,
                json={key: value for key, value in report_body.items() if key != 'utc_offset_minutes'}).status_code)
            self.assertEqual(422, client.post('/api/workbench/review/query', headers=headers,
                json={'visibility': 'public', 'review_stage': 3}).status_code)
            self.assertEqual(422, client.post('/api/workbench/review/query', headers=headers,
                json={'visibility': 'public', 'review_stage': 1, 'limit': 2001}).status_code)

    def test_invalid_transfer_and_query_are_rejected_without_mutation(self):
        self.candidate('safe.row')
        for source, dest, ids in [(1, 1, ['x']), (0, 2, ['x']), (1, 2, []), (1, 2, ['x'] * 2001)]:
            with self.assertRaises(ValidationError):
                self.move(ids, source=source, dest=dest)
        with self.assertRaises(ValidationError):
            self.queue(1, limit=501)
        self.assertEqual(1, self.queue(1)['total'])


if __name__ == '__main__':
    unittest.main()
