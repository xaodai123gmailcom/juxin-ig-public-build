"""Recovery retains bounded evidence without logging profile content."""
import unittest
import asyncio
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError


class RelationDiagnosticsR51Tests(unittest.TestCase):
    def test_replacement_failure_retains_diagnostic_counts_across_wrappers(self):
        original = WorkerExecutionError('list missing', reason='instagram_followers_list_not_rendered')
        original.details['surface_diagnostics'] = {
            'stage': 'surface_probe', 'dialog_count': 3, 'visible_dialog_count': 1,
            'source_matches': True, 'selected_kind': None,
            'username': 'must_not_be_logged', 'html': '<private-content>',
        }
        original.details['row_diagnostics'] = {'candidate_accounts': 0, 'profile_links': 8, 'context_links': 4}
        original.details['scroll_diagnostics'] = {'stage': 'reset', 'valid': False, 'scroll_candidates': 0}
        wrapped = PlaywrightWorker._page_recovery_exhausted(original)
        repeated = PlaywrightWorker._page_recovery_exhausted(wrapped)
        self.assertEqual('instagram_followers_list_not_rendered', repeated.details['original_reason'])
        self.assertEqual(3, repeated.details['surface_diagnostics']['dialog_count'])
        self.assertEqual(4, repeated.details['row_diagnostics']['context_links'])
        self.assertFalse(repeated.details['scroll_diagnostics']['valid'])
        self.assertNotIn('username', repeated.details['surface_diagnostics'])
        self.assertNotIn('private-content', repr(repeated.details))

    def test_untrusted_diagnostics_cannot_smuggle_strings_or_unbounded_numbers(self):
        self.assertEqual({}, PlaywrightWorker._safe_relation_diagnostics({
            'dialog_count': 'account@example', 'polls': True, 'profile_links': -1,
            'candidate_accounts': 10**20, 'stage': 'username', 'selected_kind': ['dialog'],
            'source_matches': 1,
        }))

    def test_manual_resume_forgets_observations_but_keeps_unsettled_operation_signal(self):
        worker = PlaywrightWorker(None)
        worker._validated_recovery_profile = (object(), 'cached_user')
        worker._profile_privacy_cache['cached_user'] = False
        worker._profile_posts_count_cache['cached_user'] = 5
        worker._profile_post_datetime_cache['cached_user'] = ('2026-01-01',)
        worker._profile_base_cache['cached_user'] = object()
        worker._page_stage_abandoned = True
        page = worker.page = object()
        worker.invalidate_after_manual_control()
        self.assertIsNone(worker._validated_recovery_profile)
        self.assertFalse(worker._profile_privacy_cache)
        self.assertFalse(worker._profile_posts_count_cache)
        self.assertFalse(worker._profile_post_datetime_cache)
        self.assertFalse(worker._profile_base_cache)
        self.assertTrue(worker._page_stage_abandoned)
        self.assertIs(page, worker.page)


class RelationDiagnosticPersistenceR51Tests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_task_persists_reader_evidence_with_its_checkpoint(self):
        import test_recovery_runtime_r25 as fixture
        await fixture.RecoveryRuntimeR25Tests.asyncSetUp(self)
        try:
            class Worker(fixture.Worker):
                async def collect_followers(self, *args, **kwargs):
                    error = WorkerExecutionError('not rendered', reason='instagram_followers_list_not_rendered')
                    error.details.update(
                        row_diagnostics={'profile_links': 9, 'candidate_accounts': 0, 'html': 'secret'},
                        surface_diagnostics={'stage': 'surface_probe', 'visible_dialog_count': 2},
                    )
                    raise error
            manager, task = fixture.RecoveryRuntimeR25Tests.fixture(self, worker_type=Worker, cooldown=300)
            await manager.start(self.owner, task['id'])
            control = manager._runs[task['id']]
            async with asyncio.timeout(5):
                while not control.network_waiters:
                    await asyncio.sleep(.005)
            checkpoint = self.service.get_checkpoint(self.owner, task['id'], task['targets'][0]['id'], 'followers')
            self.assertEqual({'profile_links': 9, 'candidate_accounts': 0}, checkpoint['counters']['row_diagnostics'])
            self.assertEqual(2, control.network_waiters['window']['surface_diagnostics']['visible_dialog_count'])
            self.assertNotIn('secret', repr(checkpoint))
        finally:
            await fixture.RecoveryRuntimeR25Tests.asyncTearDown(self)
