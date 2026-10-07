"""Offline removal/archive/ownership regressions; no model or browser is loaded."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.database import Database
from app.errors import ConflictError, UpstreamUnavailableError, ValidationError
from app.posting_retirement import PostingRetirementError, retire_legacy_posting, reconcile_retired_posting
from app.posting_schema import initialize_posting_schema
from app.service import CoreService
from app.nurture_cleanup_recovery import reconcile_closed_nurture


class PostingRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / 'fixture.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db, session_hours=1)
        self.owner = self.service.register_user('retirement-owner', 'fixture password long enough')['id']
        self.other = self.service.register_user('retirement-other', 'fixture password long enough')['id']
        with self.db.write() as connection:
            initialize_posting_schema(connection)
        self.addCleanup(self.tmp.cleanup)

    def posting(self, ident, *, status='queued', profile='w1', owner=None, **fields):
        values = dict(id=ident, owner_user_id=owner or self.owner, request_key=ident,
                      theme='场景', caption='  retained caption\n全文  ', profile_id=profile,
                      status=status, created_at='2026-01-01', updated_at='2026-01-02', **fields)
        self.insert('posting_jobs', values)
        return self.one('posting_jobs', ident)

    def studio(self, ident, *, status='queued', kind='posting', profile='w1', owner=None, result=None, **fields):
        values = dict(id=ident, owner_user_id=owner or self.owner, request_key=ident,
                      kind=kind, profile_id=profile, status=status, config_json='{"caption":"原文"}',
                      result_json=json.dumps(result or {}), due_at='2026-01-01',
                      created_at='2026-01-01', updated_at='2026-01-02', **fields)
        self.insert('studio_jobs', values)
        return self.one('studio_jobs', ident)

    def insert(self, table, values):
        with self.db.write() as connection:
            connection.execute(f'INSERT INTO {table}(' + ','.join(values) + ') VALUES(' +
                               ','.join('?' for _ in values) + ')', tuple(values.values()))

    def one(self, table, ident):
        with self.db.read() as connection:
            row = connection.execute(f'SELECT * FROM {table} WHERE id=?', (ident,)).fetchone()
            return dict(row) if row is not None else None

    def records(self, table):
        with self.db.read() as connection:
            return [dict(row) for row in connection.execute(f'SELECT * FROM {table}')]

    def lease(self, ident, *, operation='posting', owner=None, profile='w1', token='legacy-token'):
        self.insert('browser_operation_leases', dict(profile_id=profile, owner_user_id=owner or self.owner,
                    operation_type=operation, entity_id=ident, lease_token=token,
                    acquired_at='2000-01-01', heartbeat_at='2000-01-01', expires_at='2000-01-01'))
        return self.records('browser_operation_leases')[-1]

    def asset(self, ident, job_id, *, table='posting_assets', content=b'original\x00asset bytes'):
        material = self.root / (ident + '.bin')
        material.write_bytes(content)
        if table == 'posting_assets':
            values = dict(id=ident, provider_id='remote-' + ident, job_id=job_id,
                          path=str(material), source_url='https://example.invalid/a', photographer='Fixture',
                          download_url='https://example.invalid/d', sha256=hashlib.sha256(content).hexdigest(),
                          created_at='2026-01-01')
        else:
            values = dict(id=ident, owner_user_id=self.owner, source='manual', name=ident,
                          path=str(material), media_type='image', created_at='2026-01-01')
        self.insert(table, values)
        return material, self.one(table, ident)

    def archived(self, table):
        path = Path(str(self.db.path) + '.posting-retirement') / 'archive.sqlite3'
        with closing(sqlite3.connect(path)) as connection:
            return [json.loads(row[0]) for row in connection.execute(
                'SELECT row_json FROM archived_rows WHERE source_table=?', (table,))]

    def test_idle_legacy_jobs_and_material_are_archived_exactly_then_removed(self):
        originals = [self.studio('studio-' + status, status=status,
                     kind='material' if status == 'prepared' else 'posting')
                     for status in ('queued', 'waiting_window', 'ready', 'prepared', 'paused')]
        post = self.posting('queued-post')
        material, asset = self.asset('asset', post['id'])
        summary = retire_legacy_posting(self.db)
        self.assertFalse(summary['quarantined_jobs'])
        self.assertEqual(7, summary['retired_rows'])
        self.assertEqual([], self.records('studio_jobs'))
        self.assertEqual([], self.records('posting_jobs'))
        self.assertCountEqual(originals, self.archived('studio_jobs'))
        self.assertEqual([post], self.archived('posting_jobs'))
        self.assertEqual([asset], self.archived('posting_assets'))
        self.assertEqual(b'original\x00asset bytes', material.read_bytes())
        with closing(sqlite3.connect(summary['archive_path'])) as archive:
            copied, size = archive.execute('SELECT archive_path,size_bytes FROM files').fetchone()
        self.assertEqual(material.read_bytes(), Path(copied).read_bytes())
        self.assertEqual(material.stat().st_size, size)
        self.assertEqual(7, len(self.records('posting_retirement_tombstones')))

    def test_terminal_success_and_cross_linked_history_leave_active_tables(self):
        original = self.posting('sent', status='completed', attempt_id='attempt', submitted_at='2026-01-01')
        receipt = dict(job_id='sent', owner_user_id=self.owner, profile_id='w1', username='fixture',
                       post_url='https://example.invalid/p/1', confirmed_at='2026-01-01',
                       day_utc='2026-01-01', evidence_json='{"confirmed":true}')
        self.insert('posting_receipts', receipt)
        self.insert('posting_retry_history', dict(id='retry', job_id='sent', owner_user_id=self.owner,
                    failure_stage='pre_submit', failure_code='fixture', retried_at='2026-01-01'))
        retire_legacy_posting(self.db)
        self.assertEqual([], self.records('posting_jobs'))
        self.assertEqual([], self.records('posting_receipts'))
        self.assertEqual([], self.records('posting_retry_history'))
        self.assertEqual([original], self.archived('posting_jobs'))
        self.assertEqual([receipt], self.archived('posting_receipts'))

    def test_active_uncertain_submitted_and_lock_rows_remain_byte_identical(self):
        for ident, fields in (
                ('running', {'status': 'running'}), ('uncertain', {'status': 'needs_review'}),
                ('submitting', {'status': 'submitting'}), ('token', {'lease_token': 'orphan-token'}),
                ('attempt', {'attempt_id': 'unknown-attempt'}), ('submitted', {'submitted_at': '2026-01-01'}),
                ('registered', {}), ('leased', {})):
            self.posting(ident, profile=ident, **fields)
        self.lease('leased', profile='leased')
        self.studio('legacy-live', status='running', profile='legacy-live')
        self.studio('legacy-effect', inflight=1, profile='legacy-effect')
        self.studio('legacy-hold', result={'window_hold': True}, profile='legacy-hold')
        before = {table: self.records(table) for table in ('posting_jobs', 'studio_jobs', 'browser_operation_leases')}
        summary = retire_legacy_posting(self.db, active_posting_ids={'registered'})
        self.assertEqual(11, len(summary['quarantined_jobs']))
        self.assertEqual(0, summary['retired_rows'])
        for table, rows in before.items():
            self.assertEqual(rows, self.records(table))

    def test_same_profile_other_owner_lease_is_never_removed_or_changed(self):
        self.posting('queued')
        original = self.lease('actual-other-owner', operation='account', owner=self.other)
        retire_legacy_posting(self.db)
        self.assertIsNone(self.one('posting_jobs', 'queued'))
        self.assertEqual([original], self.records('browser_operation_leases'))

    def test_lease_acquire_persist_crash_seam_is_quarantined(self):
        original = self.posting('seam', lease_token='')
        lease = self.lease('seam')
        summary = retire_legacy_posting(self.db)
        self.assertEqual(original, self.one('posting_jobs', 'seam'))
        self.assertEqual([lease], self.records('browser_operation_leases'))
        self.assertEqual(1, len(summary['quarantined_jobs']))

    def test_quarantine_assets_and_history_are_preserved(self):
        self.posting('uncertain', status='needs_review', asset_id='shared')
        path, asset = self.asset('shared', 'uncertain')
        self.studio('studio-uncertain', status='needs_review',
                    result={'prepared': {'asset_ids': ['studio-material']}})
        studio_path, studio_asset = self.asset('studio-material', None, table='studio_assets')
        retire_legacy_posting(self.db)
        self.assertEqual(asset, self.one('posting_assets', 'shared'))
        self.assertEqual(studio_asset, self.one('studio_assets', 'studio-material'))
        self.assertTrue(path.exists() and studio_path.exists())

    def test_nurture_login_inventory_and_shared_account_counts_unchanged(self):
        self.studio('nurture', kind='nurture', status='completed', result={'counts': {'like': 2}})
        self.insert('posting_account_snapshots', dict(owner_user_id=self.owner, profile_id='w1',
                    username='historical', posts_count=23, status='ok', checked_at='2026-01-01'))
        self.insert('native_browser_profiles', dict(id='native:fixture', owner_user_id=self.owner,
                    serial=1, name='Keep login', created_at='2026-01-01', updated_at='2026-01-01'))
        self.insert('account_window_plans', dict(id='plan', owner_user_id=self.owner, serial=1,
                    name='Keep inventory', profile_id='native:fixture', created_at='2026-01-01', updated_at='2026-01-01'))
        self.insert('studio_daily_actions', dict(owner_user_id=self.owner, profile_id='w1',
                    day='2026-01-01', action='like', count=2))
        login = self.root / 'browser-profiles' / 'fixture' / 'Cookies'
        login.parent.mkdir(parents=True)
        login.write_bytes(b'fixture login bytes never touch')
        tables = ('app_users', 'native_browser_profiles', 'account_window_plans',
                  'posting_account_snapshots', 'studio_daily_actions', 'studio_jobs', 'global_identity_owners')
        before = {table: self.records(table) for table in tables}
        self.posting('retire')
        retire_legacy_posting(self.db)
        for table in tables:
            self.assertEqual(before[table], self.records(table))
        self.assertEqual(b'fixture login bytes never touch', login.read_bytes())

    def test_idempotence_and_concurrent_retry(self):
        self.posting('once')
        self.studio('kept', status='needs_review')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: retire_legacy_posting(self.db), range(2)))
        self.assertEqual(1, sum(result['retired_rows'] for result in results))
        self.assertEqual(2, sum(result['archived_rows'] for result in results))
        before = self.records('posting_retirement_tombstones')
        repeated = retire_legacy_posting(self.db)
        self.assertEqual(0, repeated['retired_rows'])
        self.assertEqual(0, repeated['archived_rows'])
        self.assertEqual(before, self.records('posting_retirement_tombstones'))

    def test_missing_material_rolls_back_every_active_deletion(self):
        self.posting('queued')
        material, _ = self.asset('missing', 'queued')
        material.unlink()
        before = self.records('posting_jobs')
        with self.assertRaises(PostingRetirementError):
            retire_legacy_posting(self.db)
        self.assertEqual(before, self.records('posting_jobs'))
        self.assertIsNotNone(self.one('posting_assets', 'missing'))

    def test_corrupt_archive_is_not_accepted_and_retry_retains_source(self):
        original = self.posting('queued')
        with patch('app.posting_retirement._sync_dir', side_effect=OSError('simulated power failure')):
            with self.assertRaises(PostingRetirementError):
                retire_legacy_posting(self.db)
        self.assertEqual(original, self.one('posting_jobs', 'queued'))
        path = Path(str(self.db.path) + '.posting-retirement') / 'archive.sqlite3'
        with closing(sqlite3.connect(path)) as archive:
            archive.execute("UPDATE archived_rows SET row_json='corrupt'")
            archive.commit()
        with self.assertRaises(PostingRetirementError):
            retire_legacy_posting(self.db)
        self.assertEqual(original, self.one('posting_jobs', 'queued'))

    def test_archive_failure_after_commit_retries_safely(self):
        original = self.posting('queued')
        with patch('app.posting_retirement._sync_dir', side_effect=OSError('simulated interruption')):
            with self.assertRaises(PostingRetirementError):
                retire_legacy_posting(self.db)
        self.assertEqual(original, self.one('posting_jobs', 'queued'))
        self.assertEqual([original], self.archived('posting_jobs'))
        retried = retire_legacy_posting(self.db)
        self.assertEqual(0, retried['archived_rows'])
        self.assertEqual(1, retried['retired_rows'])

    def test_symlink_archive_and_material_are_rejected(self):
        self.posting('queued')
        material, _ = self.asset('asset', 'queued')
        outside = self.root / 'outside.bin'
        outside.write_bytes(b'outside')
        material.unlink()
        material.symlink_to(outside)
        with self.assertRaises(PostingRetirementError):
            retire_legacy_posting(self.db)
        self.assertIsNotNone(self.one('posting_jobs', 'queued'))
        self.assertEqual(b'outside', outside.read_bytes())

    def test_outside_root_material_is_never_moved_or_deleted(self):
        self.posting('queued')
        _, asset = self.asset('asset', 'queued')
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external) / 'original.bin'
            outside.write_bytes(b'external original')
            with self.db.write() as connection:
                connection.execute('UPDATE posting_assets SET path=? WHERE id=?', (str(outside), asset['id']))
            with self.assertRaises(PostingRetirementError):
                retire_legacy_posting(self.db)
            self.assertEqual(b'external original', outside.read_bytes())
            self.assertIsNotNone(self.one('posting_jobs', 'queued'))

    def test_stale_legacy_locks_survive_startup_and_occupancy_without_exposing_history(self):
        self.studio('legacy-private-id', status='running')
        lease = self.lease('legacy-private-id', operation='studio')
        retire_legacy_posting(self.db)
        self.service.recover_interrupted_operations()
        states = self.service.list_browser_lease_states(self.owner, active_studio_entity_ids=set(),
                                                       inactive_grace_seconds=0)
        self.assertEqual([lease], self.records('browser_operation_leases'))
        self.assertEqual(('account', 'occupied', None),
                         (states[0]['operation_type'], states[0]['state'], states[0]['entity_id']))
        with self.assertRaises(ConflictError):
            self.service.acquire_browser_lease(self.owner, 'w1', operation_type='account', entity_id='new')

    def test_missing_legacy_lease_is_still_fenced_and_visible_as_generic_occupied(self):
        self.posting('uncertain-private-id', status='needs_review')
        retire_legacy_posting(self.db)
        with self.assertRaises(ConflictError) as raised:
            self.service.acquire_browser_lease(self.owner, 'w1', operation_type='account', entity_id='new')
        self.assertEqual('unresolved_window_hold', raised.exception.details['reason'])
        self.assertNotIn('uncertain-private-id', str(raised.exception.details))
        state = self.service.list_browser_lease_states(self.owner)[0]
        self.assertEqual(('occupied', None), (state['state'], state['entity_id']))

    def test_retired_queue_no_longer_blocks_closed_nurture_recovery(self):
        self.studio('completed-nurture', kind='nurture', status='completed', result={'window_hold': True})
        self.posting('obsolete-queue')
        self.studio('obsolete-prepared', status='prepared', kind='material')
        retire_legacy_posting(self.db)
        @contextmanager
        def proof(profile, owner):
            yield {'closed': True, 'profile_id': profile, 'owner_user_id': owner,
                   'verification': 'desktop-absence-v1'}
        manager = SimpleNamespace(db=self.db, active_ids=lambda: set(), task_profiles={},
                                  bitbrowser=SimpleNamespace(closed_profile_guard=proof))
        response = reconcile_closed_nurture(manager, self.owner, 'completed-nurture')
        self.assertTrue(response['cleanup_reconciled'])
        self.assertFalse(json.loads(self.one('studio_jobs', 'completed-nurture')['result_json'])['window_hold'])

    def test_orphan_or_mismatched_studio_generation_survives_recovery(self):
        original = self.lease('missing-entity', operation='studio')
        self.service.recover_interrupted_operations()
        self.service.list_browser_lease_states(self.owner, active_studio_entity_ids=set(), inactive_grace_seconds=0)
        self.assertEqual([original], self.records('browser_operation_leases'))

    def test_unknown_failure_and_nonzero_unfinished_progress_stay_fenced(self):
        post = self.posting('unknown', status='failed', failure_stage='unknown')
        studio = self.studio('progress', status='paused', cursor=1)
        retire_legacy_posting(self.db)
        self.assertEqual(post, self.one('posting_jobs', 'unknown'))
        self.assertEqual(studio, self.one('studio_jobs', 'progress'))
        with self.assertRaises(ConflictError):
            self.service.acquire_browser_lease(self.owner, 'w1', operation_type='account', entity_id='new')

    def test_failed_backup_does_not_make_idle_queue_block_nurture_recovery(self):
        self.studio('completed-nurture', kind='nurture', status='completed', result={'window_hold': True})
        self.posting('idle')
        self.studio('idle-studio', status='paused')
        material, _ = self.asset('missing', 'idle')
        material.unlink()
        with self.assertRaises(PostingRetirementError):
            retire_legacy_posting(self.db)
        @contextmanager
        def proof(profile, owner):
            yield {'closed': True, 'profile_id': profile, 'owner_user_id': owner,
                   'verification': 'desktop-absence-v1'}
        manager = SimpleNamespace(db=self.db, active_ids=lambda: set(), task_profiles={},
                                  bitbrowser=SimpleNamespace(closed_profile_guard=proof))
        self.assertTrue(reconcile_closed_nurture(manager, self.owner, 'completed-nurture')['cleanup_reconciled'])
        self.assertIsNotNone(self.one('posting_jobs', 'idle'))

    def test_archive_directory_symlink_refuses_without_writing_target(self):
        self.posting('queued')
        with tempfile.TemporaryDirectory() as external:
            folder = Path(str(self.db.path) + '.posting-retirement')
            folder.symlink_to(external, target_is_directory=True)
            with self.assertRaises(PostingRetirementError):
                retire_legacy_posting(self.db)
            self.assertEqual([], list(Path(external).iterdir()))
        self.assertIsNotNone(self.one('posting_jobs', 'queued'))

    def test_corrupt_schema_backup_blocks_active_deletion(self):
        original = self.posting('queued')
        with patch('app.posting_retirement._sync_dir', side_effect=OSError('interrupted')):
            with self.assertRaises(PostingRetirementError):
                retire_legacy_posting(self.db)
        with closing(sqlite3.connect(Path(str(self.db.path) + '.posting-retirement') / 'archive.sqlite3')) as archive:
            archive.execute("UPDATE archived_schema SET schema_sql='corrupt'")
            archive.commit()
        with self.assertRaises(PostingRetirementError):
            retire_legacy_posting(self.db)
        self.assertEqual(original, self.one('posting_jobs', 'queued'))

    def test_archived_schema_rebuild_restores_dedup_indexes_and_triggers(self):
        self.posting('queued')
        self.asset('asset', 'queued')
        self.studio('draft-assignment', source_draft_id='permanent-draft')
        with self.db.write() as connection:
            connection.execute("UPDATE posting_assets SET render_sha256=? WHERE id='asset'",
                               (hashlib.sha256(b'rendered').hexdigest(),))
            connection.execute("CREATE TRIGGER posting_fixture_guard BEFORE INSERT ON posting_jobs "
                               "WHEN new.status='fixture-prohibited' BEGIN SELECT RAISE(ABORT,'fixture guard'); END")
        result = retire_legacy_posting(self.db)
        with closing(sqlite3.connect(result['archive_path'])) as archive:
            schemas = dict(archive.execute('SELECT source_table,schema_sql FROM archived_schema'))
            records = [(table, json.loads(payload)) for table, payload in archive.execute(
                'SELECT source_table,row_json FROM archived_rows')]
        with closing(sqlite3.connect(':memory:')) as restored:
            for ddl in schemas.values():
                restored.executescript(ddl)
            for table, row in records:
                restored.execute(f'INSERT INTO {table}(' + ','.join(row) + ') VALUES(' +
                                 ','.join('?' for _ in row) + ')', tuple(row.values()))
            asset = next(row for table, row in records if table == 'posting_assets')
            duplicate = dict(asset, id='duplicate', provider_id='different', job_id='other-job', render_sha256=None)
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'posting_assets.sha256'):
                restored.execute('INSERT INTO posting_assets(' + ','.join(duplicate) + ') VALUES(' +
                                 ','.join('?' for _ in duplicate) + ')', tuple(duplicate.values()))
            duplicate['sha256'] = None
            duplicate['render_sha256'] = asset['render_sha256']
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'posting_assets.render_sha256'):
                restored.execute('INSERT INTO posting_assets(' + ','.join(duplicate) + ') VALUES(' +
                                 ','.join('?' for _ in duplicate) + ')', tuple(duplicate.values()))
            draft = next(row for table, row in records if table == 'studio_jobs')
            duplicate = dict(draft, id='new-assignment', request_key='new-request')
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'studio_jobs.owner_user_id, studio_jobs.source_draft_id'):
                restored.execute('INSERT INTO studio_jobs(' + ','.join(duplicate) + ') VALUES(' +
                                 ','.join('?' for _ in duplicate) + ')', tuple(duplicate.values()))
            post = next(row for table, row in records if table == 'posting_jobs')
            duplicate = dict(post, id='new-job', request_key='new-request', status='fixture-prohibited')
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'fixture guard'):
                restored.execute('INSERT INTO posting_jobs(' + ','.join(duplicate) + ') VALUES(' +
                                 ','.join('?' for _ in duplicate) + ')', tuple(duplicate.values()))

    def test_foreign_queued_acquire_persist_seam_never_exposes_legacy_identity(self):
        for table, operation in (('posting_jobs', 'posting'), ('studio_jobs', 'studio')):
            profile = 'foreign-' + operation
            ident = 'private-' + operation + '-job'
            if table == 'posting_jobs':
                self.posting(ident, profile=profile, owner=self.other)
            else:
                self.studio(ident, profile=profile, owner=self.other)
            self.lease(ident, operation=operation, owner=self.other, profile=profile, token=profile)
            before = self.records('browser_operation_leases')
            retire_legacy_posting(self.db)
            with self.assertRaises(ConflictError) as error:
                self.service.acquire_browser_lease(self.owner, profile, operation_type='account', entity_id='new')
            self.assertEqual('account', error.exception.details['operation_type'])
            self.assertIsNone(error.exception.details['entity_id'])
            self.assertNotIn(ident, str(error.exception.details))
            self.assertEqual(before, self.records('browser_operation_leases'))

    def test_orphan_posting_lease_conflict_is_generic(self):
        original = self.lease('private-missing-job', owner=self.other)
        with self.assertRaises(ConflictError) as error:
            self.service.acquire_browser_lease(self.owner, 'w1', operation_type='account', entity_id='new')
        self.assertEqual('account', error.exception.details['operation_type'])
        self.assertIsNone(error.exception.details['entity_id'])
        self.assertEqual([original], self.records('browser_operation_leases'))

    def test_nested_uncertain_or_malformed_failure_is_quarantined(self):
        originals = []
        for index, failure in enumerate(({'result_uncertain': True}, {'stage': 'unknown'}, [], None)):
            originals.append(self.studio('nested-' + str(index), status='failed',
                                         profile='nested-' + str(index), result={'failure': failure}))
        summary = retire_legacy_posting(self.db)
        self.assertEqual(4, len(summary['quarantined_jobs']))
        self.assertCountEqual(originals, self.records('studio_jobs'))
        for row in originals:
            with self.assertRaises(ConflictError):
                self.service.acquire_browser_lease(self.owner, row['profile_id'], operation_type='account', entity_id='new')


class RetiredWindowReconciliationTests(unittest.TestCase):
    setUp = PostingRetirementTests.setUp
    posting = PostingRetirementTests.posting
    studio = PostingRetirementTests.studio
    insert = PostingRetirementTests.insert
    one = PostingRetirementTests.one
    records = PostingRetirementTests.records
    lease = PostingRetirementTests.lease
    asset = PostingRetirementTests.asset
    archived = PostingRetirementTests.archived
    def provider(self, *, before_proof=None, proof=None, error=None):
        @contextmanager
        def guard(profile, owner):
            if error:
                raise error
            if before_proof:
                before_proof()
            yield proof if proof is not None else {
                'closed': True, 'profile_id': profile, 'owner_user_id': owner,
                'verification': 'desktop-absence-v1'}
        return SimpleNamespace(closed_profile_guard=guard)

    def reconcile(self, provider=None, active=None):
        return reconcile_retired_posting(self.db, provider or self.provider(), self.owner, 'w1',
                                        active_profile=active or (lambda profile: False))

    def test_closed_uncertain_exact_generation_archives_then_releases_once(self):
        original = self.posting('uncertain', status='needs_review', lease_token='legacy-token', attempt_id='do-not-replay')
        lease = self.lease('uncertain')
        summary = self.reconcile()
        self.assertEqual('reconciled_closed', summary['status'])
        self.assertEqual([], self.records('browser_operation_leases'))
        self.assertEqual([], self.records('posting_jobs'))
        self.assertEqual([original], self.archived('posting_jobs'))
        self.assertEqual([lease], self.archived('browser_operation_leases'))
        self.assertEqual('already_clear', self.reconcile()['status'])
        with closing(sqlite3.connect(Path(str(self.db.path) + '.posting-retirement') / 'archive.sqlite3')) as archive:
            proof = json.loads(archive.execute('SELECT evidence_json FROM verified_closures').fetchone()[0])
        self.assertEqual('desktop-absence-v1', proof['verification'])
        token = self.service.acquire_browser_lease(self.owner, 'w1', operation_type='account', entity_id='new')
        self.service.release_browser_lease('w1', token)

    def test_closed_old_studio_uncertainty_and_asset_retire_without_outcome_rewrite(self):
        original = self.studio('uncertain', status='needs_review', inflight=1,
                               result={'prepared': {'asset_ids': ['material']}})
        material, asset = self.asset('material', None, table='studio_assets')
        self.lease('uncertain', operation='studio')
        self.reconcile()
        self.assertEqual([original], self.archived('studio_jobs'))
        self.assertEqual([asset], self.archived('studio_assets'))
        self.assertEqual([], self.records('studio_jobs'))
        self.assertTrue(material.exists())

    def test_missing_generation_can_only_clear_after_closed_proof(self):
        original = self.posting('missing', status='needs_review', lease_token='old-token')
        self.reconcile()
        self.assertEqual([original], self.archived('posting_jobs'))
        self.assertEqual([], self.records('posting_jobs'))

    def test_live_manager_or_live_token_refuses(self):
        original = self.posting('live', status='running', lease_token='legacy-token')
        lease = self.lease('live')
        with self.assertRaises(ConflictError):
            self.reconcile(active=lambda profile: True)
        self.db.live_browser_lease_tokens.add('legacy-token')
        with self.assertRaises(ConflictError):
            self.reconcile()
        self.assertEqual(original, self.one('posting_jobs', 'live'))
        self.assertEqual([lease], self.records('browser_operation_leases'))

    def test_foreign_or_successor_generation_is_not_removed(self):
        original = self.posting('legacy', status='needs_review', lease_token='old-token')
        lease = self.lease('successor', owner=self.other, operation='account')
        with self.assertRaises(ConflictError):
            self.reconcile()
        self.assertEqual(original, self.one('posting_jobs', 'legacy'))
        self.assertEqual([lease], self.records('browser_operation_leases'))

    def test_same_owner_mismatched_legacy_token_refuses(self):
        self.posting('legacy', status='needs_review', lease_token='old-token')
        lease = self.lease('legacy', token='different-token')
        with self.assertRaises(ConflictError):
            self.reconcile()
        self.assertEqual([lease], self.records('browser_operation_leases'))

    def test_profile_with_other_owners_durable_job_refuses(self):
        self.posting('owner-job', status='needs_review')
        other = self.posting('other-job', status='needs_review', owner=self.other)
        with self.assertRaises(ConflictError):
            self.reconcile()
        self.assertEqual(other, self.one('posting_jobs', 'other-job'))

    def test_open_unknown_and_malformed_proof_retain_all_state(self):
        original = self.posting('legacy', status='needs_review')
        for provider in (self.provider(error=ConflictError('open')),
                         self.provider(error=UpstreamUnavailableError('unknown')),
                         self.provider(proof={}), self.provider(proof={'closed': True})):
            with self.assertRaises((ConflictError, UpstreamUnavailableError)):
                self.reconcile(provider)
            self.assertEqual(original, self.one('posting_jobs', 'legacy'))

    def test_new_generation_between_checks_refuses_and_preserves_successor(self):
        original = self.posting('legacy', status='needs_review')
        provider = self.provider(before_proof=lambda: self.lease('successor', operation='account'))
        with self.assertRaises(ConflictError):
            self.reconcile(provider)
        self.assertEqual(original, self.one('posting_jobs', 'legacy'))
        self.assertEqual('successor', self.records('browser_operation_leases')[0]['entity_id'])

    def test_active_owner_appearing_during_proof_refuses(self):
        original = self.posting('legacy', status='needs_review')
        active = [False]
        provider = self.provider(before_proof=lambda: active.__setitem__(0, True))
        with self.assertRaises(ConflictError):
            self.reconcile(provider, active=lambda profile: active[0])
        self.assertEqual(original, self.one('posting_jobs', 'legacy'))

    def test_backup_failure_keeps_exact_closed_lease(self):
        original = self.posting('legacy', status='needs_review')
        lease = self.lease('legacy')
        path, _ = self.asset('missing', 'legacy')
        path.unlink()
        with self.assertRaises(PostingRetirementError):
            self.reconcile()
        self.assertEqual(original, self.one('posting_jobs', 'legacy'))
        self.assertEqual([lease], self.records('browser_operation_leases'))

    def test_generic_other_owner_lock_not_offered_for_reconciliation(self):
        self.posting('private', status='needs_review', owner=self.other)
        state = self.service.list_browser_lease_states(self.owner)[0]
        self.assertNotIn('can_reconcile_window_state', state)
        self.assertIsNone(state['entity_id'])

    def test_new_posting_lease_cannot_be_created(self):
        self.posting('queued')
        with self.assertRaises(ValidationError):
            self.service.acquire_browser_lease(self.owner, 'w1', operation_type='posting', entity_id='queued')
        self.assertEqual([], self.records('browser_operation_leases'))

    def test_already_clear_does_not_require_provider_guard(self):
        self.assertEqual('already_clear', self.reconcile(SimpleNamespace())['status'])

    def test_closed_proof_retry_can_use_new_verification_timestamp(self):
        original = self.posting('legacy', status='needs_review')
        first = {'closed': True, 'profile_id': 'w1', 'owner_user_id': self.owner,
                 'verification': 'desktop-absence-v1', 'checked_at': 'first'}
        with patch('app.posting_retirement._sync_dir', side_effect=OSError('interrupted')):
            with self.assertRaises(PostingRetirementError):
                self.reconcile(self.provider(proof=first))
        self.assertEqual(original, self.one('posting_jobs', 'legacy'))
        second = dict(first, checked_at='second')
        self.assertEqual('reconciled_closed', self.reconcile(self.provider(proof=second))['status'])


if __name__ == '__main__':
    unittest.main()
