"""A verified hover card excludes before a candidate profile is opened."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.database import Database
from app.execution_manager import ExecutionControl, ExecutionManager
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError
from app.profile_hover_preview import hover_count_exclusion
from app.service import CoreService


class HoverPrecheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'hover.db'); self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('hover-owner', 'hover-regression-password')['id']
        self.manager = ExecutionManager(self.service, SimpleNamespace())
        self.task = self.service.create_task(self.owner, name='hover', modes=['followers'],
            targets=['source.one'], settings={'local_person_recognition': False})
        self.target = self.task['targets'][0]['id']
        event = asyncio.Event(); event.set()
        self.control = ExecutionControl(owner_user_id=self.owner, task_id=self.task['id'],
            pause_event=event, stop_event=asyncio.Event(), leases={}, target_queue=asyncio.Queue())

    async def asyncTearDown(self):
        await asyncio.get_running_loop().shutdown_default_executor()
        self.temp.cleanup()

    async def test_above_saved_limit_is_excluded_and_reconciled_without_page_read(self):
        batch = ['too.many']
        previews = {'too.many': {'username': 'too.many', 'followers': 4001,
            'following': 20, 'posts': 1, 'visibility': 'unknown', 'evidence': 'relationship_hover_card'}}
        await self.manager._record_hover_exclusions(self.control, self.target, 'followers',
            self.task['settings'], batch, previews)
        self.service.append_task_mode_candidates(self.owner, self.task['id'],
            self.target, 'followers', batch)
        candidates = self.service.list_pending_task_mode_candidates(self.owner,
            self.task['id'], self.target, 'followers')
        self.assertEqual([], [row['username'] for row in candidates])
        result = self.service.list_results(self.owner, self.task['id'])
        by_name = {item['username']: item for item in result}
        self.assertEqual('hover_preview', by_name['too.many']['profile']['page_read_status'])
        self.assertFalse(by_name['too.many']['qualified'])

    async def test_only_verified_hover_passes_to_child_queue(self):
        batch = ['approved']
        previews = {
            'approved': {'username': 'approved', 'posts': 12, 'followers': 35,
                'following': 40, 'visibility': 'unknown', 'evidence': 'relationship_hover_card'},
        }
        await self.manager._record_hover_exclusions(self.control, self.target,
            'followers', self.task['settings'], batch, previews)
        self.service.append_task_mode_candidates(self.owner, self.task['id'],
            self.target, 'followers', batch)
        pending = self.service.list_pending_task_mode_candidates(self.owner,
            self.task['id'], self.target, 'followers')
        self.assertEqual(['approved'], [item['username'] for item in pending])
        self.assertEqual([], self.service.list_results(self.owner, self.task['id']))
        with self.assertRaises(WorkerExecutionError):
            await self.manager._record_hover_exclusions(self.control, self.target,
                'followers', self.task['settings'], ['no.card'], {})
        self.assertFalse(self.service.check_global_dedupe('no.card')['seen'])
        with self.assertRaises(WorkerExecutionError):
            await self.manager._record_hover_exclusions(self.control, self.target,
                'followers', self.task['settings'], ['abbreviated'], {
                    'abbreviated': {'username': 'abbreviated', 'posts': 5,
                        'followers': '1.2K', 'following': 12, 'evidence': 'relationship_hover_card'},
                })
        self.assertFalse(self.service.check_global_dedupe('abbreviated')['seen'])

    async def test_later_incomplete_card_does_not_partially_write_exclusions(self):
        batch = ['first.excluded', 'second.bad']
        previews = {
            'first.excluded': {'username': 'first.excluded', 'posts': 0,
                'followers': 34, 'following': 360, 'visibility': 'public',
                'evidence': 'relationship_hover_card'},
            'second.bad': {'username': 'second.bad', 'posts': 5,
                'followers': None, 'following': 34, 'visibility': 'unknown',
                'evidence': 'relationship_hover_card'},
        }
        with self.assertRaises(WorkerExecutionError):
            await self.manager._record_hover_exclusions(self.control, self.target,
                'followers', self.task['settings'], batch, previews)
        self.assertFalse(self.service.check_global_dedupe('first.excluded')['seen'])
        self.assertEqual([], self.service.list_results(self.owner, self.task['id']))

    def test_unknown_privacy_requires_both_limits_and_exact_counts(self):
        saved = {'public_discard_followers_max': 3000, 'private_discard_followers_max': 5000}
        base = {'followers': 4001, 'following': 1, 'posts': 1, 'visibility': 'unknown'}
        self.assertIsNone(hover_count_exclusion(base, saved))
        self.assertIsNotNone(hover_count_exclusion({**base, 'followers': 5001}, saved))
        self.assertIsNone(hover_count_exclusion({**base, 'followers': '5.1K'}, saved))
        self.assertIsNone(hover_count_exclusion({**base, 'posts': None}, saved))

    async def test_public_zero_post_hover_is_terminal_before_profile_open(self):
        username = 'sample_hover_empty01'
        preview = {username: {'username': username, 'posts': 0,
            'followers': 34, 'following': 360, 'visibility': 'public',
            'evidence': 'relationship_hover_card'}}
        await self.manager._record_hover_exclusions(self.control, self.target,
            'followers', self.task['settings'], [username], preview)
        self.service.append_task_mode_candidates(self.owner, self.task['id'],
            self.target, 'followers', [username])
        pending = self.service.list_pending_task_mode_candidates(self.owner,
            self.task['id'], self.target, 'followers')
        self.assertEqual([], pending)
        result = next(item for item in self.service.list_results(self.owner, self.task['id'])
            if item['username'] == username)
        self.assertEqual('public_zero_posts_excluded', result['screening']['review_reason'])
        self.assertEqual('public', result['profile']['visibility'])
        self.assertEqual('unknown_zero_posts_excluded', hover_count_exclusion(
            {**preview[username], 'visibility': 'unknown'}, self.task['settings'])[0])
        self.assertIsNone(hover_count_exclusion(
            preview[username], {**self.task['settings'], 'exclude_public_zero_posts': False}))

    async def test_rounded_hover_exclusion_records_lower_bound_not_exact_count(self):
        username = 'rounded.high'
        preview = {username: {
            'username': username, 'posts': 1200, 'followers': 63000,
            'following': 1500, 'visibility': 'unknown',
            'bounded_counts': {'followers': {'display': '6.4万', 'minimum': 63000}},
            'evidence': 'relationship_hover_card',
        }}
        await self.manager._record_hover_exclusions(self.control, self.target,
            'followers', self.task['settings'], [username], preview)
        self.service.append_task_mode_candidates(self.owner, self.task['id'],
            self.target, 'followers', [username])
        self.assertEqual([], self.service.list_pending_task_mode_candidates(
            self.owner, self.task['id'], self.target, 'followers'))
        result = next(row for row in self.service.list_results(self.owner, self.task['id'])
            if row['username'] == username)
        self.assertIsNone(result['profile']['followers'])
        self.assertEqual('bounded_hover_card_counts', result['profile']['page_read_reason'])
        self.assertEqual({'display': '6.4万', 'minimum': 63000},
            result['profile']['bounded_counts']['followers'])
        self.assertEqual({'display': '6.4万', 'minimum': 63000, 'maximum': 4000},
            result['screening']['count_ceiling']['exceeded']['followers'])
        self.assertNotIn('actual', result['screening']['count_ceiling']['exceeded']['followers'])

    async def test_mismatched_rounded_hover_evidence_cannot_record_exclusion(self):
        username = 'rounded.unconfirmed'
        preview = {username: {
            'username': username, 'posts': 9, 'followers': 5000,
            'following': 35, 'visibility': 'unknown',
            'bounded_counts': {'followers': {'display': '5K', 'minimum': 6000}},
            'evidence': 'relationship_hover_card',
        }}
        with self.assertRaises(WorkerExecutionError) as error:
            await self.manager._record_hover_exclusions(self.control, self.target,
                'followers', self.task['settings'], [username], preview)
        self.assertEqual('instagram_hover_card_unavailable', error.exception.code)
        self.assertFalse(self.service.check_global_dedupe(username)['seen'])

    async def test_crash_after_claim_rechecks_same_hover_before_any_child_page(self):
        username = 'unfinished.claim'
        preview = {username: {
            'username': username, 'posts': 3, 'followers': 5001,
            'following': 7, 'visibility': 'unknown',
            'evidence': 'relationship_hover_card',
        }}
        with patch.object(self.manager, '_record_collection_exclusion',
                          side_effect=RuntimeError('simulated exit after claim')):
            with self.assertRaisesRegex(RuntimeError, 'simulated exit'):
                await self.manager._record_hover_exclusions(self.control,
                    self.target, 'followers', self.task['settings'], [username], preview)
        self.db.initialize()  # Startup registry repair must not make the claim terminal.
        self.assertTrue(self.service.check_global_dedupe(username)['seen'])
        # A different task/owner must keep respecting this global reservation.
        self.assertFalse(self.service.should_skip_relationship_hover(self.owner,
            username, source='followers', source_target=self.target))
        self.assertTrue(self.service.should_skip_relationship_hover(self.owner,
            username, source='following', source_target=self.target))
        other = self.service.register_user('hover-second-owner', 'second-owner-test-password')['id']
        self.assertTrue(self.service.should_skip_relationship_hover(other,
            username, source='followers', source_target=self.target))
        # Recovery still sees the hover card and resumes the unfinished claim.
        await self.manager._record_hover_exclusions(self.control,
            self.target, 'followers', self.task['settings'], [username], preview)
        self.assertTrue(self.service.should_skip_relationship_hover(self.owner,
            username, source='followers', source_target=self.target))
        self.service.append_task_mode_candidates(self.owner, self.task['id'],
            self.target, 'followers', [username])
        self.assertEqual([], self.service.list_pending_task_mode_candidates(
            self.owner, self.task['id'], self.target, 'followers'))
        self.assertEqual(1, len(self.service.list_results(self.owner, self.task['id'])))

    async def test_crash_after_exclusion_repairs_missing_result_on_hover_retry(self):
        username = 'half.excluded'
        preview = {username: {
            'username': username, 'posts': 3, 'followers': 5001,
            'following': 7, 'visibility': 'unknown',
            'evidence': 'relationship_hover_card',
        }}
        with patch.object(self.service, 'record_result',
                          side_effect=RuntimeError('simulated exit before result')):
            with self.assertRaisesRegex(RuntimeError, 'simulated exit'):
                await self.manager._record_hover_exclusions(self.control,
                    self.target, 'followers', self.task['settings'], [username], preview)
        with self.db.read() as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM workbench_collection_exclusions").fetchone()[0])
        self.assertEqual([], self.service.list_results(self.owner, self.task['id']))
        self.assertFalse(self.service.should_skip_relationship_hover(self.owner,
            username, source='followers', source_target=self.target))
        # The exclusion insert is idempotent; recovery fills the missing result.
        await self.manager._record_hover_exclusions(self.control,
            self.target, 'followers', self.task['settings'], [username], preview)
        self.service.append_task_mode_candidates(self.owner, self.task['id'],
            self.target, 'followers', [username])
        self.assertEqual(1, len(self.service.list_results(self.owner, self.task['id'])))
        stats = self.service.task_mode_candidate_stats(self.owner, self.task['id'],
            self.target, 'followers')
        self.assertEqual({'total': 1, 'pending': 0, 'recorded': 1, 'deduped': 0}, stats)
        self.assertTrue(self.service.should_skip_relationship_hover(self.owner,
            username, source='followers', source_target=self.target))

    async def test_current_public_zero_hover_exclusion_still_repairs_missing_result(self):
        username = 'half.zero.hover'
        preview = {username: {
            'username': username, 'posts': 0, 'followers': 34,
            'following': 360, 'visibility': 'public',
            'evidence': 'relationship_hover_card',
        }}
        with patch.object(self.service, 'record_result',
                          side_effect=RuntimeError('simulated exit before result')):
            with self.assertRaisesRegex(RuntimeError, 'simulated exit before result'):
                await self.manager._record_hover_exclusions(self.control,
                    self.target, 'followers', self.task['settings'], [username], preview)
        self.db.initialize()
        self.assertEqual([], self.service.list_results(self.owner, self.task['id']))
        with self.db.read() as connection:
            reason = connection.execute(
                'SELECT reason_code FROM workbench_collection_exclusions'
            ).fetchone()[0]
        self.assertEqual('public_zero_posts_excluded', reason)
        self.assertFalse(self.service.should_skip_relationship_hover(self.owner,
            username, source='followers', source_target=self.target))
        resumed = self.service.claim_workbench_identity(
            self.owner, username=username, source='followers',
            source_target=self.target, allow_owned_resume=True,
        )
        self.assertTrue(resumed['resumed'])
        self.assertFalse(resumed['duplicate'])
        await self.manager._record_hover_exclusions(self.control,
            self.target, 'followers', self.task['settings'], [username], preview)
        self.assertEqual(1, len(self.service.list_results(self.owner, self.task['id'])))
        self.assertTrue(self.service.should_skip_relationship_hover(self.owner,
            username, source='followers', source_target=self.target))

    async def test_real_instagram_worker_uses_direct_profile_handoff_without_hover(self):
        worker = PlaywrightWorker(object())
        observed = []

        async def collector(target, *, limit, candidate_sink,
                            initial_candidate_count, hover_precheck=True):
            observed.append(hover_precheck)
            return 'direct-collection'

        worker.collect_followers = collector
        result = await self.manager._collect_mode(worker, 'source.one', 'followers',
            self.task['settings'], candidate_sink=lambda batch: None)
        self.assertEqual('direct-collection', result)
        self.assertEqual([False], observed)
