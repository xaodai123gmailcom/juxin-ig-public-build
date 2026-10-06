"""Observed, unconfirmed list identities survive recovery without entering spool."""
import asyncio
import sqlite3
import unittest
from unittest.mock import AsyncMock

from app.errors import ConflictError
from app.execution_manager import ExecutionManager
from app.playwright_worker import CollectionOutcome, WorkerExecutionError
from test_core import FakeCollectionWorker
import test_collection_drain_r56 as drain


class RelationPendingCheckpointR97Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = drain.CollectionDrainR56Tests.asyncSetUp
    asyncTearDown = drain.CollectionDrainR56Tests.asyncTearDown
    task = drain.CollectionDrainR56Tests.task
    manager = drain.CollectionDrainR56Tests.manager
    gate = drain.CollectionDrainR56Tests.gate
    until = drain.CollectionDrainR56Tests.until
    leases = drain.CollectionDrainR56Tests.leases

    async def test_unconfirmed_identity_survives_login_stop_restart_without_queue_or_count(self):
        task = self.task()
        target = task['targets'][0]['id']
        class Interrupted(FakeCollectionWorker):
            supports_collection_progress_sink = True
            supports_candidate_batch_sink = True
            async def collect_followers(self, username, *, progress_sink, **kwargs):
                await progress_sink({'source_total': 1, 'pending_relation_usernames': ['Ghost', '@GHOST', 'fb:bad', {}, 'bad/name']})
                raise WorkerExecutionError('login required', reason='instagram_login_required', pause_required=True)
        manager, _ = self.manager(Interrupted)
        manager._recover_network_connection = AsyncMock(return_value=None)
        await manager.start(self.owner, task['id'])
        await self.until(lambda: self.service.get_task(self.owner, task['id'])['targets'][0]['status'] == 'recoverable')
        stats = self.service.task_mode_candidate_stats(self.owner, task['id'], target, 'followers')
        self.assertEqual({'total': 0, 'pending': 0, 'recorded': 0, 'deduped': 0}, stats)
        checkpoint = self.service.get_checkpoint(self.owner, task['id'], target, 'followers')
        self.assertEqual(['ghost'], ExecutionManager._checkpoint_resume_cursor(checkpoint)['pending_relation_usernames'])
        self.assertFalse(self.service.get_task(self.owner, task['id'])['targets'][0]['collection_list_dismissed'])
        await manager.stop(self.owner, task['id'])
        self.database.initialize()
        self.service.recover_interrupted_operations()
        received, reads = [], []
        outer = self
        class Resumed(FakeCollectionWorker):
            supports_collection_progress_sink = True
            supports_candidate_batch_sink = True
            async def collect_followers(self, username, *, progress_sink, candidate_sink, initial_pending_relation_usernames, **kwargs):
                received.extend(initial_pending_relation_usernames)
                await progress_sink({'source_total': 1})  # omission retains obligations
                saved = outer.service.get_checkpoint(outer.owner, task['id'], target, 'followers')
                outer.assertEqual(['ghost'], saved['cursor']['pending_relation_usernames'])
                await candidate_sink(['ghost'])  # real worker calls only after hover/dedup
                await progress_sink({'pending_relation_usernames': []})
                return CollectionOutcome('followers', [], source_total=1)
            async def read_visible_profile(self, username, **kwargs):
                if not kwargs.get('include_activity'):
                    reads.append(username)
                return await super().read_visible_profile(username, **kwargs)
        resumed, _ = self.manager(Resumed)
        await resumed.retry_target(self.owner, task['id'], target)
        await asyncio.wait_for(resumed.wait(task['id']), 10)
        self.assertEqual(['ghost'], received)
        self.assertEqual(['ghost'], reads)
        current = self.service.get_task(self.owner, task['id'])['targets'][0]
        self.assertEqual('completed', current['status'])
        self.assertTrue(current['collection_list_dismissed'])
        self.assertEqual([], self.service.get_checkpoint(self.owner, task['id'], target, 'followers')['cursor']['pending_relation_usernames'])

    async def test_stale_natural_end_cannot_complete_with_wrapped_unconfirmed_identity(self):
        task = self.task()
        target = task['targets'][0]['id']
        cursor = {'candidate_spool_complete': True, 'candidate_spool_natural_end': True,
                  'pending_relation_usernames': ['ghost']}
        checkpoint = {'cursor': {'resume_cursor': cursor}}
        self.assertFalse(ExecutionManager._candidate_spool_complete(checkpoint, require_natural_end=True))
        self.service.upsert_checkpoint(self.owner, task['id'], target, mode='followers', stage='mode_completed',
            cursor=checkpoint['cursor'], counters={'source_total': 1, 'discovered': 0, 'processed': 0})
        with self.assertRaises(ConflictError):
            self.service.set_target_runtime_status(self.owner, task['id'], target, 'completed')
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.write() as connection:
                connection.execute("UPDATE task_targets SET status='completed' WHERE id=?", (target,))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.write() as connection:
                connection.execute("UPDATE tasks SET status='completed' WHERE id=?", (task['id'],))

    async def test_unsupported_adapter_and_overflow_fail_closed(self):
        with self.assertRaises(WorkerExecutionError):
            await ExecutionManager._collect_mode(FakeCollectionWorker(None), 'source', 'followers', {},
                initial_pending_relation_usernames=['ghost'])
        with self.assertRaises(WorkerExecutionError):
            ExecutionManager._normalize_pending_relation_usernames([f'person_{i}' for i in range(1001)])
        self.assertEqual(['ghost'], ExecutionManager._normalize_pending_relation_usernames([' Ghost ', '@GHOST', 'fb:wrong', 'url/user']))

    async def test_committed_candidate_clears_stale_obligation_without_reseeing_row(self):
        task = self.task()
        target = task['targets'][0]['id']
        self.service.append_task_mode_candidates(self.owner, task['id'], target, 'followers', ['ghost'])
        self.service.upsert_checkpoint(self.owner, task['id'], target, mode='followers', stage='discovering_accounts',
            cursor={'candidate_spool_complete': False, 'pending_relation_usernames': ['ghost']},
            counters={'source_total': 1, 'discovered': 1, 'processed': 0})
        observed, reads = [], []
        class Resumed(FakeCollectionWorker):
            async def collect_followers(self, username, *, initial_pending_relation_usernames=(), **kwargs):
                observed.extend(initial_pending_relation_usernames)
                # The row disappeared, but its committed candidate is proof of handoff.
                return CollectionOutcome('followers', [], source_total=1)
            async def read_visible_profile(self, username, **kwargs):
                if not kwargs.get('include_activity'):
                    reads.append(username)
                return await super().read_visible_profile(username, **kwargs)
        manager, _ = self.manager(Resumed)
        await manager.start(self.owner, task['id'])
        await asyncio.wait_for(manager.wait(task['id']), 10)
        self.assertEqual([], observed)
        self.assertEqual(['ghost'], reads)
        current = self.service.get_task(self.owner, task['id'])['targets'][0]
        self.assertEqual('completed', current['status'])
        self.assertEqual(1, current['mode_coverage']['followers']['processed'])
        self.assertEqual([], self.service.get_checkpoint(self.owner, task['id'], target, 'followers')['cursor']['pending_relation_usernames'])

    async def test_adapter_false_natural_return_with_unconfirmed_names_cannot_complete(self):
        task = self.task()
        target = task['targets'][0]['id']
        class BrokenAdapter(FakeCollectionWorker):
            supports_collection_progress_sink = True
            async def collect_followers(self, username, *, progress_sink, **kwargs):
                await progress_sink({'source_total': 3, 'pending_relation_usernames': ['unconfirmed']})
                return CollectionOutcome('followers', [], source_total=3)
        manager, _ = self.manager(BrokenAdapter)
        await manager.start(self.owner, task['id'])
        await self.until(lambda: self.service.get_task(self.owner, task['id'])['targets'][0]['status'] == 'recoverable')
        current = self.service.get_task(self.owner, task['id'])['targets'][0]
        self.assertFalse(current['collection_list_dismissed'])
        self.assertFalse(current['mode_coverage']['followers']['discovery_finished'])
        self.assertIsNone(current['completion_policy'])
        self.assertNotIn('automatic_recheck', current['mode_coverage']['followers'])
        self.assertEqual(0, self.service.task_mode_candidate_stats(self.owner, task['id'], target, 'followers')['total'])
        await manager.stop(self.owner, task['id'])
