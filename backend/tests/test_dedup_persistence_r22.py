from __future__ import annotations

import json
import base64
import hashlib
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cloud_workspace import decode_workspace, export_workspace, import_workspace
from app.database import Database
from app.identity_registry import repair_global_registry, reserve_split_identity
from app.service import CoreService, isoformat


PASSWORD = 'correct horse battery staple'


class DedupPersistenceR22Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / 'source.sqlite')
        self.db.initialize()
        self.s = CoreService(self.db)
        self.owner = self.s.register_user('dedup-owner', PASSWORD)['id']
        self.other = self.s.register_user('dedup-other', PASSWORD)['id']

    def tearDown(self):
        self.tmp.cleanup()

    def destination(self):
        db = Database(self.root / 'destination.sqlite')
        db.initialize()
        service = CoreService(db)
        owner = service.register_user('dedup-destination', PASSWORD)['id']
        return db, service, owner

    def split_with_rename(self):
        rows = self.s.upsert_manual_split_candidates(
            self.owner, [{'username': 'split.original', 'queued': True}]
        )
        self.assertTrue(self.s.claim_workbench_identity(
            self.owner, username='split.original', instagram_user_id='112233', source='following'
        )['duplicate'])
        self.assertTrue(self.s.claim_workbench_identity(
            self.owner, username='split.renamed', instagram_user_id='112233', source='following'
        )['duplicate'])
        return next(row['id'] for row in rows if row['username'] == 'split.original')

    def assert_backup_preserves_renamed_split(self):
        payload = export_workspace(self.db, self.owner, self.root)[0]
        data = decode_workspace(payload)
        self.assertEqual(1, len(data['tables']['instagram_accounts']))
        self.assertEqual('112233', data['tables']['instagram_accounts'][0]['instagram_user_id'])
        self.assertEqual(
            {'split.original', 'split.renamed'},
            {row['username_norm'] for row in data['tables']['instagram_username_aliases']},
        )
        db, service, owner = self.destination()
        import_workspace(db, owner, self.root / 'restored', payload)
        for username in ('split.original', 'split.renamed'):
            self.assertTrue(service.check_global_dedupe(username)['seen'])
        self.assertTrue(service.claim_workbench_identity(
            owner, username='split.renamed', instagram_user_id='112233', source='followers'
        )['duplicate'])
        with db.read() as c:
            self.assertEqual([], c.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(owner, c.execute('SELECT owner_user_id FROM global_identity_owners').fetchone()[0])
        return data

    def test_split_only_backup_preserves_stable_id_and_all_aliases(self):
        self.split_with_rename()
        self.assert_backup_preserves_renamed_split()

    def test_deleted_split_and_pruned_events_keep_permanent_backup_identity(self):
        candidate = self.split_with_rename()
        self.s.delete_waiting_split_candidate(self.owner, candidate)
        with self.db.write() as c:
            c.execute('DELETE FROM event_log')
        data = self.assert_backup_preserves_renamed_split()
        self.assertEqual([], data['tables']['split_candidates'])
        self.assertEqual([], data['tables']['split_candidate_history'])
        self.assertEqual(1, len(data['tables']['global_identity_owners']))

    def test_owner_backup_excludes_foreign_and_unattributed_orphan_identities(self):
        with self.db.write() as c:
            reserve_split_identity(c, 'own.identity', 'own.identity', isoformat(), owner_user_id=self.owner)
            reserve_split_identity(c, 'foreign.identity', 'foreign.identity', isoformat(), owner_user_id=self.other)
            reserve_split_identity(c, 'unknown.orphan', 'unknown.orphan', isoformat())
        data = decode_workspace(export_workspace(self.db, self.owner, self.root)[0])
        text = json.dumps(data)
        self.assertIn('own.identity', text)
        self.assertNotIn('foreign.identity', text)
        self.assertNotIn('unknown.orphan', text)
        self.assertNotIn(self.other, text)
        self.assertTrue(self.s.check_global_dedupe('foreign.identity')['seen'])
        self.assertTrue(self.s.check_global_dedupe('unknown.orphan')['seen'])

    def test_migration_repairs_every_retained_business_source_without_mutating_history(self):
        task = self.s.create_task(self.owner, name='source audit', modes=['followers'], targets=['source.old'], settings={})
        self.s.record_result(
            self.owner, task['id'], task['targets'][0]['id'], username='result.old',
            instagram_user_id='991122', source_mode='followers', visibility='private',
            profile={'is_private': True}, screening={}, qualified=None,
        )
        self.s.create_action_campaign(
            self.owner, operation='follow', execution_type='manual', profile_id='audit-window',
            targets=['action.old'], message=None, interval_min_seconds=8,
            interval_max_seconds=15, limit_count=1,
        )
        self.s.upsert_manual_split_candidates(self.owner, [{'username': 'split.old', 'queued': True}])
        now = isoformat()
        with self.db.write() as c:
            c.execute('''INSERT INTO action_success_ledger VALUES(?,?,?,?,?,?,?,?)''',
                      (self.owner, 'follow', 'success.old', 'success.old', 'retired-campaign', 'retired-target', 'retired-attempt', now))
            c.execute('''INSERT INTO task_target_recovery_controls
                (target_id,owner_user_id,candidate_id,username_norm,username_display,state,updated_at)
                VALUES(?,?,?,?,?,'dismissed',?)''',
                ('retired-source', self.owner, 'retired-candidate', 'failure.old', 'failure.old', now))
            c.execute('DELETE FROM global_identity_owners')
            c.execute('DELETE FROM global_seen')
            c.execute('DELETE FROM instagram_username_aliases')
            c.execute('DELETE FROM schema_migrations WHERE version=30')
        business_tables = ('tasks', 'task_targets', 'task_results', 'action_campaigns',
                           'action_targets', 'action_success_ledger', 'split_candidates', 'task_target_recovery_controls')
        def business_snapshot():
            with self.db.read() as c:
                return {table: [dict(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                        for table in business_tables}
        before = business_snapshot()
        self.db.initialize()
        self.db.initialize()
        self.assertEqual(before, business_snapshot())
        for username in ('source.old', 'result.old', 'action.old', 'split.old', 'success.old', 'failure.old'):
            self.assertTrue(self.s.check_global_dedupe(username)['seen'], username)
            self.assertTrue(self.s.claim_workbench_identity(self.other, username=username, source='followers')['duplicate'], username)
        with self.db.read() as c:
            self.assertEqual(6, c.execute('SELECT total_count FROM global_seen_stats').fetchone()[0])
            self.assertEqual(6, c.execute('SELECT count(*) FROM global_identity_owners WHERE owner_user_id=?', (self.owner,)).fetchone()[0])
            self.assertEqual([], c.execute('PRAGMA foreign_key_check').fetchall())

    def test_new_source_membership_changes_snapshot_revision_only_when_new(self):
        with self.db.write() as c:
            initial = c.execute('SELECT revision FROM workbench_state_revision').fetchone()[0]
            reserve_split_identity(c, 'one.source', 'one.source', isoformat(), owner_user_id=self.owner)
            changed = c.execute('SELECT revision FROM workbench_state_revision').fetchone()[0]
            reserve_split_identity(c, 'one.source', 'one.source', isoformat(), owner_user_id=self.owner)
            repeated = c.execute('SELECT revision FROM workbench_state_revision').fetchone()[0]
        self.assertGreater(changed, initial)
        self.assertEqual(changed, repeated)

    def test_registry_repair_uses_provenance_after_source_and_diagnostic_removal(self):
        with self.db.write() as c:
            identity = reserve_split_identity(c, 'removed.source', 'removed.source', isoformat(), owner_user_id=self.owner)
            c.execute('DELETE FROM global_seen WHERE account_id=?', (identity,))
            repair_global_registry(c)
        self.assertTrue(self.s.check_global_dedupe('removed.source')['seen'])

    def test_legacy_duplicate_result_is_archived_in_full_and_survives_backup(self):
        first = self.s.create_task(self.owner, name='first task', modes=['followers'], targets=['first.source'], settings={})
        second = self.s.create_task(self.owner, name='second task', modes=['followers'], targets=['second.source'], settings={})
        result = self.s.record_result(
            self.owner, first['id'], first['targets'][0]['id'], username='same.person',
            instagram_user_id='448899', source_mode='followers', visibility='private',
            profile={'original': 'first'}, screening={}, qualified=None,
        )
        with self.db.write() as c:
            c.execute('DROP INDEX idx_results_global_account')
            c.execute('''INSERT INTO task_results
                SELECT 'legacy-copy',?,?,account_id,sources_json,'private',
                       '{"original":"second","notes":"retain me"}',
                       '{"review":"approved"}',1,'2099-01-01','2099-01-02'
                FROM task_results WHERE id=?''',
                (second['id'], second['targets'][0]['id'], result['id']))
            original = dict(c.execute("SELECT * FROM task_results WHERE id='legacy-copy'").fetchone())
        self.db.initialize()
        self.db.initialize()
        with self.db.read() as c:
            self.assertEqual(1, c.execute('SELECT count(*) FROM task_results').fetchone()[0])
            archive = dict(c.execute('SELECT * FROM task_result_duplicate_archive').fetchone())
            self.assertEqual(self.owner, archive.pop('owner_user_id'))
            self.assertTrue(archive.pop('archived_at'))
            archive['id'] = archive.pop('original_result_id')
            self.assertEqual(original, archive)
        payload = export_workspace(self.db, self.owner, self.root)[0]
        db, _, owner = self.destination()
        import_workspace(db, owner, self.root / 'restored', payload)
        with db.read() as c:
            restored = dict(c.execute('SELECT * FROM task_result_duplicate_archive').fetchone())
            self.assertEqual(owner, restored['owner_user_id'])
            self.assertEqual(original['profile_json'], restored['profile_json'])
            self.assertEqual(original['target_id'], restored['target_id'])
            self.assertEqual([], c.execute('PRAGMA foreign_key_check').fetchall())

    def test_legacy_backup_without_new_registry_tables_restores_business_provenance(self):
        self.split_with_rename()
        data = decode_workspace(export_workspace(self.db, self.owner, self.root)[0])
        for table in ('global_identity_owners', 'task_result_duplicate_archive', 'account_creation_batches'):
            data['tables'].pop(table)
        raw = json.dumps(data).encode()
        payload = {'format': 1, 'encoding': 'zlib+base64',
                   'sha256': hashlib.sha256(raw).hexdigest(),
                   'data': base64.b64encode(zlib.compress(raw)).decode()}
        db, service, owner = self.destination()
        import_workspace(db, owner, self.root / 'restored', payload)
        self.assertTrue(service.check_global_dedupe('split.renamed')['seen'])
        with db.read() as c:
            self.assertEqual(owner, c.execute('SELECT owner_user_id FROM global_identity_owners').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
