"""Installed completed-card proof with small fixtures and no browser execution."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from app.database import Database
from app.errors import ValidationError
from app.service import CoreService

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from completed_card_smoke_fixture import (
    PREFIX, SOURCE, WINDOW, assert_completed_card_dto, assert_completed_card_retained,
    completed_card_state, seed_completed_card_smoke,
)


class InstalledCompletedCardFixtureR96Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='Juxin-CoreSmoke-test-r96-')
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'collector.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('install-smoke', 'synthetic smoke password')['id']

    def task(self, **kwargs):
        return self.service.create_task(self.owner, name='Installed completed-card fixture',
            modes=kwargs.pop('modes', ['followers']), targets=kwargs.pop('targets', [SOURCE]),
            window_ids=[WINDOW], settings={'platform': kwargs.pop('platform', 'instagram')}, **kwargs)

    def seed(self, task):
        return seed_completed_card_smoke(self.db.path, self.owner, task['id'], task['targets'][0]['id'],
                                         isolated_directory=self.temp.name)

    def dump(self):
        with self.db.read() as connection:
            return list(connection.iterdump())

    def test_real_dto_completed_recheck_gap_and_dismissal_retain_nonempty_evidence(self):
        task = self.task()
        fixture = self.seed(task)
        before = completed_card_state(self.db.path, fixture)
        for table in ('task_results', 'task_checkpoints', 'task_source_rechecks',
                      'split_candidate_history', 'split_completed_targets'):
            self.assertEqual(1, len(before['rows'][table]), table)
        self.assertEqual(2, before['counts']['global_seen'])
        self.assertEqual([], before['rows']['browser_operation_leases'])
        assert_completed_card_dto(self.service.get_task(self.owner, task['id']), fixture, dismissed=False)
        result = self.service.dismiss_completed_task_target(self.owner, task['id'], fixture['target_id'])
        self.assertIs(result['collection_list_dismissed'], True)
        after = completed_card_state(self.db.path, fixture)
        assert_completed_card_retained(before, after, fixture)
        self.service.dismiss_completed_task_target(self.owner, task['id'], fixture['target_id'])
        self.assertEqual(after, completed_card_state(self.db.path, fixture))
        self.db.initialize()
        self.assertEqual(after, completed_card_state(self.db.path, fixture))
        for current in (self.service.get_task(self.owner, task['id']),
                        self.service.list_tasks_with_details(self.owner, platform='instagram')[0]):
            assert_completed_card_dto(current, fixture, dismissed=True)
        with self.assertRaises(ValidationError):
            self.service.list_tasks_with_details(self.owner, platform='facebook')

    def test_repeat_seed_refuses_without_any_data_change(self):
        task = self.task()
        self.seed(task)
        before = self.dump()
        with self.assertRaisesRegex(ValueError, 'fresh owned'):
            self.seed(task)
        self.assertEqual(before, self.dump())

    def test_wrong_owner_platform_target_and_nonfresh_task_are_rejected(self):
        task = self.task()
        # Inject dormant legacy state after initialization; new non-IG tasks are rejected.
        facebook = self.task(targets=['legacy.installed.card.source'])
        with self.db.write() as connection:
            connection.execute("UPDATE tasks SET settings_json=json_set(settings_json,'$.platform','facebook') WHERE id=?", (facebook['id'],))
            connection.execute("UPDATE task_targets SET username_norm='fb:installed.card.source',username_display='fb:installed.card.source' WHERE task_id=?", (facebook['id'],))
        before = self.dump()
        for owner, task_id, target_id in (
            ('other-owner', task['id'], task['targets'][0]['id']),
            (self.owner, facebook['id'], facebook['targets'][0]['id']),
            (self.owner, task['id'], facebook['targets'][0]['id']),
        ):
            with self.subTest(owner=owner, task=task_id, target=target_id), self.assertRaises(ValueError):
                seed_completed_card_smoke(self.db.path, owner, task_id, target_id, isolated_directory=self.temp.name)
            self.assertEqual(before, self.dump())
        with self.db.write() as connection:
            connection.execute("UPDATE tasks SET status='running' WHERE id=?", (task['id'],))
        before = self.dump()
        with self.assertRaisesRegex(ValueError, 'fresh owned'):
            self.seed(task)
        self.assertEqual(before, self.dump())

    def test_explicit_directory_guard_and_existing_target_evidence_refuse(self):
        task = self.task()
        before = self.dump()
        with tempfile.TemporaryDirectory() as unrelated, self.assertRaisesRegex(ValueError, 'isolated probe'):
            seed_completed_card_smoke(self.db.path, self.owner, task['id'], task['targets'][0]['id'],
                                      isolated_directory=unrelated)
        self.assertEqual(before, self.dump())
        self.service.upsert_checkpoint(self.owner, task['id'], task['targets'][0]['id'], mode='followers',
            stage='screening_accounts', cursor={}, counters={})
        before = self.dump()
        with self.assertRaisesRegex(ValueError, 'existing target evidence'):
            self.seed(task)
        self.assertEqual(before, self.dump())

    def test_collision_and_late_write_error_roll_back_all_seed_rows(self):
        task = self.task()
        with self.db.write() as connection:
            connection.execute('INSERT INTO instagram_accounts VALUES(?,NULL,?,?,?,?)',
                (PREFIX + 'account', 'other.fixture', 'other.fixture', task['created_at'], task['created_at']))
        before = self.dump()
        with self.assertRaisesRegex(ValueError, 'fixture IDs'):
            self.seed(task)
        self.assertEqual(before, self.dump())
        with self.db.write() as connection:
            connection.execute('DELETE FROM instagram_accounts WHERE id=?', (PREFIX + 'account',))
            connection.execute("CREATE TRIGGER fixture_fail BEFORE UPDATE OF status ON tasks "
                "WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'fixture write fault'); END")
        before = self.dump()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'fixture write fault'):
            self.seed(task)
        self.assertEqual(before, self.dump())

    def test_dto_and_retention_assertions_reject_missing_or_corrupt_proof(self):
        task = self.task()
        fixture = self.seed(task)
        dto = self.service.get_task(self.owner, task['id'])
        for field, value in [('collection_list_dismissed', None), ('collection_list_dismissed', 0),
                             ('source_recheck', None), ('source_recheck', {'state': 'completed'})]:
            broken = deepcopy(dto)
            broken['targets'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                assert_completed_card_dto(broken, fixture, dismissed=False)
        before = completed_card_state(self.db.path, fixture)
        self.service.dismiss_completed_task_target(self.owner, task['id'], fixture['target_id'])
        after = completed_card_state(self.db.path, fixture)
        for key in ('rows', 'counts', 'marker', 'events'):
            broken = deepcopy(after)
            if key in ('rows', 'counts'):
                broken[key]['task_results'] = [] if key == 'rows' else 0
            else:
                broken[key] = []
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                assert_completed_card_retained(before, broken, fixture)


class InstalledCompletedCardProcessR96Tests(unittest.TestCase):
    def test_real_http_probe_checks_dismissal_idempotence_platform_and_restart(self):
        spec = importlib.util.spec_from_file_location('completed_card_core_probe', ROOT / 'scripts/verify_frozen_core_service.py')
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        paths = [str(ROOT / 'backend'), *[str(Path(p).resolve()) for p in sys.path if p]]
        code = f'import sys; sys.path[:0]={json.dumps(paths)}; from app.__main__ import main; main()'
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'completed-card-core.log'
            try:
                result = probe.probe_core([sys.executable, '-c', code], log, timeout=45,
                    snapshot_scale_smoke=True, snapshot_result_count=12, snapshot_identity_count=20)
            except Exception as error:
                self.fail(f'{error}\n{log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""}')
        proof = result['snapshot_scale']['completed_card_dismissal']
        for key in ('verified', 'persistence_after_restart', 'platform_isolation', 'idempotent',
                    'completed_recheck_positive_gap', 'isolated_temporary_database'):
            self.assertIs(proof[key], True, key)
        self.assertIs(proof['user_data_touched'], False)
        self.assertIs(proof['retained_data'], True)
        self.assertEqual(13, proof['retained_data_details']['ledger_counts']['task_results'])
        # The new pure-IG upgrade proof deliberately adds one retained IG sentinel.
        self.assertEqual(23, proof['retained_data_details']['ledger_counts']['global_seen'])
        self.assertTrue(result['snapshot_scale']['pure_ig_upgrade']['ig_identity_hash_preserved'])


if __name__ == '__main__':
    unittest.main()
