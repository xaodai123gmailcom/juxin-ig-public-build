"""Completed split review preserves exact per-source metadata and r46 progress."""
from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.cloud_workspace import decode_workspace, export_workspace, import_workspace
from app.database import Database
from app.errors import NotFoundError
from app.service import CoreService
from app.work_reports import split_review_report


class SplitCompletionDetailsR56Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / 'details.db')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('completion-details', 'long test password')['id']
        self.task = self.service.create_task(self.owner, name='source stats',
            modes=['followers', 'following'], targets=['source.one', 'source.two'], settings={})
        self.target = self.task['targets'][0]['id']
        self.other_target = self.task['targets'][1]['id']

    def tearDown(self):
        self.temp.cleanup()

    def source(self, **values):
        return self.service.capture_target_source_profile(self.owner, self.task['id'], self.target,
            {'username': 'source.one', **values})

    def checkpoint(self, mode, values, *, target=None):
        return self.service.upsert_checkpoint(self.owner, self.task['id'], target or self.target,
            mode=mode, stage='mode_completed', cursor={'candidate_spool_complete': True,
                'candidate_spool_natural_end': True}, counters=values, recoverable=False)

    def finish(self):
        self.service.set_target_runtime_status(self.owner, self.task['id'], self.target,
            'completed', window_id='source-window')

    def items(self, *, db=None, owner=None):
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return split_review_report(db or self.db, owner or self.owner, start.isoformat(),
            (start + timedelta(days=1)).isoformat(), utc_offset_minutes=0, now=now)['items']

    def stats(self, item):
        return tuple(item[field] for field in ('processed_count', 'new_count', 'duplicate_count'))

    def complete_checkpoints(self):
        self.checkpoint('followers', {'source_total': 12, 'processed': 5, 'saved': 2, 'skipped_global_duplicates': 3})
        self.checkpoint('following', {'source_total': 7, 'processed': 4, 'saved': 1, 'skipped_global_duplicates': 3})

    def test_completion_matches_r46_per_mode_counts_and_excludes_other_source(self):
        self.source(followers=41, following=17, posts=0)
        for mode, terminal in (('followers', [('a', 'recorded'), ('b', 'deduped'), ('c', 'deduped')]),
                               ('following', [('a', 'deduped'), ('d', 'recorded')])):
            self.service.append_task_mode_candidates(self.owner, self.task['id'], self.target,
                mode, [name for name, _ in terminal])
            for name, state in terminal:
                self.service.finish_task_mode_candidate(self.owner, self.task['id'], self.target,
                    mode, name, state=state)
            self.checkpoint(mode, {'processed': 900, 'saved': 800, 'skipped_global_duplicates': 100})
        self.checkpoint('followers', {'processed': 200, 'saved': 100, 'skipped_global_duplicates': 100}, target=self.other_target)
        self.finish()
        item = self.items()[0]
        self.assertEqual((41, 17, 0), tuple(item[key] for key in ('followers', 'following', 'posts')))
        self.assertEqual((5, 2, 3), self.stats(item))
        self.assertEqual('source-window', item['source_window_id'])

    def test_snapshot_survives_archive_checkpoint_mutation_task_deletion_and_restart(self):
        self.source(followers=12, following=7, posts=3)
        self.complete_checkpoints()
        self.finish()
        baseline = self.items()
        self.assertEqual((9, 3, 6), self.stats(baseline[0]))
        self.service.archive_completed_task_target_from_list(self.owner, self.task['id'], self.target)
        self.checkpoint('followers', {'processed': 999, 'saved': 999, 'skipped_global_duplicates': 0})
        self.assertFalse(self.source(followers=999, following=999, posts=999))
        self.service.set_target_runtime_status(self.owner, self.task['id'], self.target,
            'completed', window_id='late-window')
        self.assertEqual(baseline, self.items())
        with self.db.write() as c:
            c.execute('DELETE FROM tasks WHERE id=?', (self.task['id'],))
        self.db.initialize()
        self.assertEqual(baseline, self.items())

    def test_missing_legacy_progress_is_unknown_and_visible_zero_is_preserved(self):
        self.source(followers=None, following=0, posts=None)
        self.checkpoint('followers', {'source_total': 241, 'processed': 47})
        self.checkpoint('following', {'source_total': 0, 'processed': 0, 'saved': 0, 'skipped_global_duplicates': 0})
        self.finish()
        item = self.items()[0]
        self.assertEqual((241, 0, None), tuple(item[key] for key in ('followers', 'following', 'posts')))
        self.assertEqual((47, None, None), self.stats(item))

    def test_missing_mode_never_undercounts_as_zero(self):
        self.checkpoint('followers', {'source_total': 5, 'processed': 5, 'saved': 3, 'skipped_global_duplicates': 2})
        self.finish()
        item = self.items()[0]
        self.assertEqual((None, None, None), self.stats(item))
        self.assertEqual((5, None, None), tuple(item[key] for key in ('followers', 'following', 'posts')))

    def test_source_identity_owner_and_invalid_counts_are_not_guessed(self):
        self.assertFalse(self.service.capture_target_source_profile(self.owner, self.task['id'], self.target,
            {'username': 'source.two', 'followers': 999, 'posts': 999}))
        other_owner = self.service.register_user('other-details', 'long test password')['id']
        with self.assertRaises(NotFoundError):
            self.service.capture_target_source_profile(other_owner, self.task['id'], self.target,
                {'username': 'source.one', 'followers': 999})
        self.source(followers=True, following='42', posts=-1)
        self.finish()
        item = self.items()[0]
        self.assertEqual((None, None, None), tuple(item[key] for key in ('followers', 'following', 'posts')))

    def test_history_only_uses_saved_source_snapshot_without_current_account_lookup(self):
        now = datetime.now(timezone.utc).isoformat()
        with self.db.write() as c:
            c.execute("""INSERT INTO split_candidate_history(id,owner_user_id,username_norm,username_display,
                source_status,profile_json,completed_at,created_at,updated_at)
                VALUES('legacy',?,'old.source','old.source','completed',?,?,?,?)""",
                (self.owner, json.dumps({'followers': 12, 'following': 0, 'posts': None}), now, now, now))
        item = self.items()[0]
        self.assertEqual((12, 0, None), tuple(item[key] for key in ('followers', 'following', 'posts')))
        self.assertEqual((None, None, None), self.stats(item))

    def test_migration_backfills_only_retained_progress_and_keeps_first_completion_date(self):
        self.complete_checkpoints()
        self.finish()
        before = self.items()[0]['completed_at']
        with self.db.write() as c:
            c.execute('DELETE FROM schema_migrations WHERE version=35')
            c.execute('ALTER TABLE task_targets DROP COLUMN source_profile_json')
            c.execute('ALTER TABLE split_completed_targets DROP COLUMN completion_details_json')
            c.execute("UPDATE task_targets SET updated_at='2099-01-01T00:00:00Z' WHERE id=?", (self.target,))
        self.db.initialize()
        self.db.initialize()
        item = self.items()[0]
        self.assertEqual(before, item['completed_at'])
        self.assertEqual((12, 7, None), tuple(item[key] for key in ('followers', 'following', 'posts')))
        self.assertEqual((9, 3, 6), self.stats(item))
        with self.db.read() as c:
            self.assertEqual(35, c.execute('SELECT version FROM schema_migrations WHERE version=35').fetchone()[0])

    def test_current_and_legacy_cloud_backup_restore_completion_snapshots(self):
        self.source(followers=12, following=7, posts=3)
        self.complete_checkpoints()
        self.finish()
        payload, _ = export_workspace(self.db, self.owner, self.root)
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                imported = payload
                if legacy:
                    data = decode_workspace(payload)
                    for row in data['tables']['task_targets']:
                        row.pop('source_profile_json')
                    for row in data['tables']['split_completed_targets']:
                        row.pop('completion_details_json')
                    raw = json.dumps(data).encode()
                    imported = {'format': 1, 'encoding': 'zlib+base64',
                        'sha256': hashlib.sha256(raw).hexdigest(),
                        'data': base64.b64encode(zlib.compress(raw)).decode()}
                destination = Database(self.root / f'restored-{legacy}.db')
                destination.initialize()
                service = CoreService(destination)
                owner = service.register_user(f'restored-{legacy}', 'long test password')['id']
                import_workspace(destination, owner, self.root / f'assets-{legacy}', imported)
                item = self.items(db=destination, owner=owner)[0]
                self.assertEqual((9, 3, 6), self.stats(item))
                self.assertEqual((12, 7, None if legacy else 3),
                    tuple(item[key] for key in ('followers', 'following', 'posts')))

    def test_review_reads_do_not_change_completion_or_dedup_data(self):
        self.complete_checkpoints()
        self.finish()
        with self.db.read() as c:
            before = tuple(c.iterdump())
        self.assertEqual(self.items(), self.items())
        with self.db.read() as c:
            self.assertEqual(before, tuple(c.iterdump()))


if __name__ == '__main__':
    unittest.main()
