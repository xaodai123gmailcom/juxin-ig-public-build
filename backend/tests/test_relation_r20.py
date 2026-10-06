"""Regression coverage for relation resets, deferred repaint, and partial reads."""
import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.playwright_worker import PlaywrightWorker, WorkerExecutionError
from app.collection_surface import relation_scroll_script


class RelationR20Tests(unittest.IsolatedAsyncioTestCase):
    def worker(self):
        worker = PlaywrightWorker(None)
        worker._guard = AsyncMock()
        worker._navigate_profile = AsyncMock()
        worker._visible_relation_count = AsyncMock(return_value=17)
        return worker

    async def test_regular_collection_waits_for_repaint_before_second_scroll(self):
        worker = self.worker()
        worker._read_visible_account_hrefs = AsyncMock(side_effect=[
            ['/first/'], ['/first/'], ['/first/', '/second/'],
        ])
        surface = SimpleNamespace(evaluate=AsyncMock(return_value={
            'valid': True, 'moved': True, 'top': 100,
        }))
        saved = set()
        async def sink(batch):
            saved.update(batch)
            return len(saved)
        with patch('app.playwright_worker.asyncio.sleep', new=AsyncMock()):
            await worker._read_visible_account_dialog(
                surface, None, surface_kind='followers', candidate_sink=sink,
                candidate_total_limit=2,
            )
        self.assertEqual({'first', 'second'}, saved)
        self.assertEqual(1, surface.evaluate.await_count)

    async def test_clipped_empty_or_reordered_old_rows_do_not_acknowledge_repaint(self):
        worker = self.worker()
        first = [f'/user{i}/' for i in range(6)]
        worker._read_visible_account_hrefs = AsyncMock(side_effect=[
            first, first[4:], [], list(reversed(first[4:])), first[3:] + ['/user6/'],
        ])
        surface = SimpleNamespace(evaluate=AsyncMock(return_value={
            'valid': True, 'moved': True, 'top': 156,
        }))
        saved = set()
        async def sink(batch):
            saved.update(batch)
            return len(saved)
        with patch('app.playwright_worker.asyncio.sleep', new=AsyncMock()):
            await worker._read_visible_account_dialog(
                surface, None, surface_kind='followers', candidate_sink=sink,
                candidate_total_limit=7,
            )
        self.assertEqual({f'user{i}' for i in range(7)}, saved)
        self.assertEqual(1, surface.evaluate.await_count)

    async def test_failed_top_reset_cannot_start_from_middle(self):
        for result in ({'valid': False}, {'valid': True, 'top': 250}):
            with self.subTest(result=result):
                worker = self.worker()
                worker.collection_loading_grace_seconds = 0
                worker._open_relation_surface = AsyncMock(return_value=SimpleNamespace(
                    evaluate=AsyncMock(return_value=result),
                ))
                worker._read_visible_account_dialog = AsyncMock(return_value=['middle'])
                with patch('app.playwright_worker.asyncio.sleep', new=AsyncMock()):
                    with self.assertRaises(WorkerExecutionError) as error:
                        await worker._collect_relation_once(
                            'owner', relation='following', limit=None,
                            monitor_observation=True,
                        )
                self.assertEqual('instagram_following_list_not_rendered', error.exception.code)
                worker._read_visible_account_dialog.assert_not_awaited()

    async def test_reset_waits_for_skeleton_rows_to_reveal_scroll_container(self):
        worker = self.worker()
        surface = SimpleNamespace(evaluate=AsyncMock(side_effect=[
            {'valid': False}, {'valid': False}, {'valid': True, 'top': 0},
        ]))
        worker._open_relation_surface = AsyncMock(return_value=surface)
        worker._read_visible_account_dialog = AsyncMock(return_value=['first'])
        with patch('app.playwright_worker.asyncio.sleep', new=AsyncMock()):
            result = await worker._collect_relation_once(
                'owner', relation='following', limit=None, monitor_observation=True,
            )
        self.assertEqual(['first'], result.usernames)
        self.assertEqual(3, surface.evaluate.await_count)
        worker._read_visible_account_dialog.assert_awaited_once()

    async def test_failed_scroll_keeps_only_confirmed_current_attempt_rows(self):
        worker = self.worker()
        worker.last_relation_partial_usernames = ['old_round']
        worker._read_visible_account_hrefs = AsyncMock(side_effect=[
            ['/first/', '/first/', '/owner/', '/accounts/'],
        ])
        surface = SimpleNamespace(evaluate=AsyncMock(return_value={'valid': False}))
        with self.assertRaises(WorkerExecutionError):
            await worker._read_visible_account_dialog(
                surface, None, surface_kind='following', monitor_observation=True,
                exclude={'owner'},
            )
        self.assertEqual(['first'], worker.last_relation_partial_usernames)

    async def test_navigation_failure_clears_previous_partial_observation(self):
        worker = self.worker()
        worker.last_relation_partial_usernames = ['previous_round']
        worker.last_relation_source_total = 17
        worker._navigate_profile = AsyncMock(side_effect=WorkerExecutionError(
            'login required', reason='instagram_login_required',
        ))
        with self.assertRaises(WorkerExecutionError):
            await worker._collect_relation_once(
                'owner', relation='following', limit=None, monitor_observation=True,
            )
        self.assertEqual([], worker.last_relation_partial_usernames)
        self.assertIsNone(worker.last_relation_source_total)


class RelationDOMR20Tests(unittest.IsolatedAsyncioTestCase):
    """Real Chromium, only locally generated HTML; Windows release gate cannot skip."""
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.runtime = await async_playwright().start()
        self.addAsyncCleanup(self.runtime.stop)
        executable = os.environ.get('IGAC_TEST_CHROMIUM_EXECUTABLE', self.runtime.chromium.executable_path)
        if not Path(executable).is_file() and os.name != 'nt':
            if os.environ.get('IGAC_REQUIRE_POSTING_BROWSER') == '1':
                self.fail('Required Chromium runtime is missing')
            self.skipTest('Chromium is not installed in this non-Windows environment')
        # The Windows builder uses `playwright install chromium --no-shell`.
        self.browser = await self.runtime.chromium.launch(executable_path=executable)
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()

    async def test_real_nested_overflow_uses_account_viewport(self):
        await self.page.set_content('''
          <div id="outer" style="width:400px;height:300px;overflow-y:auto">
            <a href="/source/">source</a>
            <section id="viewport" style="height:240px;overflow-y:auto">
              <div style="overflow-y:auto">''' + ''.join(
                  f'<div style="height:40px"><a href="/user{i}/">user{i}</a></div>'
                  for i in range(17)
              ) + '''</div>
            </section><div style="height:400px"></div>
          </div>''')
        state = await self.page.locator('#outer').evaluate(relation_scroll_script('advance'))
        self.assertTrue(state['valid'])
        self.assertTrue(state['moved'])
        self.assertEqual(156, state['top'])
        self.assertEqual(0, await self.page.locator('#outer').evaluate('el => el.scrollTop'))
        self.assertEqual(156, await self.page.locator('#viewport').evaluate('el => el.scrollTop'))

    async def virtual_list(self, *, delay=120, repaint=True):
        await self.page.set_content('''
          <div role="dialog" id="surface">
            <section id="viewport" style="width:400px;height:240px;overflow-y:auto">
              <div id="rows" style="height:680px;position:relative"></div>
            </section>
          </div>
          <script>
          (() => {
            const viewport = document.querySelector('#viewport');
            const rows = document.querySelector('#rows');
            function render() {
              const start = Math.floor(viewport.scrollTop / 40);
              rows.innerHTML = Array.from({length: Math.min(6, 17-start)}, (_, i) => {
                const n = start+i;
                return `<div style="position:absolute;top:${n*40}px;height:40px;width:100%">
                  <a href="/user${n}/">user${n}</a><button>Following</button></div>`;
              }).join('');
            }
            viewport.addEventListener('scroll', () => {
              if (REPAINT) setTimeout(render, DELAY);
            });
            render();
          })();
          </script>'''.replace('REPAINT', 'true' if repaint else 'false').replace('DELAY', str(delay)))
        worker = PlaywrightWorker(None)
        worker.page = self.page
        worker.collection_poll_interval_seconds = .02
        worker.collection_loading_grace_seconds = .4
        worker.collection_settled_idle_rounds = 2
        worker._guard = AsyncMock()
        worker._relation_surface_failure = AsyncMock(return_value=None)
        worker._has_visible_relation_loading_indicator = AsyncMock(return_value=False)
        return worker

    async def test_real_virtualized_delayed_following_list_reads_all_seventeen(self):
        worker = await self.virtual_list()
        names = await worker._read_visible_account_dialog(
            self.page.locator('#surface'), None,
            monitor_observation=True, surface_kind='following',
        )
        self.assertEqual({f'user{i}' for i in range(17)}, set(names))
        self.assertGreaterEqual(worker.last_relation_scroll['movements'], 3)
        self.assertEqual(0, worker.last_relation_scroll['invalid'])

    async def test_repaint_slower_than_loading_grace_still_reads_all_seventeen(self):
        worker = await self.virtual_list(delay=600)
        names = await asyncio.wait_for(worker._read_visible_account_dialog(
            self.page.locator('#surface'), None,
            monitor_observation=True, surface_kind='following',
        ), 10)
        self.assertEqual({f'user{i}' for i in range(17)}, set(names))
        self.assertEqual(3, worker.last_relation_scroll['movements'])

    async def test_repaint_beyond_old_fixed_wait_does_not_skip_middle_accounts(self):
        for monitor in (False, True):
            with self.subTest(monitor=monitor):
                worker = await self.virtual_list(delay=2000)
                worker.collection_loading_grace_seconds = 3
                names = await asyncio.wait_for(worker._read_visible_account_dialog(
                    self.page.locator('#surface'), None,
                    monitor_observation=monitor, surface_kind='following',
                ), 15)
                self.assertEqual({f'user{i}' for i in range(17)}, set(names))
                self.assertEqual(3, worker.last_relation_scroll['movements'])

    async def test_static_following_rows_are_read_by_viewport_until_physical_end(self):
        worker = await self.virtual_list(repaint=False)
        worker.collection_loading_grace_seconds = .4
        await self.page.locator('#rows').evaluate('''rows => {
          rows.innerHTML = Array.from({length:17}, (_, n) =>
            `<div style="position:absolute;top:${n*40}px;height:40px;width:100%">
              <a href="/user${n}/">user${n}</a><button>Following</button></div>`).join('');
        }''')
        first = await worker._read_monitor_following_hrefs(self.page.locator('#surface'), exclude=set())
        self.assertEqual({f'/user{i}/' for i in range(17)}, set(first))
        self.assertEqual({f'user{i}' for i in range(6)}, worker._monitor_visible_relation_names)
        names = await asyncio.wait_for(worker._read_visible_account_dialog(
            self.page.locator('#surface'), None,
            monitor_observation=True, surface_kind='following',
        ), 6)
        self.assertEqual({f'user{i}' for i in range(17)}, set(names))
        self.assertEqual(440, await self.page.locator('#viewport').evaluate('el => el.scrollTop'))

    async def test_collection_clipping_and_delayed_repaint_commit_every_account(self):
        for kind in ('followers', 'following'):
            for delay in (120, 600):
                with self.subTest(kind=kind, delay=delay):
                    worker = await self.virtual_list(delay=delay)
                    saved = set()
                    async def sink(batch):
                        saved.update(batch)
                        return {'total': len(saved)}
                    await asyncio.wait_for(worker._read_visible_account_dialog(
                        self.page.locator('#surface'), None,
                        surface_kind=kind, candidate_sink=sink,
                    ), 10)
                    self.assertEqual({f'user{i}' for i in range(17)}, saved)
                    self.assertEqual(3, worker.last_relation_scroll['movements'])
                    self.assertEqual(0, worker.last_relation_scroll['invalid'])

    async def test_missing_repaint_preserves_partial_without_repeated_scroll_or_success(self):
        for monitor in (False, True):
            with self.subTest(monitor=monitor):
                worker = await self.virtual_list(repaint=False)
                saved = set()
                async def sink(batch):
                    saved.update(batch)
                    return len(saved)
                with self.assertRaises(WorkerExecutionError) as error:
                    await asyncio.wait_for(worker._read_visible_account_dialog(
                        self.page.locator('#surface'), None,
                        monitor_observation=monitor, surface_kind='following',
                        **({} if monitor else {'candidate_sink': sink}),
                    ), 6)
                self.assertEqual('instagram_following_list_incomplete', error.exception.code)
                observed = set(worker.last_relation_partial_usernames) if monitor else saved
                self.assertEqual({f'user{i}' for i in range(6)}, observed)
                self.assertEqual(1, worker.last_relation_scroll['movements'])
                self.assertEqual(156, await self.page.locator('#viewport').evaluate('el => el.scrollTop'))

    async def test_real_clipped_non_scrollable_list_is_invalid(self):
        await self.page.set_content('''
          <section id="surface" style="height:120px;overflow-y:hidden">
            <div style="height:500px"><a href="/first/">first</a></div>
          </section>''')
        result = await self.page.locator('#surface').evaluate(relation_scroll_script('advance'))
        self.assertFalse(result['valid'])
        self.assertFalse(result['bottom'])


if __name__ == '__main__':
    unittest.main()
