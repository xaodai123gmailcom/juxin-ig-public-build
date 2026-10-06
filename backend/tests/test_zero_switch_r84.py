"""The saved zero-post switch governs hover admission and profile fallback."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.playwright_worker import WorkerExecutionError, CollectionOutcome
from app.profile_hover_preview import hover_count_exclusion
from app.service import CoreService


class ZeroSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'zero.sqlite3')
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('zero-r84', 'zero-switch-regression-password')['id']
        self.manager = ExecutionManager(self.service, SimpleNamespace())
        self.serial = 0

    async def asyncTearDown(self):
        await asyncio.get_running_loop().shutdown_default_executor()
        self.tmp.cleanup()

    def task(self, enabled, parallel=1):
        self.serial += 1
        task = self.service.create_task(self.owner, name='zero switch', modes=['followers'],
            targets=[f'source{self.serial}'], settings={'exclude_public_zero_posts': enabled,
                'local_person_recognition': False, 'parallel_screening_workers': parallel})
        pause = asyncio.Event(); pause.set()
        control = ExecutionControl(owner_user_id=self.owner, task_id=task['id'],
            pause_event=pause, stop_event=asyncio.Event(), leases={}, target_queue=asyncio.Queue())
        return task, task['targets'][0], control

    def preview(self, username, visibility='private', posts=0):
        return dict(username=username, visibility=visibility, posts=posts, followers=34,
                    following=360, evidence='relationship_hover_card')

    def test_exact_zero_switch_matrix_does_not_depend_on_privacy_or_other_limits(self):
        for enabled in (False, True):
            for visibility in ('public', 'private', 'unknown'):
                for ceilings in (False, True):
                    with self.subTest(enabled=enabled, visibility=visibility, ceilings=ceilings):
                        result = hover_count_exclusion(self.preview('zero', visibility), {
                            'exclude_public_zero_posts': enabled, 'discard_count_limits_enabled': ceilings})
                        self.assertEqual(enabled, result is not None)
                        if enabled:
                            self.assertEqual(f'{visibility}_zero_posts_excluded', result[0])

    def test_missing_invalid_or_rounded_zero_never_proves_zero(self):
        for posts in (None, False, '', '0', -1):
            self.assertIsNone(hover_count_exclusion(self.preview('unread', posts=posts), {}))
        for visibility in ('public', 'private', 'unknown'):
            preview = self.preview('rounded', visibility)
            preview['bounded_counts'] = {'posts': {'minimum': 0, 'display': '0K'}}
            self.assertIsNone(hover_count_exclusion(preview, {}))

    async def test_hover_exclusions_never_enter_child_queue_and_off_retains_both_types(self):
        for enabled in (False, True):
            for parallel in (1, 2, 3):
                with self.subTest(enabled=enabled, parallel=parallel):
                    task, target, control = self.task(enabled, parallel)
                    previews = {f'{kind}.{self.serial}': self.preview(f'{kind}.{self.serial}', kind)
                                for kind in ('public', 'private', 'unknown')}
                    kept = f'kept.{self.serial}'
                    previews[kept] = self.preview(kept, 'private', 1)
                    await self.manager._record_hover_exclusions(control, target['id'], 'followers',
                        task['settings'], list(previews), previews)
                    self.service.append_task_mode_candidates(self.owner, task['id'], target['id'],
                        'followers', list(previews))
                    pending = self.service.list_pending_task_mode_candidates(
                        self.owner, task['id'], target['id'], 'followers')
                    expected = [kept] if enabled else list(previews)
                    self.assertEqual(set(expected), {row['username'] for row in pending})
                    reads = []
                    class Reader:
                        async def read_visible_profile(inner, name, **kwargs):
                            reads.append(name)
                            return {**previews[name], 'visibility': 'private'}
                    await self.manager._execute_candidate_spooled_mode(control, Reader(), target,
                        'followers', task['settings'], {'cursor': {'candidate_spool_complete': True,
                            'candidate_spool_natural_end': True}})
                    self.assertEqual(set(expected), set(reads))
                    stats = self.service.task_mode_candidate_stats(self.owner, task['id'], target['id'], 'followers')
                    self.assertEqual((4, 0, 4), (stats['total'], stats['pending'], stats['recorded']))
                    results = self.service.list_results(self.owner, task['id'])
                    self.assertEqual(3 if enabled else 0,
                        sum(row['screening'].get('routing_result') == 'excluded_from_hover_card' for row in results))

    async def test_profile_fallback_obeys_saved_true_and_false_for_both_privacies(self):
        for enabled in (False, True):
            for visibility in ('public', 'private'):
                with self.subTest(enabled=enabled, visibility=visibility):
                    task, target, control = self.task(enabled)
                    name = f'profile.{self.serial}'
                    preview = self.preview(name, visibility)
                    class Reader:
                        async def read_visible_profile(inner, username, **kwargs):
                            return {**preview, 'activity_status': 'no_posts'}
                    claim = self.service.claim_workbench_identity(self.owner, username=name,
                        source='followers', source_target=target['id'])
                    await self.manager._screen_and_record(control, Reader(), target['id'], name,
                        'followers', task['settings'], claim_id=claim['claim_id'])
                    result = self.service.list_results(self.owner, task['id'])[0]
                    self.assertEqual(enabled, result['screening'].get('routing_result') == 'excluded_zero_posts')
                    with self.db.read() as conn:
                        admitted = conn.execute('SELECT COUNT(*) FROM workbench_candidates c JOIN instagram_accounts a '
                            'ON a.id=c.account_id WHERE a.current_username_norm=?', (name,)).fetchone()[0]
                    self.assertEqual(0 if enabled else 1, admitted)

    async def test_live_hover_sink_only_dispatches_retained_accounts_to_selected_child_count(self):
        case = self
        for enabled in (False, True):
            for parallel in (1, 2, 3):
                with self.subTest(enabled=enabled, parallel=parallel):
                    task, target, control = self.task(enabled, parallel)
                    previews = {f'{kind}.{self.serial}': self.preview(f'{kind}.{self.serial}', kind)
                                for kind in ('public', 'private', 'unknown')}
                    kept = f'kept.{self.serial}'
                    previews[kept] = self.preview(kept, 'private', 2)
                    reads, children = [], []
                    class Child:
                        def __init__(inner): inner.closed = 0
                        async def read_visible_profile(inner, name, **kwargs):
                            reads.append(name)
                            return {**previews[name], 'visibility': 'private'}
                        async def disconnect(inner): inner.closed += 1
                    class Source:
                        supports_candidate_batch_sink = True
                        supports_collection_progress_sink = True
                        supports_parallel_screening_tab = True
                        async def collect_followers(inner, username, *, hover_precheck=False, **kwargs):
                            case.assertTrue(hover_precheck)
                            await kwargs['candidate_sink'](list(previews), previews)
                            return CollectionOutcome('followers', [], source_total=4)
                        async def create_parallel_screening_worker(inner):
                            child = Child(); children.append(child); return child
                        async def read_visible_profile(inner, *args, **kwargs):
                            case.fail('candidate dispatched to source page instead of healthy child')
                    stats = await asyncio.wait_for(self.manager._execute_candidate_spooled_mode(
                        control, Source(), target, 'followers', task['settings'], None), 10)
                    self.assertEqual((4, 0, True),
                        (stats['recorded'], stats['pending'], stats['discovery_complete']))
                    self.assertEqual(parallel, len(children))
                    self.assertEqual([1] * parallel, [child.closed for child in children])
                    self.assertCountEqual([kept] if enabled else list(previews), reads)

    async def test_unread_or_wrong_card_never_writes_zero_exclusion_even_with_switch_on(self):
        task, target, control = self.task(True)
        for name, preview in (('missing', self.preview('missing', posts=None)),
                              ('mismatch', self.preview('another'))):
            with self.assertRaises(WorkerExecutionError):
                await self.manager._record_hover_exclusions(control, target['id'], 'followers',
                    task['settings'], [name], {name: preview})
            self.assertFalse(self.service.check_global_dedupe(name)['seen'])
        self.assertEqual([], self.service.list_results(self.owner, task['id']))

    async def test_private_and_unknown_zero_recover_missing_result_once_after_restart(self):
        for visibility in ('private', 'unknown'):
            task, target, control = self.task(True)
            name = f'recover.{self.serial}'
            previews = {name: self.preview(name, visibility)}
            with patch.object(self.service, 'record_result', side_effect=RuntimeError('write interrupted')):
                with self.assertRaisesRegex(RuntimeError, 'write interrupted'):
                    await self.manager._record_hover_exclusions(control, target['id'], 'followers',
                        task['settings'], [name], previews)
            self.db.initialize()
            self.assertFalse(self.service.should_skip_relationship_hover(self.owner, name,
                source='followers', source_target=target['id']))
            await self.manager._record_hover_exclusions(control, target['id'], 'followers',
                task['settings'], [name], previews)
            self.assertEqual(1, len(self.service.list_results(self.owner, task['id'])))
            self.assertTrue(self.service.should_skip_relationship_hover(self.owner, name,
                source='followers', source_target=target['id']))


if __name__ == '__main__':
    unittest.main()
