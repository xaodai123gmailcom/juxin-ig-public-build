"""R29 read amplification and exact-profile recovery regressions.

These exercise production readers with controlled DOM adapters; no live browser
or Instagram service is involved.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.playwright_worker import (
    EmbeddedProfileEvidence, PlaywrightWorker, WorkerExecutionError,
)
from app.collection_surface import RELATION_ROWS_SCRIPT


class _EmptyLocator:
    async def count(self):
        return 0


class _RelationFrames:
    def __init__(self, frames):
        self.frames = frames
        self.index = 0
        self.reads = 0

    def locator(self, selector):
        return self if "a[href" in selector else _EmptyLocator()

    async def evaluate_all(self, expression):
        self.reads += 1
        return [f"/{name}/" for name in self.frames[self.index]]

    async def evaluate(self, expression):
        if expression == RELATION_ROWS_SCRIPT:
            self.reads += 1
            return {"hrefs": [f"/{name}/" for name in self.frames[self.index]],
                    "recommendations_reached": False}
        if "relation-action: measure" in expression:
            return {"valid": True, "bottom": True, "height": 400, "client": 400, "top": 0}
        self.index = min(self.index + 1, len(self.frames) - 1)
        return None


class RelationReadR29Tests(unittest.IsolatedAsyncioTestCase):
    def worker(self):
        worker = PlaywrightWorker(None)
        worker._guard = AsyncMock()
        worker._relation_surface_failure = AsyncMock(return_value=None)
        worker.collection_poll_interval_seconds = 0
        worker.collection_settled_idle_rounds = 1
        worker.collection_loading_grace_seconds = 0
        return worker

    async def scan(self, frames, *, stored=None, initial_resume_tail=()):
        worker, surface = self.worker(), _RelationFrames(frames)
        saved = set(stored or ())
        submitted = []
        surface.sink_calls = 0
        initial_count = len(saved)
        expected = len(saved | {name for frame in frames for name in frame})

        async def sink(batch):
            surface.sink_calls += 1
            submitted.extend(batch)
            saved.update(batch)
            return {"total": len(saved)}

        await worker._read_visible_account_dialog(
            surface, None, candidate_sink=sink,
            initial_candidate_count=initial_count,
            initial_resume_tail=initial_resume_tail,
            expected_minimum=expected,
        )
        return saved, submitted, surface

    async def test_growing_thousand_row_dom_submits_each_row_once(self):
        frames = [[f"row{index}" for index in range(size)] for size in range(10, 1001, 10)]
        saved, submitted, surface = await self.scan(frames)
        self.assertEqual(1000, len(saved))
        self.assertEqual(1000, len(submitted))
        self.assertEqual(100, surface.sink_calls)
        # One snapshot after each durable commit protects rows repainted during
        # the write; two final reads confirm the tail. Reads remain linear and
        # every one of the 1000 accounts is submitted only once.
        self.assertEqual(2 * len(frames) + 2, surface.reads)

    async def test_unchanged_frame_does_not_repeat_persisted_rows(self):
        saved, submitted, surface = await self.scan([["alpha", "bravo"]])
        self.assertEqual({"alpha", "bravo"}, saved)
        self.assertEqual(["alpha", "bravo"], submitted)
        # Initial frame, post-commit snapshot, idle frame, final confirmation.
        self.assertEqual(4, surface.reads)

    async def test_insert_before_saved_tail_and_reorder_do_not_lose_new_account(self):
        saved, submitted, _ = await self.scan([
            ["alpha", "bravo", "charlie"],
            ["inserted", "charlie", "alpha", "bravo"],
        ])
        self.assertEqual({"alpha", "bravo", "charlie", "inserted"}, saved)
        self.assertEqual(["alpha", "bravo", "charlie", "inserted"], submitted)

    async def test_recovery_first_frame_replays_historical_names_and_new_prefix(self):
        saved, submitted, _ = await self.scan(
            [["inserted", "alpha", "bravo", "charlie"]],
            stored={"alpha", "bravo", "charlie"},
            initial_resume_tail=("alpha", "bravo", "charlie"),
        )
        self.assertEqual(4, len(saved))
        self.assertEqual(["inserted", "alpha", "bravo", "charlie"], submitted)

    async def test_virtualized_row_reappearing_after_other_frame_is_rechecked(self):
        saved, submitted, _ = await self.scan([
            ["alpha", "bravo"], ["bravo", "charlie"], ["alpha", "charlie", "delta"],
        ])
        self.assertEqual({"alpha", "bravo", "charlie", "delta"}, saved)
        self.assertEqual(["alpha", "bravo", "charlie", "alpha", "delta"], submitted)

    async def test_failed_partial_write_does_not_suppress_retry_frame(self):
        worker = self.worker()
        names = [f"row{index}" for index in range(250)]
        saved, first_batches, retry_submitted = set(), [], []

        async def fails_on_second_batch(batch):
            first_batches.append(list(batch))
            if len(first_batches) == 2:
                raise OSError("simulated persistence failure")
            saved.update(batch)
            return len(saved)

        with self.assertRaisesRegex(OSError, "persistence failure"):
            await worker._read_visible_account_dialog(
                _RelationFrames([names]), None, candidate_sink=fails_on_second_batch,
                expected_minimum=250,
            )
        self.assertEqual(100, len(saved))

        async def retry_sink(batch):
            retry_submitted.extend(batch)
            saved.update(batch)
            return len(saved)

        await worker._read_visible_account_dialog(
            _RelationFrames([names]), None, candidate_sink=retry_sink,
            initial_candidate_count=100, expected_minimum=250,
        )
        self.assertEqual(names, retry_submitted)
        self.assertEqual(set(names), saved)

    async def test_invalid_acknowledgement_cannot_confirm_retry_rows(self):
        worker = self.worker()
        with self.assertRaisesRegex(RuntimeError, "integer total"):
            await worker._read_visible_account_dialog(
                _RelationFrames([["alpha"]]), None,
                candidate_sink=AsyncMock(return_value=None), expected_minimum=1,
            )
        sink = AsyncMock(return_value=1)
        await worker._read_visible_account_dialog(
            _RelationFrames([["alpha"]]), None, candidate_sink=sink, expected_minimum=1,
        )
        sink.assert_awaited_once_with(["alpha"])

    async def test_cached_frame_never_turns_unconfirmed_bottom_into_completion(self):
        worker = self.worker()
        worker._confirm_relation_list_end = AsyncMock(return_value=False)
        sink = AsyncMock(return_value=2)
        with self.assertRaises(WorkerExecutionError) as raised:
            await worker._read_visible_account_dialog(
                _RelationFrames([["alpha", "bravo"]]), None,
                candidate_sink=sink, expected_minimum=2,
            )
        self.assertEqual("instagram_followers_list_incomplete", raised.exception.code)
        sink.assert_awaited_once_with(["alpha", "bravo"])


class _ProfileNode:
    def __init__(self, text="", *, href=None, present=False, children=()):
        self.text, self.href, self.present, self.children = text, href, present, children
        self.first = children[0] if children else self

    async def count(self):
        return len(self.children) if self.children else int(self.present)

    def nth(self, index):
        return self.children[index]

    async def is_visible(self):
        return self.present

    async def inner_text(self, **kwargs):
        return self.text

    async def get_attribute(self, name):
        return self.href if name == "href" else None


class ProfileRecoveryReadsR29Tests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_counts_ignore_suggested_offsite_and_hidden_links(self):
        for exact_links in (False, True):
            with self.subTest(exact_links=exact_links):
                followers = [
                    _ProfileNode("9999 followers", href="/suggested/followers/", present=True),
                    _ProfileNode("7777 followers", href="https://example.com/target/followers/", present=True),
                    _ProfileNode("6666 followers", href="/target/followers/", present=False),
                ]
                following = [_ProfileNode("8888 following", href="/suggested/following/", present=True)]
                if exact_links:
                    followers.append(_ProfileNode("123 followers", href="https://www.instagram.com/target/followers/?v=2", present=True))
                    following.append(_ProfileNode("45 following", href="/target/following?variant=desktop", present=True))
                header = _ProfileNode("target\n10 posts 120 followers 40 following", present=True)

                def locator(selector):
                    if "/followers" in selector:
                        return _ProfileNode(children=followers)
                    if "/following" in selector:
                        return _ProfileNode(children=following)
                    return _ProfileNode()

                worker = PlaywrightWorker(None)
                worker.page = SimpleNamespace(url="https://www.instagram.com/target/", locator=locator)
                worker._visible_profile_header = AsyncMock(return_value=header)
                worker._read_inline_profile_evidence = AsyncMock(return_value=EmbeddedProfileEvidence(is_private=True))
                result = await worker._read_visible_profile_recovery_evidence_once("target")
                self.assertEqual("private", result["visibility"])
                self.assertEqual(10, result["posts"])
                self.assertEqual(123 if exact_links else 120, result["followers"])
                self.assertEqual(45 if exact_links else 40, result["following"])


if __name__ == "__main__":
    unittest.main()
