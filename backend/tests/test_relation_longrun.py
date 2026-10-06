from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.playwright_worker import PlaywrightWorker, WorkerExecutionError


class _NoopBitBrowser:
    pass


class _VisibilityLocator:
    def __init__(self, visible: bool) -> None:
        self.visible = visible

    @property
    def first(self) -> "_VisibilityLocator":
        return self

    def nth(self, _index: int) -> "_VisibilityLocator":
        return self

    async def count(self) -> int:
        return 1 if self.visible else 0

    async def is_visible(self) -> bool:
        return self.visible


class _BatchLinks:
    def __init__(self, dialog: "_VirtualRelationDialog") -> None:
        self.dialog = dialog

    async def evaluate_all(self, _script: str) -> list[str]:
        self.dialog.evaluate_all_calls += 1
        return [f"/{username}/" for username in self.dialog.current]

    async def count(self) -> int:
        self.dialog.per_link_calls += 1
        raise AssertionError("production relation scan regressed to per-link CDP reads")


class _VirtualRelationDialog:
    def __init__(
        self,
        snapshots: list[list[str]],
        *,
        relation_spinner: bool = False,
    ) -> None:
        self.snapshots = snapshots
        self.index = 0
        self.relation_spinner = relation_spinner
        self.evaluate_all_calls = 0
        self.per_link_calls = 0
        self.end_confirmations = 0

    @property
    def current(self) -> list[str]:
        return self.snapshots[min(self.index, len(self.snapshots) - 1)]

    def locator(self, selector: str) -> Any:
        if "a[href" in selector:
            return _BatchLinks(self)
        return _VisibilityLocator(self.relation_spinner)

    async def evaluate(self, script: str) -> dict[str, Any] | None:
        if "relation-action: measure" in script:
            self.end_confirmations += 1
            return {"bottom": self.index == len(self.snapshots) - 1,
                    "height": len(self.current)}
        if "relation-action: advance" in script:
            self.index = min(self.index + 1, len(self.snapshots) - 1)
        elif "relation-action: reset" in script:
            self.index = 0
        # Row projections are read-only. Advancing on the r37 recommendation
        # snapshot silently skipped every other frame in this older fixture.
        return None

    async def inner_text(self, timeout: int = 0) -> str:
        del timeout
        return "Followers"


class _BodyLocator:
    async def inner_text(self, timeout: int = 0) -> str:
        del timeout
        return "Followers"


class _GlobalSpinnerPage:
    url = "https://www.instagram.com/source/followers/"

    def locator(self, selector: str) -> Any:
        if selector == "body":
            return _BodyLocator()
        return _VisibilityLocator(True)


class _RelationWorker(PlaywrightWorker):
    async def _guard(self) -> None:
        return None


class _GlobalSpinnerMustNotWinWorker(_RelationWorker):
    async def _has_visible_loading_indicator(self) -> bool:
        raise AssertionError("relationship completion consulted a page-wide spinner")


class _AcceleratedDayWorker(_RelationWorker):
    def __init__(self) -> None:
        super().__init__(_NoopBitBrowser())
        self.logical_seconds = 0.0

    def _collection_monotonic(self) -> float:
        self.logical_seconds += 10.0
        return self.logical_seconds


class _CollectRelationWorker(_RelationWorker):
    def __init__(self, dialog: _VirtualRelationDialog) -> None:
        super().__init__(_NoopBitBrowser())
        self.dialog = dialog

    async def _navigate_profile(self, username: str) -> str:
        return username

    async def _visible_relation_count(
        self, username_norm: str, relation: str
    ) -> int:
        del username_norm, relation
        return 6

    async def _open_relation_surface(
        self, username_norm: str, relation: str
    ) -> _VirtualRelationDialog:
        del username_norm, relation
        return self.dialog


class RelationLongRunTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_large_scan_extracts_each_rendered_batch_in_one_cdp_call(self) -> None:
        dialog = _VirtualRelationDialog(
            [[f"account_{index:04d}" for index in range(250)]]
        )
        worker = _RelationWorker(_NoopBitBrowser())
        stored: set[str] = set()

        async def sink(batch: list[str]) -> int:
            stored.update(batch)
            return len(stored)

        await worker._read_visible_account_dialog(
            dialog,
            250,
            candidate_sink=sink,
            candidate_total_limit=250,
        )

        self.assertEqual(250, len(stored))
        self.assertEqual(1, dialog.evaluate_all_calls)
        self.assertEqual(0, dialog.per_link_calls)

    async def test_resume_tail_replays_visible_rows_to_durable_dedupe_and_reports_tail(self) -> None:
        dialog = _VirtualRelationDialog([["a", "b", "c", "d", "e", "f"]])
        worker = _RelationWorker(_NoopBitBrowser())
        submitted: list[str] = []
        saved = set('abcde')
        progress: list[dict[str, Any]] = []

        async def sink(batch: list[str]) -> int:
            submitted.extend(batch)
            saved.update(batch)
            return len(saved)

        async def progress_sink(update: dict[str, Any]) -> None:
            progress.append(dict(update))

        await worker._read_visible_account_dialog(
            dialog,
            6,
            candidate_sink=sink,
            initial_candidate_count=5,
            candidate_total_limit=6,
            initial_resume_tail=("c", "d", "e"),
            scan_progress_sink=progress_sink,
        )

        self.assertEqual(list('abcdef'), submitted)
        self.assertEqual(set('abcdef'), saved)
        self.assertEqual(["b", "c", "d", "e", "f"], progress[-1]["resume_tail"])
        self.assertEqual(6, progress[-1]["rendered_count"])

    async def test_public_collection_api_passes_resume_tail_both_directions(self) -> None:
        dialog = _VirtualRelationDialog([["a", "b", "c", "d", "e", "f"]])
        worker = _CollectRelationWorker(dialog)
        submitted: list[str] = []
        progress: list[dict[str, Any]] = []

        async def sink(batch: list[str]) -> int:
            submitted.extend(batch)
            return 6

        async def progress_sink(update: dict[str, Any]) -> None:
            progress.append(dict(update))

        outcome = await worker.collect_followers(
            "source",
            limit=6,
            candidate_sink=sink,
            initial_candidate_count=5,
            progress_sink=progress_sink,
            initial_resume_tail=("c", "d", "e"),
        )

        self.assertEqual(list('abcdef'), submitted)
        self.assertEqual(6, outcome.candidate_count)
        self.assertEqual(["c", "d", "e"], progress[0]["resume_tail"])
        self.assertEqual(["b", "c", "d", "e", "f"], progress[-1]["resume_tail"])

    async def test_unrelated_page_spinner_does_not_block_relation_completion(self) -> None:
        dialog = _VirtualRelationDialog([["settled_user"]])
        worker = _GlobalSpinnerMustNotWinWorker(_NoopBitBrowser())
        worker.page = _GlobalSpinnerPage()
        worker.collection_poll_interval_seconds = 0
        worker.collection_settled_idle_rounds = 1

        result = await worker._read_visible_account_dialog(
            dialog,
            None,
            expected_minimum=1,
        )

        self.assertEqual(["settled_user"], result)
        self.assertEqual(3, dialog.end_confirmations)

    async def test_accelerated_24_hour_large_list_keeps_progress_grace_alive(self) -> None:
        # One new virtualized row every ten logical seconds, with a one-round loading
        # pause every fifty rows.  This represents more than 24 hours of continuing
        # progress without making the test wait in real time.
        target_count = 8_640
        snapshots: list[list[str]] = []
        for index in range(target_count):
            row = [f"account_{index:05d}"]
            snapshots.append(row)
            if index and index % 50 == 0:
                snapshots.append(list(row))
        dialog = _VirtualRelationDialog(snapshots, relation_spinner=True)
        worker = _AcceleratedDayWorker()
        worker.collection_poll_interval_seconds = 0
        worker.collection_settled_idle_rounds = 1
        worker.collection_loading_grace_seconds = 25.0
        stored: set[str] = set()
        last_tail: list[str] = []
        progress_count = 0

        async def sink(batch: list[str]) -> int:
            stored.update(batch)
            return len(stored)

        async def progress_sink(update: dict[str, Any]) -> None:
            nonlocal last_tail, progress_count
            progress_count += 1
            last_tail = list(update["resume_tail"])

        await worker._read_visible_account_dialog(
            dialog,
            target_count,
            surface_kind="followers",
            candidate_sink=sink,
            candidate_total_limit=target_count,
            scan_progress_sink=progress_sink,
        )

        self.assertEqual(target_count, len(stored))
        self.assertEqual(target_count, progress_count)
        self.assertEqual([f"account_{target_count - 1:05d}"], last_tail)
        self.assertGreaterEqual(worker.logical_seconds, 24 * 60 * 60)

    async def test_hung_batch_dom_read_is_bounded_and_checkpoint_safe(self) -> None:
        class HangingLinks:
            async def evaluate_all(self, _script: str) -> list[str]:
                await asyncio.Future()
                return []

        class HangingDialog(_VirtualRelationDialog):
            def locator(self, selector: str) -> Any:
                if "a[href" in selector:
                    return HangingLinks()
                return _VisibilityLocator(False)

        worker = _RelationWorker(_NoopBitBrowser())
        worker.collection_dom_operation_timeout_seconds = 0.01
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._read_visible_account_dialog(
                HangingDialog([[]]),
                None,
                surface_kind="followers",
                candidate_sink=lambda _batch: asyncio.sleep(0, result=0),
            )

        self.assertEqual("instagram_followers_list_incomplete", raised.exception.code)
        self.assertTrue(raised.exception.details["pause_required"])

    async def test_hung_scoped_spinner_probe_expires_as_incomplete_not_network(self) -> None:
        class HangingSpinner:
            async def count(self) -> int:
                await asyncio.Future()
                return 0

            def nth(self, _index: int) -> "HangingSpinner":
                return self

        class HangingSpinnerDialog(_VirtualRelationDialog):
            def locator(self, selector: str) -> Any:
                if "a[href" in selector:
                    return _BatchLinks(self)
                return HangingSpinner()

        dialog = HangingSpinnerDialog([["saved_before_spinner_hang"]])
        worker = _RelationWorker(_NoopBitBrowser())
        worker.page = _GlobalSpinnerPage()
        worker.collection_dom_operation_timeout_seconds = 0.01
        worker.collection_loading_grace_seconds = 0.001
        worker.collection_settled_idle_rounds = 1
        worker.collection_poll_interval_seconds = 0
        stored: set[str] = set()

        async def sink(batch: list[str]) -> int:
            stored.update(batch)
            return len(stored)

        with self.assertRaises(WorkerExecutionError) as raised:
            await asyncio.wait_for(
                worker._read_visible_account_dialog(
                    dialog,
                    None,
                    surface_kind="followers",
                    candidate_sink=sink,
                ),
                timeout=0.5,
            )

        self.assertEqual({"saved_before_spinner_hang"}, stored)
        self.assertEqual("instagram_followers_list_incomplete", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
