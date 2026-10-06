"""R31 browser long-run resource and optional-observer deadline regressions."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.playwright_worker import PlaywrightWorker


class _RetainingRequestContext:
    """Models APIResponse's documented retention until dispose/context close."""
    def __init__(self, *, payload=b"avatar-bytes", ok=True, content_type="image/png"):
        self.payload, self.ok, self.content_type = payload, ok, content_type
        self.retained = {}
        self.responses = []

    async def get(self, url, **kwargs):
        response = SimpleNamespace(ok=self.ok, headers={"content-type": self.content_type})
        key = len(self.responses)
        self.retained[key] = self.payload
        response.body = AsyncMock(return_value=self.payload)

        async def dispose():
            self.retained.pop(key, None)

        response.dispose = AsyncMock(side_effect=dispose)
        self.responses.append(response)
        return response


class AvatarResponseLifetimeR31Tests(unittest.IsolatedAsyncioTestCase):
    def worker(self, requests):
        worker = PlaywrightWorker(None)
        worker.page = SimpleNamespace(context=SimpleNamespace(request=requests))
        worker._decoded_image_size = Mock(return_value=(64, 64))
        return worker

    async def test_hundred_downloads_release_request_bodies_without_closing_context(self):
        requests = _RetainingRequestContext()
        worker = self.worker(requests)
        results = [await worker._download_visible_avatar("https://cdninstagram.com/avatar")
                   for _ in range(100)]
        self.assertTrue(all(result == (b"avatar-bytes", 64, 64) for result in results))
        self.assertEqual({}, requests.retained)
        self.assertTrue(all(response.dispose.await_count == 1 for response in requests.responses))

    async def test_rejected_and_invalid_images_also_dispose_the_response(self):
        for scenario in ("http_error", "not_image", "empty_body", "small_image", "invalid_image"):
            with self.subTest(scenario=scenario):
                requests = _RetainingRequestContext(
                    ok=scenario != "http_error",
                    content_type="text/html" if scenario == "not_image" else "image/png",
                    payload=b"" if scenario == "empty_body" else b"avatar-bytes",
                )
                worker = self.worker(requests)
                if scenario == "small_image":
                    worker._decoded_image_size = Mock(return_value=(10, 10))
                elif scenario == "invalid_image":
                    worker._decoded_image_size = Mock(side_effect=ValueError("invalid image"))
                self.assertIsNone(await worker._download_visible_avatar("https://cdninstagram.com/avatar"))
                self.assertEqual({}, requests.retained)
                requests.responses[0].dispose.assert_awaited_once()

    async def test_cancelled_body_read_drains_response_disposal_before_propagating(self):
        body_entered, dispose_entered, release_dispose = asyncio.Event(), asyncio.Event(), asyncio.Event()
        disposed = asyncio.Event()

        async def body():
            body_entered.set()
            await asyncio.Event().wait()

        async def dispose():
            dispose_entered.set()
            await release_dispose.wait()
            disposed.set()

        response = SimpleNamespace(ok=True, headers={"content-type": "image/png"}, body=body, dispose=dispose)
        worker = self.worker(SimpleNamespace(get=AsyncMock(return_value=response)))
        operation = asyncio.create_task(worker._download_visible_avatar("https://cdninstagram.com/avatar"))
        try:
            await asyncio.wait_for(body_entered.wait(), 1)
            operation.cancel()
            await asyncio.wait_for(dispose_entered.wait(), 0.3)
            operation.cancel()
            await asyncio.sleep(0)
            self.assertFalse(operation.done())
            release_dispose.set()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            self.assertTrue(disposed.is_set())
        finally:
            release_dispose.set()
            await asyncio.gather(operation, return_exceptions=True)

    async def test_disposal_failure_does_not_discard_a_valid_download(self):
        response = SimpleNamespace(
            ok=True, headers={"content-type": "image/png"},
            body=AsyncMock(return_value=b"avatar-bytes"),
            dispose=AsyncMock(side_effect=RuntimeError("driver disconnected during disposal")),
        )
        worker = self.worker(SimpleNamespace(get=AsyncMock(return_value=response)))
        self.assertEqual((b"avatar-bytes", 64, 64),
                         await worker._download_visible_avatar("https://cdninstagram.com/avatar"))

    async def test_stalled_disposal_does_not_hang_the_next_profile(self):
        dispose_cancelled = asyncio.Event()

        async def dispose():
            try:
                await asyncio.Event().wait()
            finally:
                dispose_cancelled.set()

        response = SimpleNamespace(
            ok=True, headers={"content-type": "image/png"},
            body=AsyncMock(return_value=b"avatar-bytes"), dispose=dispose,
        )
        worker = self.worker(SimpleNamespace(get=AsyncMock(return_value=response)))
        worker.disconnect_timeout_seconds = 0.01
        result = await asyncio.wait_for(
            worker._download_visible_avatar("https://cdninstagram.com/avatar"), 0.3,
        )
        self.assertEqual((b"avatar-bytes", 64, 64), result)
        await asyncio.wait_for(dispose_cancelled.wait(), 0.3)
        await asyncio.sleep(0)
        self.assertFalse(worker._late_lifecycle_tasks)


class _SlowLabelSession:
    def __init__(self):
        self.label_entered = asyncio.Event()
        self.detach = AsyncMock()

    async def send(self, method, params=None):
        if method == "Target.getTargetInfo":
            self.label_entered.set()
            await asyncio.Event().wait()
        return {}


class OptionalPageLabelsR31Tests(unittest.IsolatedAsyncioTestCase):
    def worker(self):
        old_page = SimpleNamespace(close=AsyncMock())
        candidate = SimpleNamespace(close=AsyncMock(), goto=AsyncMock())
        source_session, candidate_session = _SlowLabelSession(), _SlowLabelSession()
        browser = SimpleNamespace(native=SimpleNamespace(bridge=SimpleNamespace(call=Mock())))
        worker = PlaywrightWorker(browser)
        worker.profile_id = "native:profile"
        worker.page = old_page
        worker._cdp_session = source_session
        worker._context = SimpleNamespace(
            new_page=AsyncMock(return_value=candidate),
            new_cdp_session=AsyncMock(return_value=candidate_session),
        )
        worker.cdp_command_timeout_seconds = 0.01
        worker.disconnect_timeout_seconds = 0.02
        worker._validate_recovery_profile_page = AsyncMock()
        return worker, old_page, candidate, source_session, candidate_session

    async def test_stalled_labels_cannot_block_screening_child_creation(self):
        worker, old_page, candidate, source_session, child_session = self.worker()
        child = await asyncio.wait_for(worker.create_parallel_screening_worker(), timeout=0.3)
        try:
            self.assertIs(child.page, candidate)
            self.assertEqual("source", worker._task_page_role)
            self.assertEqual("screening", child._task_page_role)
            self.assertTrue(source_session.label_entered.is_set())
            self.assertTrue(child_session.label_entered.is_set())
            old_page.close.assert_not_awaited()
        finally:
            await child.disconnect()
        candidate.close.assert_awaited_once()

    async def test_stalled_label_cannot_block_valid_replacement_or_close_old_early(self):
        worker, old_page, candidate, _, _ = self.worker()
        try:
            recovered = await asyncio.wait_for(worker._recover_stalled_profile_page("target"), timeout=0.3)
            self.assertTrue(recovered)
            self.assertIs(worker.page, candidate)
            old_page.close.assert_not_awaited()
        finally:
            await worker._finish_page_recovery(progressed=False)
        self.assertIs(worker.page, old_page)
        candidate.close.assert_awaited_once()

    async def test_stop_during_label_still_cancels_factory_and_closes_child(self):
        worker, old_page, candidate, source_session, _ = self.worker()
        worker.cdp_command_timeout_seconds = 1
        operation = asyncio.create_task(worker.create_parallel_screening_worker())
        await asyncio.wait_for(source_session.label_entered.wait(), 1)
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        candidate.close.assert_awaited_once()
        old_page.close.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
