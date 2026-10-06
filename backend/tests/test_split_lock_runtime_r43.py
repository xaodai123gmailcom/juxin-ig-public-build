"""Independent r43 audit: real SQLite and ExecutionManager, controlled source only."""
from __future__ import annotations
import asyncio
from collections import Counter
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.service import CoreService
from app.execution_manager import ExecutionManager
from app.errors import ConflictError
from app.playwright_worker import CollectionOutcome
from support.legacy_split_fixture import insert_legacy_waiting_generation


class Browser:
    def close_profile(self, _profile):
        return {'closed': True}


class Worker:
    supports_candidate_batch_sink = True
    supports_collection_progress_sink = True

    def __init__(self, audit):
        self.audit = audit
        self.profile_id = None

    async def connect(self, profile_id, **_kwargs):
        self.profile_id = profile_id
        self.audit.connections.append(profile_id)
        if profile_id in self.audit.connect_gates:
            await self.audit.connect_gates[profile_id].wait()

    async def disconnect(self):
        pass

    async def connection_healthy(self):
        return True

    async def collect_followers(self, target, *, limit, candidate_sink,
                                initial_candidate_count, progress_sink, **_kwargs):
        self.audit.calls.append((self.profile_id, target))
        if target in self.audit.gates:
            await self.audit.gates[target].wait()
        await progress_sink({'source_total': 0})
        return CollectionOutcome('followers', [], source_total=0)


class SplitLockRuntimeR43Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / 'audit.sqlite3')
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user('dispatch-audit', 'long audit password')['id']
        self.calls = []
        self.connections = []
        self.gates = {}
        self.connect_gates = {}
        self.manager = ExecutionManager(self.service, Browser(), worker_factory=lambda _: Worker(self))

    async def asyncTearDown(self):
        for gate in self.gates.values():
            gate.set()
        for gate in self.connect_gates.values():
            gate.set()
        await self.manager.shutdown()
        self.temp.cleanup()

    def task(self, names, *, live=True, manual=False):
        return self.service.create_task(
            self.owner, name='independent dispatch audit', targets=names,
            modes=['followers'], window_ids=['audit-window'],
            assignment_mode='manual' if manual else 'sequential',
            settings={'live_queue_enabled': live, 'local_person_recognition': False,
                      'location_enabled': False})

    def legacy_overlapping_row(self, name):
        # Existing pre-r55 databases may contain both a direct task target and a
        # waiting row for one username. New admission must reject that overlap;
        # build historical state explicitly to keep its lock/recovery coverage.
        outcome = self.service.upsert_manual_split_candidates(
            self.owner, [{'username': name, 'queued': True}], include_outcome=True)
        self.assertEqual(0, outcome['accepted_count'])
        self.assertEqual('split_already_queued', outcome['duplicates'][0]['reason'])
        return insert_legacy_waiting_generation(self.service, self.owner, name, [])

    def target_status(self, task, name):
        return next(target['status'] for target in self.service.get_task(self.owner, task['id'])['targets']
                    if target['username'] == name)

    # This watchdog checks convergence, not a subsecond performance SLA.
    # Leave scheduling margin for the Windows builder on busy/slow disks.
    async def until(self, predicate, *, timeout=5):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.005)

    async def settle(self):
        await asyncio.sleep(.06)

    def lock_owner(self, value):
        self.service.set_split_claim_locked(self.owner, value)
        self.manager.notify_split_queue(self.owner)

    def lock_row(self, row, value):
        self.service.set_split_candidate_locked(self.owner, row['id'], value)
        self.manager.notify_split_queue(self.owner)

    async def test_current_completes_then_global_lock_blocks_shared_and_added(self):
        self.gates['current_source'] = asyncio.Event()
        task = self.task(['current_source', 'next_source'])
        await self.manager.start(self.owner, task['id'])
        await self.until(lambda: bool(self.calls))
        control = self.manager._runs[task['id']]
        token = control.leases['audit-window']
        self.lock_owner(True)
        await self.manager.add_targets(self.owner, task['id'], ['new_while_locked'])
        self.gates['current_source'].set()
        await self.until(lambda: self.target_status(task, 'current_source') == 'completed')
        await self.settle()
        self.assertEqual(self.calls, [('audit-window', 'current_source')])
        self.assertEqual(self.target_status(task, 'next_source'), 'pending')
        self.assertEqual(self.target_status(task, 'new_while_locked'), 'pending')
        self.assertFalse(control.coordinator.done())
        self.assertEqual(control.leases.get('audit-window'), token)
        self.assertEqual(self.service.get_task(self.owner, task['id'])['status'], 'running')
        self.lock_owner(False)
        await self.until(lambda: len(self.calls) == 3)
        self.assertEqual(Counter(name for _, name in self.calls),
                         Counter(['current_source', 'next_source', 'new_while_locked']))

    async def test_owner_unlock_preserves_individual_lock_and_skips_to_other(self):
        # Restore an old pending-task/waiting-row overlap and verify that both
        # the durable gate and in-memory queue continue to respect its lock.
        task = self.task(['individual_locked', 'eligible_source'])
        row = self.legacy_overlapping_row('individual_locked')
        self.lock_row(row, True)
        self.lock_owner(True)
        await self.manager.start(self.owner, task['id'])
        await self.until(lambda: bool(self.connections))
        await self.settle()
        self.assertEqual(self.calls, [])
        self.lock_owner(False)
        await self.until(lambda: ('audit-window', 'eligible_source') in self.calls)
        await self.settle()
        self.assertEqual(self.calls, [('audit-window', 'eligible_source')])
        self.assertEqual(self.target_status(task, 'individual_locked'), 'pending')
        self.lock_row(row, False)
        await self.until(lambda: len(self.calls) == 2)
        self.assertEqual(self.calls[-1], ('audit-window', 'individual_locked'))

    async def test_locked_preferred_non_live_waits_and_preserves_pause_stop(self):
        # Preserve coverage of an already-created manual task whose source later
        # appears in the waiting pool; the preferred payload must obey its lock.
        task = self.task(['preferred_source'], live=False, manual=True)
        row = self.legacy_overlapping_row('preferred_source')
        self.lock_row(row, True)
        self.service.set_manual_assignments(self.owner, task['id'], {'audit-window': 'preferred_source'})
        await self.manager.start(self.owner, task['id'])
        await self.until(lambda: bool(self.connections))
        await self.settle()
        control = self.manager._runs[task['id']]
        self.assertEqual(self.calls, [])
        self.assertFalse(control.coordinator.done())
        self.assertIn('audit-window', control.leases)
        await asyncio.wait_for(self.manager.pause(self.owner, task['id']), 5)
        self.lock_row(row, False)
        await self.settle()
        self.assertEqual(self.calls, [])
        await asyncio.wait_for(self.manager.resume(self.owner, task['id']), 5)
        await self.until(lambda: len(self.calls) == 1)
        await self.until(lambda: control.coordinator.done())
        self.assertEqual(self.service.get_task(self.owner, task['id'])['status'], 'completed')

    async def test_lock_wait_stop_responsive(self):
        self.lock_owner(True)
        task = self.task(['stop_locked'], live=False)
        await self.manager.start(self.owner, task['id'])
        await self.until(lambda: bool(self.connections))
        await self.settle()
        control = self.manager._runs[task['id']]
        self.assertFalse(control.coordinator.done())
        await asyncio.wait_for(self.manager.stop(self.owner, task['id']), 5)
        self.assertTrue(control.coordinator.done())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service.get_task(self.owner, task['id'])['status'], 'stopped')
        with self.database.read() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM browser_operation_leases').fetchone()[0], 0)

    async def test_non_live_other_window_only_does_not_leave_incompatible_window_hung(self):
        self.connect_gates['window_b'] = asyncio.Event()
        task = self.service.create_task(
            self.owner, name='incompatible window queue audit', targets=['only_for_b'],
            modes=['followers'], window_ids=['window_a', 'window_b'],
            settings={'live_queue_enabled': False, 'local_person_recognition': False,
                      'location_enabled': False})
        with self.database.write() as connection:
            connection.execute('UPDATE task_targets SET allowed_window_ids_json=? WHERE id=?',
                               ('["window_b"]', task['targets'][0]['id']))
        await self.manager.start(self.owner, task['id'])
        await self.until(lambda: len(self.connections) == 2)
        await self.settle()
        self.connect_gates['window_b'].set()
        await self.until(lambda: self.target_status(task, 'only_for_b') == 'completed')
        control = self.manager._runs[task['id']]
        await self.until(lambda: control.coordinator.done(), timeout=5)
        self.assertEqual(self.service.get_task(self.owner, task['id'])['status'], 'completed')

    async def test_service_transaction_guards_new_claim_allows_owned_recovery_persists(self):
        task = self.task(['owned_source', 'new_source'])
        owned, new = task['targets']
        self.service.set_target_runtime_status(self.owner, task['id'], owned['id'], 'running', window_id='audit-window')
        row = self.legacy_overlapping_row('new_source')
        self.lock_row(row, True)
        self.lock_owner(True)
        self.service.set_target_runtime_status(self.owner, task['id'], owned['id'], 'waiting_network', window_id='audit-window')
        self.service.set_target_runtime_status(self.owner, task['id'], owned['id'], 'running', window_id='audit-window')
        with self.assertRaises(ConflictError):
            self.service.set_target_runtime_status(self.owner, task['id'], new['id'], 'running', window_id='audit-window')
        self.database.initialize()
        reloaded = CoreService(Database(self.database.path))
        self.assertTrue(reloaded.get_split_claim_locked(self.owner))
        self.assertIn('new_source', reloaded.collection_dispatch_state(self.owner)['usernames'])
        reloaded.set_split_claim_locked(self.owner, False)
        self.assertIn('new_source', reloaded.collection_dispatch_state(self.owner)['usernames'])
        with self.assertRaises(ConflictError):
            reloaded.set_target_runtime_status(self.owner, task['id'], new['id'], 'running', window_id='audit-window')


if __name__ == '__main__':
    unittest.main(verbosity=2)
