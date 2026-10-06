"""Real SQLite ownership for legacy handoff and parallel batch collection."""
import asyncio
from contextlib import contextmanager
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.database import Database
from app.playwright_worker import CollectionOutcome, PlaywrightWorker, WorkerExecutionError
from app.service import CoreService
from app.execution_manager import ExecutionManager
from completion_wait import wait_for_collection_operation
import test_parallel_relation_pipeline as fixture
from test_parallel_relation_pipeline import (
    _PipelineState, _RelationParent, _ScreeningChild, _PipelineManager, _NoopBitBrowser)


class SingleHandoffR94Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ParallelRelationPipelineTests.asyncSetUp
    asyncTearDown = fixture.ParallelRelationPipelineTests.asyncTearDown
    _task_and_control = fixture.ParallelRelationPipelineTests._task_and_control

    def pipeline(self, count, *, names=None, create_error=False, fail=False, checkpoint=None,
                 fixture=None, pooled=False, batch=False):
        task, target, control = fixture or self._task_and_control(count)
        state = _PipelineState(f'direct{count}')
        state.candidates = names or [f'direct{count}_{i}' for i in range(count + 3)]
        state.gates = {name: asyncio.Event() for name in state.candidates}
        state.started = {name: asyncio.Event() for name in state.candidates}
        state.delivered = []
        state.attempts = []
        state.source_calls = 0
        state.source_resume_tails = []
        state.children = []
        state.pool_releases = []
        class Child(_ScreeningChild):
            async def screen(self, username):
                state.attempts.append((id(self), username))
                state.started[username].set()
                await state.gates[username].wait()
                if fail is True or isinstance(fail, set) and username in fail:
                    error = WorkerExecutionError('child unavailable', reason='instagram_page_recovery_exhausted')
                    error.details['original_reason'] = 'instagram_profile_not_ready'
                    raise error
                state.screened.append(('child', username))
        class Source(_RelationParent):
            supports_single_candidate_handoff = True
            collection_pipeline = PlaywrightWorker.collection_pipeline if batch else None
            def release_parallel_screening_worker(self, child):
                if pooled:
                    state.pool_releases.append(child)
                return pooled
            async def create_parallel_screening_worker(self):
                if create_error: raise RuntimeError('cannot create child')
                child = Child(state); state.children.append(child); return child
            async def collect_followers(self, _target, *, candidate_sink, hover_precheck=True, **kwargs):
                if hover_precheck: raise AssertionError('hover must be disabled')
                state.source_calls += 1
                state.source_resume_tails.append(list(kwargs.get('initial_resume_tail') or []))
                state.source_running = True
                try:
                    if batch:
                        await candidate_sink(list(state.candidates))
                        state.delivered.extend(state.candidates)
                    else:
                        for name in state.candidates:
                            await candidate_sink([name])
                            state.delivered.append(name)
                    return CollectionOutcome('followers', [], source_total=len(state.candidates))
                finally:
                    state.source_running = False
            async def screen(self, username):
                raise AssertionError('parent page must never screen candidate profiles')
        source = Source(state)
        manager = _PipelineManager(self.service, _NoopBitBrowser())
        state.completion_context = (manager, control)
        job = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, source, target, 'followers', task['settings'], checkpoint))
        return task, target, control, state, source, job

    async def until(self, predicate, job):
        async with asyncio.timeout(10):
            while not predicate():
                if job.done(): job.result(); self.fail('pipeline finished before expected boundary')
                await asyncio.sleep(.005)

    def stats(self, task, target):
        return self.service.task_mode_candidate_stats(self.user['id'], task['id'], target['id'], 'followers')

    async def finish(self, state, job):
        for gate in state.gates.values(): gate.set()
        manager, control = state.completion_context

        async def completion():
            return await job

        # Two sources and six children serialize many real SQLite commits. A
        # healthy drain can exceed ten seconds on Windows; only durable progress
        # renews this budget, and the actual pipeline must still return. The
        # duplicate-only tail (80 spellings) cannot advance unique counters, so
        # allow thirty seconds without advancement, plus a two-minute hard cap.
        return await wait_for_collection_operation(
            completion, manager, self.service, control.owner_user_id, control.task_id,
            idle_timeout=30, overall_timeout=120,
        )

    async def cleanup(self, state, job):
        for gate in state.gates.values(): gate.set()
        if not job.done(): job.cancel()
        await asyncio.gather(job, return_exceptions=True)

    async def test_all_topologies_allow_one_waiting_beside_busy_children_and_wake_on_claim(self):
        for count in (1, 2, 3):
            with self.subTest(topology=count):
                task, target, control, state, source, job = self.pipeline(count)
                try:
                    for name in state.candidates[:count]:
                        await asyncio.wait_for(state.started[name].wait(), 10)
                    await self.until(lambda: self.stats(task, target)['total'] == count + 1, job)
                    # All children are event-held; exactly one further account is
                    # durable/global-reserved but has not been handed to a child.
                    self.assertEqual(count + 1, self.stats(task, target)['pending'])
                    self.assertEqual(count, len(state.delivered))
                    queued = state.candidates[count]
                    self.assertFalse(state.started[queued].is_set())
                    self.assertTrue(self.service.check_global_dedupe(queued)['seen'])
                    # Releasing ONE child allows it to claim the waiting row and
                    # the parent immediately adds ONE replacement while it reads.
                    state.gates[state.candidates[0]].set()
                    await asyncio.wait_for(state.started[queued].wait(), 10)
                    await self.until(lambda: self.stats(task, target)['total'] == count + 2, job)
                    self.assertFalse(state.gates[queued].is_set())
                    self.assertEqual(count + 1, self.stats(task, target)['pending'])
                    result = await self.finish(state, job)
                    self.assertEqual(0, result['pending'])
                    self.assertEqual(len(state.candidates), len(set(name for _, name in state.screened)))
                    self.assertEqual(count, len(state.children))
                    self.assertTrue(all(child.disconnect_calls == 1 for child in state.children))
                finally:
                    await self.cleanup(state, job)

    async def test_global_duplicate_never_opens_in_a_child(self):
        self.service.claim_workbench_identity(self.user['id'], username='already.seen', source='other')
        task, target, control, state, source, job = self.pipeline(1, names=['already.seen', 'new.account'])
        try:
            result = await self.finish(state, job)
            self.assertEqual([('child', 'new.account')], state.screened)
            self.assertEqual((2, 1, 1), (result['total'], result['deduped'], result['recorded']))
        finally: await self.cleanup(state, job)

    async def test_stop_keeps_busy_and_single_waiting_claims_resumable(self):
        task, target, control, state, source, job = self.pipeline(1)
        try:
            await self.until(lambda: self.stats(task, target)['total'] == 2, job)
            control.stop_event.set()
            with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(job, 10)
            self.assertEqual(2, self.stats(task, target)['pending'])
            resumed_service = CoreService(Database(self.service.database.path))
            for name in state.candidates[:2]:
                claim = resumed_service.claim_workbench_identity(self.user['id'], username=name,
                    source='followers', source_target=target['id'], allow_owned_resume=True)
                self.assertTrue(claim['resumed'])
                self.assertFalse(claim['duplicate'])
        finally: await self.cleanup(state, job)

    async def test_pause_does_not_admit_another_account_after_child_claim(self):
        task, target, control, state, source, job = self.pipeline(1)
        try:
            await self.until(lambda: self.stats(task, target)['total'] == 2, job)
            control.pause_event.clear()
            state.gates[state.candidates[0]].set()
            for _ in range(15): await asyncio.sleep(0)
            self.assertEqual(2, self.stats(task, target)['total'])
            control.pause_event.set()
            self.assertEqual(0, (await self.finish(state, job))['pending'])
        finally: control.pause_event.set(); await self.cleanup(state, job)

    async def test_pause_while_parent_reads_queue_is_rechecked_before_identity_admission(self):
        task, target, control = self._task_and_control(1)
        parent_paused, violation = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        class ObservedPause(asyncio.Event):
            async def wait(self):
                if not self.is_set() and asyncio.current_task().get_coro().__name__ == 'producer':
                    parent_paused.set()
                return await super().wait()
        pause = ObservedPause(); pause.set(); control.pause_event = pause
        original_read = self.service.list_pending_task_mode_candidates
        original_admit = self.service.discover_task_mode_candidate
        armed = threading.Event()
        def read(*args, **kwargs):
            rows = original_read(*args, **kwargs)
            if 'after_discovery_order' not in kwargs and not armed.is_set():
                armed.set()
                async def pause_before_return(): pause.clear()
                asyncio.run_coroutine_threadsafe(pause_before_return(), loop).result(5)
            return rows
        def admit(*args, **kwargs):
            if not pause.is_set(): loop.call_soon_threadsafe(violation.set)
            return original_admit(*args, **kwargs)
        with patch.object(self.service, 'list_pending_task_mode_candidates', read), \
             patch.object(self.service, 'discover_task_mode_candidate', admit):
            task, target, control, state, source, job = self.pipeline(1, fixture=(task, target, control))
            watchers = [asyncio.create_task(parent_paused.wait()), asyncio.create_task(violation.wait())]
            try:
                done, _ = await asyncio.wait(watchers, timeout=5, return_when=asyncio.FIRST_COMPLETED)
                self.assertTrue(done, 'parent neither respected pause nor reached admission')
                self.assertFalse(violation.is_set(), 'parent admitted an account after pause was accepted')
                self.assertEqual(0, self.stats(task, target)['total'])
                pause.set()
                self.assertEqual(0, (await self.finish(state, job))['pending'])
            finally:
                pause.set()
                for watcher in watchers: watcher.cancel()
                await asyncio.gather(*watchers, return_exceptions=True)
                await self.cleanup(state, job)

    async def test_child_creation_failure_never_falls_back_to_parent_or_prefetch(self):
        task, target, control, state, source, job = self.pipeline(2, create_error=True)
        try:
            with self.assertRaises(WorkerExecutionError) as caught: await asyncio.wait_for(job, 10)
            self.assertEqual('screening_child', caught.exception.details['recovery_scope'])
            self.assertEqual(0, state.source_calls)
            self.assertEqual(0, self.stats(task, target)['total'])
        finally: await self.cleanup(state, job)

    async def test_all_child_failures_preserve_one_waiting_row_and_do_not_screen_on_parent(self):
        task, target, control, state, source, job = self.pipeline(2, fail=True, pooled=True)
        try:
            await self.until(lambda: self.stats(task, target)['total'] == 3, job)
            for name in state.candidates[:2]: state.gates[name].set()
            with self.assertRaises(WorkerExecutionError): await asyncio.wait_for(job, 10)
            self.assertEqual((3, 3), (self.stats(task, target)['total'], self.stats(task, target)['pending']))
            self.assertEqual([], state.screened)
            self.assertTrue(all(c.disconnect_calls == 1 for c in state.children))
            self.assertEqual([], state.pool_releases, 'a failed child must not return to the reusable pool')
        finally: await self.cleanup(state, job)

    async def test_successful_children_return_to_the_same_pool_without_unnecessary_close(self):
        task, target, control, state, source, job = self.pipeline(2, pooled=True)
        try:
            self.assertEqual(0, (await self.finish(state, job))['pending'])
            self.assertEqual(2, len(state.pool_releases))
            self.assertEqual({id(c) for c in state.children}, {id(c) for c in state.pool_releases})
            self.assertTrue(all(c.disconnect_calls == 0 for c in state.children))
        finally: await self.cleanup(state, job)

    async def test_finished_source_resumes_pending_profiles_only_in_children(self):
        task, target, control = self._task_and_control(2)
        names = ['resume.first', 'resume.second']
        for name in names:
            self.service.discover_task_mode_candidate(self.user['id'], task['id'], target['id'], 'followers', name)
        manager = _PipelineManager(self.service, _NoopBitBrowser())
        await manager._save_candidate_progress_checkpoint(control, target['id'], 'followers', self.stats(task, target),
            discovery_complete=True, source_total=2)
        checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')
        task, target, control, state, source, job = self.pipeline(2, names=names,
            fixture=(task, target, control), checkpoint=checkpoint)
        try:
            self.assertEqual(0, (await self.finish(state, job))['pending'])
            self.assertEqual(0, state.source_calls)
            self.assertEqual(set(names), {name for _, name in state.screened})
        finally: await self.cleanup(state, job)

    async def test_discovery_rolls_back_global_claim_when_spool_write_fails(self):
        task, target, _ = self._task_and_control(1)
        with patch.object(self.service, 'append_task_mode_candidates', side_effect=RuntimeError('injected spool error')):
            with self.assertRaises(RuntimeError):
                self.service.discover_task_mode_candidate(self.user['id'], task['id'], target['id'], 'followers', 'atomic.account')
        self.assertFalse(self.service.check_global_dedupe('atomic.account')['seen'])
        self.assertEqual(0, self.stats(task, target)['total'])

    async def test_two_parent_sources_cannot_both_queue_the_same_new_identity(self):
        one, target_one, _ = self._task_and_control(1)
        two, target_two, _ = self._task_and_control(2)
        results = await asyncio.gather(*[asyncio.to_thread(self.service.discover_task_mode_candidate,
            self.user['id'], task['id'], target['id'], 'followers', 'shared.account')
            for task, target in [(one, target_one), (two, target_two)]])
        self.assertEqual([False, True], sorted(r['duplicate'] for r in results))
        self.assertEqual([0, 1], sorted(r['pending'] for r in results))
        self.assertEqual([0, 1], sorted(r['deduped'] for r in results))

    async def test_child_profiles_keep_real_public_private_and_discard_rules(self):
        task, target, control = self._task_and_control(2)
        settings = {**task['settings'], 'local_person_recognition': False,
            'gpt_enabled': False, 'location_enabled': False,
            'public_discard_followers_max': 3000, 'private_discard_followers_max': 5000}
        reads = []
        class Child:
            async def read_visible_profile(self, username, **kwargs):
                reads.append((username, kwargs.get('include_activity', False)))
                return {'username': username, 'visibility': 'private' if 'private' in username else 'public',
                    'followers': 9000 if 'large' in username else 120, 'following': 80, 'posts': 9,
                    'activity_days': 2 if kwargs.get('include_activity') else None}
            async def disconnect(self): pass
        class Parent(_RelationParent):
            supports_single_candidate_handoff = True
            async def create_parallel_screening_worker(self): return Child()
            async def collect_followers(self, _target, *, candidate_sink, hover_precheck=True, **kwargs):
                assert not hover_precheck
                await candidate_sink(['good.public', 'good.private', 'large.public', 'large.private'])
                return CollectionOutcome('followers', [], source_total=4)
            async def read_visible_profile(self, *args, **kwargs):
                raise AssertionError('candidate profile read on parent')
        manager = ExecutionManager(self.service, _NoopBitBrowser())
        stats = await asyncio.wait_for(manager._execute_candidate_spooled_mode(
            control, Parent(_PipelineState()), target, 'followers', settings, None), 10)
        self.assertCountEqual(['good.public', 'good.private', 'large.public', 'large.private'],
            [name for name, activity in reads if not activity])
        self.assertEqual([('good.public', True)], [item for item in reads if item[1]])
        self.assertEqual((4, 0, 4), (stats['total'], stats['pending'], stats['recorded']))
        with self.service.database.read() as connection:
            candidates = connection.execute('SELECT a.current_username_norm, c.visibility FROM workbench_candidates c JOIN instagram_accounts a ON a.id=c.account_id').fetchall()
            exclusions = connection.execute('SELECT a.current_username_norm FROM workbench_collection_exclusions e JOIN instagram_accounts a ON a.id=e.account_id').fetchall()
        self.assertEqual({('good.public', 'public'), ('good.private', 'private')}, {tuple(r) for r in candidates})
        self.assertEqual({'large.public', 'large.private'}, {r[0] for r in exclusions})

    async def test_child_checks_counts_location_activity_in_order_and_never_runs_gender_model(self):
        task, target, control = self._task_and_control(1)
        settings = {**task['settings'], 'location_enabled': True, 'gpt_enabled': False,
            'public_discard_followers_max': 3000, 'public_discard_active_days_max': 30,
            'local_person_recognition': True, 'exclude_male_avatar': True}
        events = []
        names = ['zero.public', 'zero.private', 'counts.fail', 'country.fail', 'activity.fail', 'kept.account']
        class Child:
            async def read_visible_profile(self, username, **kwargs):
                active = kwargs.get('include_activity', False)
                events.append((username, 'activity' if active else 'counts'))
                return {'username': username, 'visibility': 'private' if username == 'zero.private' else 'public',
                    'posts': 0 if username.startswith('zero.') else 9, 'following': 80,
                    'followers': 9000 if username == 'counts.fail' else 100,
                    'activity_days': 60 if username == 'activity.fail' else 2,
                    'post_activity_days': 60 if username == 'activity.fail' else 2,
                    'activity_status': 'identified', 'post_activity_status': 'identified'}
            async def read_visible_account_location(self, username):
                events.append((username, 'location'))
                return '加拿大' if username == 'country.fail' else '美国'
            async def capture_visible_review_snapshot(self, username, **kwargs):
                events.append((username, 'save_preview')); return {}
            async def disconnect(self): pass
        class Parent(_RelationParent):
            supports_single_candidate_handoff = True
            collection_pipeline = PlaywrightWorker.collection_pipeline
            async def create_parallel_screening_worker(self): return Child()
            async def collect_followers(self, _target, *, candidate_sink, **kwargs):
                await candidate_sink(names)
                return CollectionOutcome('followers', [], source_total=len(names))
        manager = ExecutionManager(self.service, _NoopBitBrowser(), person_classifier_factory=lambda:
            self.fail('old saved switches started the retired gender model'))
        result = await asyncio.wait_for(manager._execute_candidate_spooled_mode(
            control, Parent(_PipelineState()), target, 'followers', settings, None), 10)
        self.assertEqual((6, 0), (result['recorded'], result['pending']))
        self.assertEqual([('zero.public', 'counts'), ('zero.private', 'counts'),
            ('counts.fail', 'counts'), ('country.fail', 'counts'), ('country.fail', 'location'),
            ('activity.fail', 'counts'), ('activity.fail', 'location'), ('activity.fail', 'activity'),
            ('kept.account', 'counts'), ('kept.account', 'location'), ('kept.account', 'activity'),
            ('kept.account', 'save_preview')], events)
        rows = {r['username']: r for r in self.service.list_results(self.user['id'], task['id'])}
        self.assertEqual('primary', rows['kept.account']['screening']['review_tier'])
        self.assertEqual('public_zero_posts_excluded', rows['zero.public']['screening']['review_reason'])
        self.assertEqual('private_zero_posts_excluded', rows['zero.private']['screening']['review_reason'])
        self.assertTrue(all(not r['screening'].get('person_recognition', {}).get('checked') for r in rows.values()))


class BatchCollectionR59Tests(unittest.IsolatedAsyncioTestCase):
    """The production policy keeps existing leases/dedupe, without a queue cap."""
    asyncSetUp = SingleHandoffR94Tests.asyncSetUp
    asyncTearDown = SingleHandoffR94Tests.asyncTearDown
    _task_and_control = SingleHandoffR94Tests._task_and_control
    until = SingleHandoffR94Tests.until
    stats = SingleHandoffR94Tests.stats
    finish = SingleHandoffR94Tests.finish
    cleanup = SingleHandoffR94Tests.cleanup

    def pipeline(self, *args, **kwargs):
        return SingleHandoffR94Tests.pipeline(self, *args, **kwargs, batch=True)

    async def test_unexpected_child_error_keeps_its_account_reserved_through_slow_cleanup(self):
        await self._unexpected_child_cleanup_case()

    async def test_repeated_stop_joins_failed_child_cleanup_and_resumes_only_unfinished_account(self):
        await self._unexpected_child_cleanup_case(stop=True)

    async def test_repeated_stop_after_slow_source_checkpoint_preserves_natural_end(self):
        await self._unexpected_child_cleanup_case(stop=True, checkpoint_delay=.25)

    async def test_stop_before_source_end_resumes_saved_tail_and_discovers_remaining_accounts(self):
        await self._unexpected_child_cleanup_case(stop=True, before_source_end=True)

    async def _unexpected_child_cleanup_case(self, *, stop=False, checkpoint_delay=0,
                                             before_source_end=False):
        task, target, control = self._task_and_control(3)
        state = _PipelineState('unexpected')
        state.candidates = ['broken.account'] + [f'healthy.{i}' for i in range(8)]
        attempts = []
        close_started, close_allowed = asyncio.Event(), asyncio.Event()
        source_held, source_allowed = asyncio.Event(), asyncio.Event()
        class Child(_ScreeningChild):
            failed = False
            async def screen(self, username):
                attempts.append(username)
                if username == 'broken.account':
                    self.failed = True
                    raise RuntimeError('unexpected profile reader failure')
                state.screened.append(('child', username))
            async def disconnect(self):
                if self.failed:
                    close_started.set()
                    await close_allowed.wait()
                await super().disconnect()
        class Source(_RelationParent):
            supports_single_candidate_handoff = True
            collection_pipeline = PlaywrightWorker.collection_pipeline
            async def create_parallel_screening_worker(self):return Child(state)
            async def collect_followers(self, _target, *, candidate_sink, **kwargs):
                await candidate_sink(state.candidates)
                if before_source_end:
                    await kwargs['progress_sink']({'resume_tail': state.candidates[-3:],
                        'source_total': len(state.candidates) + 1, 'rendered_count': len(state.candidates)})
                    source_held.set()
                    await source_allowed.wait()
                return CollectionOutcome('followers', [], source_total=len(state.candidates))
        manager = _PipelineManager(self.service, _NoopBitBrowser())
        original_save = manager._save_candidate_progress_checkpoint
        async def delayed_save(*args, **kwargs):
            if (checkpoint_delay and kwargs.get('stage') == 'discovering_accounts'
                    and args[3]['total'] == len(state.candidates)):
                await asyncio.sleep(checkpoint_delay)
            return await original_save(*args, **kwargs)
        manager._save_candidate_progress_checkpoint = delayed_save
        job = asyncio.create_task(manager._execute_candidate_spooled_mode(
            control, Source(state), target, 'followers', task['settings'], None))
        try:
            await asyncio.wait_for(close_started.wait(), 10)
            # The failed page deliberately remains alive. Healthy siblings must
            # finish their own rows without taking over that still-owned account.
            await self.until(lambda: len(attempts) > len(state.candidates) or
                self.stats(task, target)['recorded'] == 8, job)
            self.assertEqual(1, attempts.count('broken.account'))
            self.assertEqual(8, self.stats(task, target)['recorded'])
            if stop:
                if before_source_end:
                    await asyncio.wait_for(source_held.wait(), 10)
                else:
                    # Recorded child rows do not prove that the source returned.
                    # On slow Windows disks the final source checkpoint may still
                    # be writing. Require the real natural-end commit before
                    # asserting that a restart must never read the source again.
                    await self.until(lambda: bool((self.service.get_checkpoint(
                        self.user['id'], task['id'], target['id'], 'followers') or {}
                        ).get('cursor', {}).get('candidate_spool_natural_end')), job)
                job.cancel()
                await asyncio.sleep(0)
                job.cancel()
                await asyncio.sleep(0)
                self.assertFalse(job.done(), 'Stop must join the still-owned native cleanup')
                close_allowed.set()
                with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(job, 10)
            else:
                close_allowed.set()
                with self.assertRaisesRegex(RuntimeError, 'unexpected profile reader failure'):
                    await asyncio.wait_for(job, 10)
            self.assertEqual(1, self.stats(task, target)['pending'])
            self.service = CoreService(Database(self.service.database.path))
            checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')
            self.assertEqual(not before_source_end,
                bool(checkpoint['cursor'].get('candidate_spool_natural_end')))
            resume_names = state.candidates + (['later.account'] if before_source_end else [])
            _, _, _, resumed, _, retry = self.pipeline(3, fixture=(task, target, control),
                names=resume_names, checkpoint=checkpoint)
            try:
                result = await self.finish(resumed, retry)
                self.assertEqual(int(before_source_end), resumed.source_calls)
                self.assertCountEqual(['broken.account'] + (['later.account'] if before_source_end else []),
                    [name for _, name in resumed.attempts])
                if before_source_end:
                    self.assertEqual([state.candidates[-3:]], resumed.source_resume_tails)
                self.assertEqual((len(resume_names), 0), (result['recorded'], result['pending']))
            finally:await self.cleanup(resumed, retry)
        finally:
            close_allowed.set()
            source_allowed.set()
            if not job.done():job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def test_known_pending_aliases_have_one_profile_owner_before_any_result_exists(self):
        task, target, control = self._task_and_control(3)
        claim = self.service.claim_workbench_identity(self.user['id'], username='old.name',
            instagram_user_id='9123456', source='followers', source_target=target['id'])
        alias = self.service.claim_workbench_identity(self.user['id'], username='new.name',
            instagram_user_id='9123456', source='followers', source_target=target['id'])
        self.assertEqual(claim['account_id'], alias['account_id'])
        task, target, control, state, source, job = self.pipeline(3,
            fixture=(task, target, control), names=['old.name', 'new.name', 'other.person'])
        try:
            await self.until(lambda: len(state.delivered) == len(state.candidates), job)
            await asyncio.wait_for(state.started['old.name'].wait(), 10)
            await asyncio.wait_for(state.started['other.person'].wait(), 10)
            self.assertEqual(1, self.stats(task, target)['deduped'])
            self.assertFalse(state.started['new.name'].is_set())
            self.assertEqual(2, len(state.attempts))
            result = await self.finish(state, job)
            self.assertEqual((2, 1, 0), (result['recorded'], result['deduped'], result['pending']))
        finally:await self.cleanup(state, job)

    async def test_slow_child_does_not_block_siblings_or_duplicate_its_account(self):
        names = [f'independent.{i}' for i in range(30)]
        task, target, control, state, source, job = self.pipeline(3, names=names)
        try:
            for name in names[1:]:state.gates[name].set()
            await asyncio.wait_for(state.started[names[0]].wait(), 10)
            await self.until(lambda: self.stats(task, target)['recorded'] == len(names) - 1, job)
            self.assertFalse(state.gates[names[0]].is_set())
            self.assertEqual(1, self.stats(task, target)['pending'])
            self.assertEqual(len(names), len(state.attempts))
            slow_owner = next(owner for owner, name in state.attempts if name == names[0])
            self.assertTrue(all(owner != slow_owner for owner, name in state.attempts if name != names[0]))
            self.assertEqual(0, (await self.finish(state, job))['pending'])
            self.assertEqual(len(names), len({name for _, name in state.attempts}))
        finally:await self.cleanup(state, job)

    async def test_failed_primary_alias_remains_resumable_after_database_reopen(self):
        task, target, control = self._task_and_control(1)
        for name in ['first.alias', 'second.alias']:
            self.service.claim_workbench_identity(self.user['id'], username=name,
                instagram_user_id='9123457', source='followers', source_target=target['id'])
        task, target, control, state, source, job = self.pipeline(1,
            fixture=(task, target, control), names=['first.alias', 'second.alias'], fail=True)
        try:
            await self.until(lambda: bool((self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers') or {}).get('cursor', {}).get('candidate_spool_natural_end')), job)
            for gate in state.gates.values():gate.set()
            with self.assertRaises(WorkerExecutionError):await asyncio.wait_for(job, 10)
            self.assertEqual(['first.alias'], [name for _, name in state.attempts])
            self.assertEqual((1, 1), (self.stats(task, target)['pending'], self.stats(task, target)['deduped']))
            self.service = CoreService(Database(self.service.database.path))
            checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')
            _, _, _, resumed, _, retry = self.pipeline(1, fixture=(task, target, control),
                names=state.candidates, checkpoint=checkpoint)
            try:
                result = await self.finish(resumed, retry)
                self.assertEqual(0, resumed.source_calls)
                self.assertEqual(['first.alias'], [name for _, name in resumed.attempts])
                self.assertEqual((1, 1, 0), (result['recorded'], result['deduped'], result['pending']))
            finally:await self.cleanup(resumed, retry)
        finally:await self.cleanup(state, job)

    async def test_two_parent_pipelines_and_six_children_share_global_dedupe(self):
        names = [f'shared.{i}' for i in range(20)]
        variants = names + ['@' + name.upper() for name in names] + [
            'https://www.instagram.com/' + name + '/?source=list' for name in names]
        runs = [self.pipeline(3, names=variants) for _ in range(2)]
        try:
            results = await asyncio.gather(*(self.finish(run[3], run[5]) for run in runs))
            attempts = [name for run in runs for _, name in run[3].attempts]
            self.assertEqual(sorted(names), sorted(attempts))
            self.assertEqual(len(names), sum(result['recorded'] for result in results))
            self.assertEqual(len(names), sum(result['deduped'] for result in results))
            self.assertTrue(all(result['pending'] == 0 for result in results))
        finally:
            for run in runs:await self.cleanup(run[3], run[5])

    async def test_slow_commits_preserve_two_parent_global_dedupe(self):
        from app.discovery_write_session import DiscoveryWriteSession

        original_write = self.service.database.write
        original_session_write = DiscoveryWriteSession.write
        delayed_commits = 0

        @contextmanager
        def slow_write():
            nonlocal delayed_commits
            with original_write() as connection:
                yield connection
                # Hold the real writer lock through the injected commit delay,
                # so all six children and both sources contend as on slow disks.
                time.sleep(.1)
                delayed_commits += 1

        @contextmanager
        def slow_session_write(session, database):
            nonlocal delayed_commits
            with original_session_write(session, database) as connection:
                yield connection
                # Reused recognition connections still commit every identity.
                # Inject the same disk delay while their real writer lock is held.
                time.sleep(.1)
                delayed_commits += 1

        with patch.object(self.service.database, 'write', slow_write), \
             patch.object(DiscoveryWriteSession, 'write', slow_session_write):
            await self.test_two_parent_pipelines_and_six_children_share_global_dedupe()
        # This workload exceeds the former ten-second total deadline even with
        # no other disk/scheduler overhead, while retaining every dedupe check.
        self.assertGreater(delayed_commits * .1, 10)

    async def test_one_broken_child_leaves_siblings_running_and_only_its_account_pending(self):
        names = [f'isolated.{i}' for i in range(12)]
        task, target, control, state, source, job = self.pipeline(3, names=names, fail={names[0]})
        try:
            await self.until(lambda: len(state.delivered) == len(names), job)
            for gate in state.gates.values():gate.set()
            with self.assertRaises(WorkerExecutionError):await asyncio.wait_for(job, 10)
            self.assertEqual(sorted(names[1:]), sorted(name for _, name in state.screened))
            self.assertEqual(len(names), len(state.attempts))
            self.assertEqual(len(names), len({name for _, name in state.attempts}))
            stats = self.stats(task, target)
            self.assertEqual((len(names) - 1, 1), (stats['recorded'], stats['pending']))
            self.assertTrue(all(child.disconnect_calls == 1 for child in state.children))
        finally:await self.cleanup(state, job)

    async def test_all_three_topologies_persist_a_batch_while_children_are_busy(self):
        self.assertEqual('r59-batch', PlaywrightWorker.collection_pipeline)
        for count in (1, 2, 3):
            with self.subTest(children=count):
                task, target, control, state, source, job = self.pipeline(count, pooled=True)
                try:
                    await self.until(lambda: len(state.delivered) == len(state.candidates), job)
                    for name in state.candidates[:count]:
                        await asyncio.wait_for(state.started[name].wait(), 10)
                    stats = self.stats(task, target)
                    self.assertEqual(len(state.candidates), stats['pending'])
                    self.assertGreater(stats['pending'] - count, 1)
                    self.assertFalse(job.done(), 'Source completion must wait for pending profile work')
                    self.assertEqual(count, len(state.children))
                    for name in state.candidates:
                        self.assertTrue(self.service.check_global_dedupe(name)['seen'])
                    result = await self.finish(state, job)
                    self.assertEqual(0, result['pending'])
                    self.assertEqual(len(state.candidates), result['recorded'])
                    self.assertEqual(len(state.candidates), len(set(name for _, name in state.screened)))
                    self.assertEqual(count, len(state.pool_releases))
                    self.assertTrue(all(child.disconnect_calls == 0 for child in state.children))
                finally:await self.cleanup(state, job)

    async def test_existing_global_duplicate_never_enters_a_profile_child(self):
        self.service.claim_workbench_identity(self.user['id'], username='already.seen', source='other')
        task, target, control, state, source, job = self.pipeline(3, names=['already.seen', 'new.account', 'new.account'])
        try:
            result = await self.finish(state, job)
            self.assertEqual([('child', 'new.account')], state.screened)
            self.assertEqual((2, 1, 1), (result['total'], result['deduped'], result['recorded']))
        finally:await self.cleanup(state, job)

    async def test_stop_and_restart_drain_saved_batch_without_reading_source_again(self):
        task, target, control, state, source, job = self.pipeline(3)
        try:
            await self.until(lambda: len(state.delivered) == len(state.candidates), job)
            await self.until(lambda: bool((self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers') or {}).get('cursor', {}).get('candidate_spool_natural_end')), job)
            control.stop_event.set()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(job, 10)
            self.assertEqual(len(state.candidates), self.stats(task, target)['pending'])
            self.assertTrue(all(child.disconnect_calls == 1 for child in state.children))
            self.service = CoreService(Database(self.service.database.path))
            checkpoint = self.service.get_checkpoint(self.user['id'], task['id'], target['id'], 'followers')
            control.stop_event.clear()
            values = self.pipeline(3, names=state.candidates, fixture=(task, target, control), checkpoint=checkpoint)
            resumed, retry = values[3], values[5]
            try:
                result = await self.finish(resumed, retry)
                self.assertEqual(0, resumed.source_calls)
                self.assertEqual(0, result['pending'])
                self.assertEqual(len(state.candidates), result['recorded'])
            finally:await self.cleanup(resumed, retry)
        finally:await self.cleanup(state, job)

    async def test_pause_between_durable_rows_prevents_admitting_the_rest_of_a_batch(self):
        task, target, control = self._task_and_control(2)
        original = self.service.discover_task_mode_candidate
        loop = asyncio.get_running_loop()
        def pause_after_first(*args, **kwargs):
            result = original(*args, **kwargs)
            if result['total'] == 1:loop.call_soon_threadsafe(control.pause_event.clear)
            return result
        with patch.object(self.service, 'discover_task_mode_candidate', side_effect=pause_after_first):
            task, target, control, state, source, job = self.pipeline(2, fixture=(task, target, control))
            try:
                await self.until(lambda: not control.pause_event.is_set(), job)
                for _ in range(20):await asyncio.sleep(0)
                self.assertEqual(1, self.stats(task, target)['total'])
                control.pause_event.set()
                self.assertEqual(0, (await self.finish(state, job))['pending'])
            finally:
                control.pause_event.set()
                await self.cleanup(state, job)

    async def test_child_creation_failure_preserves_source_instead_of_opening_it_in_parent(self):
        task, target, control, state, source, job = self.pipeline(3, create_error=True)
        try:
            with self.assertRaises(WorkerExecutionError):await asyncio.wait_for(job, 10)
            self.assertEqual(0, state.source_calls)
            self.assertEqual([], state.screened)
            self.assertEqual(0, self.stats(task, target)['total'])
        finally:await self.cleanup(state, job)

    async def test_child_failure_keeps_saved_batch_pending_for_retry(self):
        task, target, control, state, source, job = self.pipeline(1, fail=True)
        try:
            await self.until(lambda: len(state.delivered) == len(state.candidates), job)
            for gate in state.gates.values():gate.set()
            with self.assertRaises(WorkerExecutionError):await asyncio.wait_for(job, 10)
            self.assertEqual(len(state.candidates), self.stats(task, target)['pending'])
            self.assertEqual([], state.screened)
            self.assertEqual(1, state.children[0].disconnect_calls)
        finally:await self.cleanup(state, job)


class BatchSourceSurfaceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_sink_failure_preserves_live_list_and_retries_without_top_navigation(self):
        for stage in ('candidate', 'initial_progress', 'candidate_progress', 'scan_progress'):
            with self.subTest(stage=stage):
                failure = OSError('local callback unavailable')
                worker = PlaywrightWorker(None)
                page = SimpleNamespace(url='https://www.instagram.com/source.account/')
                surface = object()
                worker.page = page
                worker._guard = AsyncMock()
                worker._paused_relation_surface = (page, 'source.account', 'followers', surface, 100)
                worker._navigate_profile = AsyncMock(side_effect=AssertionError('lost the live list position'))
                async def read(current_surface, _limit, *, candidate_sink, scan_progress_sink, **kwargs):
                    self.assertIs(surface, current_surface)
                    await candidate_sink(['next.account'])
                    if scan_progress_sink is not None:
                        await scan_progress_sink({'resume_tail': ['next.account']})
                    return []
                async def progress(payload):
                    if (stage == 'initial_progress' and payload['discovered_count'] == 50 or
                        stage == 'candidate_progress' and payload['discovered_count'] == 51 or
                        stage == 'scan_progress' and 'resume_tail' in payload):
                        raise failure
                worker._read_visible_account_dialog = read
                with self.assertRaises(type(failure)):
                    await worker._collect_relation_once('source.account', relation='followers', limit=None,
                        candidate_sink=AsyncMock(side_effect=failure if stage == 'candidate' else None,
                            return_value={'total': 51}), hover_precheck=False,
                        initial_candidate_count=50, progress_sink=progress)
                self.assertIsNotNone(worker._paused_relation_surface)
                sink = AsyncMock(return_value={'total': 51})
                outcome = await worker._collect_relation_once('source.account', relation='followers', limit=None,
                    candidate_sink=sink, initial_candidate_count=50, hover_precheck=False)
                self.assertEqual(51, outcome.candidate_count)
                sink.assert_awaited_once_with(['next.account'])
                worker._navigate_profile.assert_not_awaited()

    async def test_preserved_surface_never_accepts_a_different_source_profile(self):
        worker = PlaywrightWorker(None)
        page = SimpleNamespace(url='https://www.instagram.com/wrong.source/')
        worker.page = page
        worker._guard = AsyncMock()
        worker._paused_relation_surface = (page, 'source.account', 'followers', object(), 100)
        sink = AsyncMock()
        with self.assertRaises(WorkerExecutionError) as caught:
            await worker._collect_relation_once('source.account', relation='followers', limit=None,
                candidate_sink=sink, hover_precheck=False)
        self.assertEqual('instagram_followers_list_incomplete', caught.exception.code)
        sink.assert_not_awaited()


if __name__ == '__main__': unittest.main()
