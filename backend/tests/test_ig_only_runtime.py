"""Instagram-only runtime rejection and offline collection regression proof."""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.collection_completion_selftest import run_selftest
from app.collection_coverage import describe_coverage
from app.errors import ValidationError
from app.execution_manager import ExecutionManager, _visible_instagram_user_id
from app.playwright_worker import CollectionOutcome
from support.browser_fixture import browser_fixture_launch_options


class InstagramOnlyPackagingTests(unittest.TestCase):
    def test_facebook_runtime_modules_and_factory_are_absent(self):
        app = Path(__file__).resolve().parents[1] / 'app'
        for module in ('facebook_worker', 'facebook_dom'):
            with self.subTest(module=module):
                self.assertFalse((app / (module + '.py')).exists())
                self.assertIsNone(importlib.util.find_spec('app.' + module))
        self.assertNotIn('facebook_worker_factory', inspect.signature(ExecutionManager).parameters)
        for name in ('execution_manager.py', 'playwright_worker.py', 'collection_completion_selftest.py', 'collection_coverage.py'):
            self.assertNotIn('facebook', (app / name).read_text(encoding='utf-8').lower(), name)

    def test_visible_identity_never_reinterprets_foreign_platform(self):
        self.assertIsNone(_visible_instagram_user_id({'platform': 'facebook', 'facebook_user_id': '123', 'instagram_user_id': '456'}))
        self.assertIsNone(_visible_instagram_user_id({'platform': 'unknown', 'instagram_user_id': '456'}))
        self.assertEqual('456', _visible_instagram_user_id({'instagram_user_id': '456'}))
        self.assertEqual('456', _visible_instagram_user_id({'platform': 'instagram', 'instagram_user_id': '456'}))

    def test_coverage_retains_ig_evidence_without_legacy_relation_projection(self):
        actual = describe_coverage({'source_total': 3, 'discovered': 1, 'processed': 1,
                                    'facebook_selected_relation': 'friends'}, finished=True)
        self.assertNotIn('facebook_selected_relation', actual)
        self.assertEqual(('gap', 2, 0), (actual['status'], actual['unobserved_count'], actual['pending_count']))

    def test_windows_launch_policy_stays_headed_with_product_background_flags(self):
        options = browser_fixture_launch_options(windows=True)
        self.assertFalse(options['headless'])
        expected = {'--disable-background-mode', '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows', '--disable-renderer-backgrounding'}
        self.assertEqual(expected, set(options['args']))
        native = (Path(__file__).resolve().parents[1] / 'app/native_browser.py').read_text(encoding='utf-8')
        for flag in expected:
            self.assertIn(repr(flag), native)
        self.assertIn('headless=False', native)
        options = browser_fixture_launch_options(windows=False)
        self.assertTrue(options['headless'])
        self.assertFalse(any('sandbox' in flag for flag in options['args']))


class InstagramOnlyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = Mock()
        self.provider = Mock()
        self.factory = Mock()
        self.manager = ExecutionManager(self.service, self.provider, worker_factory=self.factory)
        self.addAsyncCleanup(self.manager.shutdown)

    async def test_start_rejects_foreign_saved_spec_before_lease_or_worker(self):
        for platform in ('facebook', 'unknown', None):
            self.service.get_task_execution_spec.return_value = {'settings': {'platform': platform}}
            with self.subTest(platform=platform), self.assertRaises(ValidationError):
                await self.manager.start('owner', 'task')
        self.service.acquire_browser_lease_async.assert_not_called()
        self.service.set_task_runtime_status.assert_not_called()
        self.factory.assert_not_called()
        self.provider.assert_not_called()
        self.assertEqual({}, self.manager._runs)

    async def test_direct_dispatch_boundaries_reject_before_browser_or_queue_work(self):
        worker = SimpleNamespace(collect_followers=AsyncMock(), collect_people=AsyncMock(),
                                 read_visible_profile=AsyncMock())
        calls = (
            lambda: self.manager._window_loop(object(), 'window', [], asyncio.Queue(), ['followers'], {'platform': 'facebook'}),
            lambda: ExecutionManager._collect_mode(worker, 'fb:source', 'followers', {'platform': 'facebook'}),
            lambda: self.manager._execute_candidate_spooled_mode_body(object(), worker, {}, 'followers', {'platform': 'facebook'}, None),
            lambda: self.manager._screen_and_record(object(), worker, 'target', 'fb:person', 'followers', {'platform': 'facebook'}),
        )
        for invoke in calls:
            with self.assertRaises(ValidationError):
                await invoke()
        worker.collect_followers.assert_not_awaited()
        worker.collect_people.assert_not_awaited()
        worker.read_visible_profile.assert_not_awaited()
        self.factory.assert_not_called()
        self.assertEqual([], self.service.mock_calls)

    async def test_foreign_profile_rejected_before_executor_capture_or_any_result_write(self):
        pause = asyncio.Event()
        pause.set()
        control = SimpleNamespace(pause_event=pause, stop_event=asyncio.Event(), owner_user_id='owner')
        self.manager._manual_worker_checkpoint = AsyncMock()
        worker = SimpleNamespace(profile_id='window', read_visible_profile=AsyncMock(return_value={
            'platform': 'facebook', 'username': 'fb:person', 'instagram_user_id': '123',
            'visibility': 'public', 'posts': 4, 'followers': 30, 'following': 5,
        }))
        with patch('app.work_reports.capture_executor', new=AsyncMock()) as capture:
            with self.assertRaises(ValidationError):
                await self.manager._screen_and_record(control, worker, 'target', 'person', 'followers', {})
            capture.assert_not_awaited()
        self.assertEqual([], self.service.mock_calls)

    async def test_legacy_ig_defaults_and_explicit_ig_share_same_dispatch(self):
        outcome = CollectionOutcome('followers', ['one'])
        worker = SimpleNamespace(collect_followers=AsyncMock(return_value=outcome), collect_people=AsyncMock())
        for settings in ({}, {'platform': 'instagram'}):
            self.assertIs(outcome, await ExecutionManager._collect_mode(worker, 'source', 'followers', settings))
        self.assertEqual(2, worker.collect_followers.await_count)
        worker.collect_people.assert_not_awaited()
        worker.collect_followers.assert_awaited_with('source', limit=None)

    async def test_installed_selftest_covers_only_ig_with_network_disabled(self):
        proof = await run_selftest()
        self.assertTrue(proof['verified'])
        self.assertTrue(proof['network_disabled'])
        self.assertFalse(proof['user_data_touched'])
        self.assertFalse(proof['live_accounts_tested'])
        self.assertEqual({'normal_with_truthful_gap', 'normal_without_gap', 'abnormal_source_retained',
                          'extra_pass_failure_retained', 'pause_restart_extra_pass',
                          'parent_reels_after_final_pass', 'historical_completed_gap',
                          'manual_pending_before_producer_return', 'manual_pending_parent_join',
                          'manual_pending_stop_reopen_retry', 'final_seed_single_source',
                          'final_seed_two_sources', 'final_seed_multiple_sources',
                          'final_seed_factory_worker_not_connected',
                          'final_seed_factory_browser_context_missing'}, set(proof['cases']))
        for name in ('normal_with_truthful_gap', 'normal_without_gap'):
            case = proof['cases'][name]
            for key in ('duplicates_never_requeued', 'cleanup_before_dismissal', 'retained_data',
                        'database_reopen_persistence', 'bounded_whole_list_passes', 'same_lease', 'source_seed_dedupe',
                        'cross_task_recognition_dedupe', 'completion_history_retained'):
                self.assertTrue(case[key], (name, key))
            self.assertEqual(2 if name == 'normal_with_truthful_gap' else 1, case['source_calls'])
        self.assertEqual(2, proof['cases']['normal_with_truthful_gap']['remaining_gap'])

        self.assertTrue(proof['single_gap_recheck']['verified'])
        self.assertEqual((1, 2), (proof['single_gap_recheck']['no_gap_passes'],
                                 proof['single_gap_recheck']['gap_passes']))
        restart = proof['cases']['pause_restart_extra_pass']
        self.assertEqual((3, 2, 2), (restart['source_invocations'], restart['from_top_passes'], restart['completed_passes']))
        self.assertTrue(restart['extra_pass_resumed_from_saved_tail'])
        self.assertTrue(proof['cases']['parent_reels_after_final_pass']['children_finish_and_cleanup'])
        self.assertTrue(proof['cases']['historical_completed_gap']['no_historical_rescheduling'])
        self.assertTrue(proof['manual_parent_recheck']['verified'])
        self.assertTrue(proof['manual_parent_recheck']['children_continue_while_pending'])
        self.assertTrue(proof['cases']['manual_pending_parent_join']['parent_cancel_join_before_navigation'])
        self.assertTrue(proof['cases']['manual_pending_stop_reopen_retry']['explicit_retry_same_generation'])
        final_seed = proof['final_seed_completion']
        for key in ('verified', 'production_playwright_pool', 'late_idle_children_retired',
                    'empty_queue_after_last_source', 'authoritative_factory_reconnect',
                    'exact_lease_until_confirmed_close', 'results_dedupe_history_preserved', 'synthetic'):
            self.assertIs(final_seed[key], True, key)
        self.assertIs(final_seed['live_accounts_tested'], False)
