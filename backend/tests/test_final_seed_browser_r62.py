"""Real Chrome page-retirement gate; entirely offline, no account or site actions.

Required Windows gate: IGAC_REQUIRE_FINAL_SEED_BROWSER=1.
Optional evidence: IGAC_FINAL_SEED_FIXTURE_ARTIFACT_DIR.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import unittest
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError
from support.browser_fixture import browser_fixture_launch_options


class FinalSeedBrowserR62Tests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_late_cdp_pages_retire_before_final_source_and_keep_operator_page(self):
        required = os.environ.get('IGAC_REQUIRE_FINAL_SEED_BROWSER') == '1'
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            if required: self.fail('Required final-seed browser fixture needs Playwright')
            self.skipTest('Playwright unavailable; required Windows gate remains mandatory')
        runtime = await async_playwright().start()
        self.addAsyncCleanup(runtime.stop)
        candidates = [os.environ.get('IGAC_TEST_CHROMIUM_EXECUTABLE'), runtime.chromium.executable_path]
        if os.name == 'nt':
            for base in (os.environ.get('PROGRAMFILES'), os.environ.get('PROGRAMFILES(X86)'), os.environ.get('LOCALAPPDATA')):
                if base:
                    candidates.extend(str(Path(base) / suffix) for suffix in
                        ('Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe'))
        else:
            candidates.extend(shutil.which(name) for name in ('google-chrome', 'chromium', 'chromium-browser'))
        executable = next((path for path in candidates if path and Path(path).is_file()), None)
        if not executable:
            if required: self.fail('Required final-seed Chrome missing; set IGAC_TEST_CHROMIUM_EXECUTABLE')
            self.skipTest('Chrome unavailable; required Windows gate remains mandatory')
        try:
            browser = await runtime.chromium.launch(executable_path=executable, **browser_fixture_launch_options())
        except Exception as exc:
            if not required and 'socket() failed: Operation not permitted' in str(exc):
                self.skipTest('Local sandbox blocks Chrome sockets; required Windows gate remains mandatory')
            raise
        self.addAsyncCleanup(browser.close)
        context = await browser.new_context(service_workers='block')
        requests = []
        async def reject(route):
            requests.append(route.request.url)
            await route.abort()
        await context.route('**/*', reject)
        source = await context.new_page()
        await source.set_content('<h1>Offline final-source fixture</h1>')
        parent = PlaywrightWorker(None)
        parent._context, parent.page, parent.profile_id = context, source, 'offline-final-window'
        parent.cdp_command_timeout_seconds = 1.0
        cleanup_owner = None
        async def cleanup():
            nonlocal cleanup_owner
            if cleanup_owner is None:
                cleanup_owner = asyncio.create_task(parent.wait_for_cleanup())
            done, _ = await asyncio.wait({cleanup_owner}, timeout=10)
            if cleanup_owner in done:
                return cleanup_owner.result()
            # finish_owned intentionally defers cancellation. Close only this
            # isolated fixture browser to settle native calls before reporting a
            # failed retirement; wait_for alone would not bound this failure.
            emergency_close = asyncio.create_task(browser.close())
            closed, _ = await asyncio.wait({emergency_close}, timeout=10)
            if emergency_close in closed:
                emergency_close.result()
                await asyncio.wait({cleanup_owner}, timeout=5)
            self.fail('Owned child cleanup exceeded its deadline; isolated browser close requested')
        self.addAsyncCleanup(cleanup)
        old = [await parent.create_parallel_screening_worker() for _ in range(3)]
        late_operations = []
        for child in old:
            page = child.page
            # A cancellation-resistant metadata wrapper is intentionally injected;
            # the underlying browser evaluation and page-close acknowledgement are real.
            async def metadata(owned_page=page):
                operation = asyncio.create_task(owned_page.evaluate('() => new Promise(() => {})'))
                late_operations.append(operation)
                while True:
                    try:
                        return await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        if operation.cancelled():
                            raise
                        if asyncio.current_task().cancelling():
                            continue
                        raise
            with self.assertRaises(TimeoutError):
                await child._await_lifecycle_operation(metadata(), timeout=.02)
            parent.release_parallel_screening_worker(child)
        self.assertTrue(all(child._late_lifecycle_tasks for child in old))
        fresh = []
        try:
            deadline = asyncio.get_running_loop().time() + 10
            while len(fresh) < 3 and asyncio.get_running_loop().time() < deadline:
                try:
                    fresh.append(await parent.create_parallel_screening_worker())
                except WorkerExecutionError as exc:
                    self.assertEqual('screening_slots_busy', exc.code)
                self.assertLessEqual(len(context.pages), 4)
                if len(fresh) == 3: break
                await asyncio.sleep(.02)
            self.assertEqual(3, len(fresh), 'Idle late native pages never released final-source capacity')
            self.assertTrue(all(child not in old for child in fresh))
            self.assertTrue(all(child.page is None and child._worker_owned_page is None for child in old))
            self.assertFalse(source.is_closed())
            await cleanup()
            self.assertEqual([source], context.pages)
            self.assertFalse(parent._screening_worker_pool)
            self.assertFalse(parent._late_lifecycle_tasks)
            self.assertEqual([], requests)
            destination = os.environ.get('IGAC_FINAL_SEED_FIXTURE_ARTIFACT_DIR')
            if destination:
                folder = Path(destination); folder.mkdir(parents=True, exist_ok=True)
                await source.screenshot(path=str(folder / 'final-seed-browser-r62.png'))
                (folder / 'final-seed-browser-r62.json').write_text(json.dumps({
                    'verified': True, 'synthetic_offline': True, 'live_accounts_tested': False,
                    'browser_version': browser.version, 'retired_children': 3, 'replacement_children': 3,
                    'source_preserved': True, 'all_owned_children_closed': True, 'external_requests': 0,
                }, indent=2), encoding='utf-8')
        finally:
            await cleanup()
            if late_operations:
                done, pending = await asyncio.wait(set(late_operations), timeout=5)
                self.assertFalse(pending, 'Native metadata remained live after confirmed fixture cleanup')
                await asyncio.gather(*done, return_exceptions=True)


if __name__ == '__main__': unittest.main()
