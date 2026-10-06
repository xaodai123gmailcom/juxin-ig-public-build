"""A profile count is not a quota; completion still needs a durable healthy tail."""
from __future__ import annotations

import asyncio
import unittest

from app.collection_surface import RELATION_ROWS_SCRIPT
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError, _visible_relation_count_estimate


def frame(*names, recommendations=False):
    return {"hrefs": [f"/{name}/" for name in names],
            "recommendations_reached": recommendations}


class Worker(PlaywrightWorker):
    def __init__(self, *, loading=False, failure=None):
        super().__init__(None)
        self.logical = 0.0
        self.collection_loading_grace_seconds = 2.0
        self.collection_poll_interval_seconds = 0.0
        self.collection_settled_idle_rounds = 2
        self.loading = loading
        self.failure = failure

    def _collection_monotonic(self):
        return self.logical + super()._collection_monotonic()

    async def _guard(self):
        pass

    async def _has_visible_relation_loading_indicator(self, _dialog):
        return self.loading

    async def _relation_surface_failure(self, _dialog):
        return self.failure


class Surface:
    def __init__(self, worker, names, *, bottom=True, recommendations=False,
                 final_names=None, unstable_geometry=False):
        self.worker = worker
        self.names = names
        self.bottom = bottom
        self.recommendations = recommendations
        self.final_names = final_names
        self.unstable_geometry = unstable_geometry
        self.reads = 0
        self.measures = 0
        self.measure_times = []
        self.events = []

    async def evaluate(self, script):
        if script == RELATION_ROWS_SCRIPT:
            self.worker.logical += .5
            self.reads += 1
            names = self.final_names if self.measures >= 2 and self.final_names is not None else self.names
            self.events.append(("read", tuple(names)))
            return frame(*names, recommendations=self.recommendations)
        if "relation-action: measure" in script:
            self.measures += 1
            self.measure_times.append(self.worker.logical)
            self.events.append(("measure", self.measures))
        return {"valid": True, "bottom": self.bottom, "moved": False,
                "height": 1000 + (self.measures if self.unstable_geometry else 0),
                "client": 400, "top": 600}

    async def inner_text(self, **_kwargs):
        return "Followers"


class RelationCompletionR43Tests(unittest.IsolatedAsyncioTestCase):
    async def run_reader(self, worker, surface, *, initial=(), header=167, kind="followers", sink_failure=False):
        saved = set(initial)
        batches = []

        async def sink(batch):
            if sink_failure:
                raise RuntimeError("candidate transaction failed")
            saved.update(batch)
            batches.append(list(batch))
            surface.events.append(("commit", tuple(batch)))
            return {"total": len(saved)}

        result = await asyncio.wait_for(worker._read_visible_account_dialog(
            surface, None, surface_kind=kind, candidate_sink=sink,
            initial_candidate_count=len(saved),
            expected_minimum=_visible_relation_count_estimate(str(header), header).completion_floor,
        ), 5)
        return result, saved, batches

    async def test_167_header_164_rows_finishes_after_grace_at_healthy_bottom(self):
        for kind in ("followers", "following"):
            with self.subTest(kind=kind):
                worker = Worker()
                names = [f"user_{i}" for i in range(164)]
                surface = Surface(worker, names)
                result, saved, batches = await self.run_reader(worker, surface, kind=kind)
                self.assertEqual([], result)
                self.assertEqual(set(names), saved)
                self.assertEqual([100, 64], list(map(len, batches)))
                self.assertEqual(3, surface.measures)
                self.assertGreaterEqual(surface.measure_times[0], 2.5,
                    "a short physical list must use the no-progress grace")

    async def test_large_header_difference_is_not_another_percentage_gate(self):
        worker = Worker()
        surface = Surface(worker, ["one_real_row"])
        _, saved, _ = await self.run_reader(worker, surface, header=9000)
        self.assertEqual({"one_real_row"}, saved)
        self.assertEqual(3, surface.measures)

    async def test_164_rows_with_loading_stay_incomplete(self):
        worker = Worker(loading=True)
        surface = Surface(worker, [f"user_{i}" for i in range(164)])
        with self.assertRaises(WorkerExecutionError) as caught:
            await self.run_reader(worker, surface)
        self.assertEqual("instagram_followers_list_incomplete", caught.exception.code)
        self.assertEqual(0, surface.measures)

    async def test_164_rows_above_bottom_stay_incomplete(self):
        worker = Worker()
        surface = Surface(worker, [f"user_{i}" for i in range(164)], bottom=False)
        with self.assertRaises(WorkerExecutionError) as caught:
            await self.run_reader(worker, surface)
        self.assertEqual("instagram_followers_list_incomplete", caught.exception.code)

    async def test_all_previously_saved_rows_complete_without_new_accounts(self):
        worker = Worker()
        names = [f"user_{i}" for i in range(164)]
        surface = Surface(worker, names)
        _, saved, batches = await self.run_reader(worker, surface, initial=names)
        self.assertEqual(set(names), saved)
        self.assertEqual([100, 64], list(map(len, batches)),
            "replayed rows are acknowledged durably without counting them twice")
        self.assertEqual(3, surface.measures)

    async def test_recommendation_tail_still_finishes_without_profile_quota(self):
        worker = Worker()
        surface = Surface(worker, ["last_real"], recommendations=True)
        _, saved, _ = await self.run_reader(worker, surface)
        self.assertEqual({"last_real"}, saved)
        self.assertEqual(3, surface.measures)
        self.assertGreaterEqual(surface.measure_times[0], 2.5,
            'recommendations cannot bypass the no-progress grace for a short list')

    async def test_final_repaint_is_committed_then_bottom_is_reconfirmed(self):
        worker = Worker()
        names = [f"user_{i}" for i in range(164)]
        surface = Surface(worker, names, final_names=names + ["late_row"])
        _, saved, _ = await self.run_reader(worker, surface)
        self.assertEqual(set(names) | {"late_row"}, saved)
        self.assertGreaterEqual(surface.measures, 4)
        late_commit = next(index for index, event in enumerate(surface.events)
                           if event == ("commit", ("late_row",)))
        final_measure = max(index for index, event in enumerate(surface.events)
                            if event[0] == "measure")
        self.assertLess(late_commit, final_measure)

    async def test_unstable_bottom_geometry_does_not_finish(self):
        worker = Worker()
        surface = Surface(worker, ["real_row"], unstable_geometry=True)
        with self.assertRaises(WorkerExecutionError):
            await self.run_reader(worker, surface)

    async def test_page_failure_overrides_apparent_bottom(self):
        worker = Worker(failure="instagram_network_unavailable")
        surface = Surface(worker, ["real_row"])
        with self.assertRaises(WorkerExecutionError) as caught:
            await self.run_reader(worker, surface)
        self.assertEqual("instagram_network_unavailable", caught.exception.code)

    async def test_recommendations_without_any_source_row_cannot_finish(self):
        for initial in ([], [f"old_{i}" for i in range(164)]):
            with self.subTest(initial_count=len(initial)):
                worker = Worker()
                surface = Surface(worker, [], recommendations=True)
                with self.assertRaises(WorkerExecutionError):
                    await self.run_reader(worker, surface, initial=initial)

    async def test_failed_candidate_commit_cannot_confirm_completion(self):
        worker = Worker()
        surface = Surface(worker, ["real_row"], recommendations=True)
        with self.assertRaisesRegex(RuntimeError, "transaction failed"):
            await self.run_reader(worker, surface, sink_failure=True)
        self.assertEqual(0, surface.measures)


if __name__ == "__main__":
    unittest.main()
