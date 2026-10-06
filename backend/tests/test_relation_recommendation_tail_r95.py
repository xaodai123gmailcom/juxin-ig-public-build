"""A positioned recommendation repaint is scroll evidence, never a candidate.

The eight-frame, 12-account geometry is the minimal version of IG500 case 13:
60px cards, 18px links, 1.5 scale, 280px inner viewport clipped to 220px.
Production DOM scripts generate the frames; no browser or live account is used.
"""
import asyncio
import copy
import json
import re
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.collection_surface import RELATION_ROWS_SCRIPT, relation_scroll_script
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError


def tail_frames():
    options = dict(scale=1.5, row_height=60, client=280, clip=220, header=0,
        overflow='auto', virtual=True, link_height=18, link_offset=0,
        duplicate_links=False, recommendations=True, resize_at=-1, resize_to=280)
    result = subprocess.run(['node', str(Path(__file__).resolve().parents[2] /
        'scripts/ig500_geometry.cjs')], input=json.dumps(dict(names=[f'truth_{n}' for n in range(12)],
        options=options, projection=RELATION_ROWS_SCRIPT,
        advance=relation_scroll_script('advance', []), measure=relation_scroll_script('measure'))),
        capture_output=True, text=True, check=True, timeout=10)
    return json.loads(result.stdout)['frames']


class TailSurface:
    def __init__(self, frames, saved, *, delayed=0):
        self.frames, self.saved, self.delayed = frames, saved, delayed
        self.index = self.previous = self.pending = self.moves = self.delayed_reads = 0

    async def evaluate(self, script):
        if script == RELATION_ROWS_SCRIPT:
            index = self.index
            if self.pending:
                self.pending -= 1
                self.delayed_reads += 1
                index = self.previous
            return {k: self.frames[index][k] for k in ('hrefs', 'positioned_rows', 'recommendations_reached')}
        frame = self.frames[self.index]
        if 'relation-action: measure' in script:
            return frame['measurement']
        if 'relation-action: advance' in script:
            assert not self.pending, 'Cannot advance an unacknowledged delayed frame'
            names = set(json.loads(re.search(r'const acknowledgedNames = new Set\((\[.*?\])\);', script)[1]))
            assert names == {row['username'] for row in frame['positioned_rows']}
            assert {href.strip('/') for href in frame['hrefs']} <= self.saved, 'Uncommitted real rows were skipped'
            move = frame.get('movement', {**frame['measurement'], 'moved': False})
            if move.get('moved'):
                self.previous, self.index = self.index, self.index + 1
                self.pending = self.delayed
                self.moves += 1
            return move
        raise AssertionError('Unexpected reset or DOM operation')

    async def inner_text(self, **_):
        return 'Followers'


class RelationRecommendationTailR95Tests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.frames = tail_frames()

    def setup_reader(self, *, delayed=0, frames=None, grace=.02):
        saved = set()
        surface = TailSurface(copy.deepcopy(frames or self.frames), saved, delayed=delayed)
        worker = PlaywrightWorker(None)
        worker.page = SimpleNamespace(url='https://www.instagram.com/source/')
        worker.collection_poll_interval_seconds = 0
        worker.collection_loading_grace_seconds = grace
        worker.collection_settled_idle_rounds = 2
        worker._guard = AsyncMock()
        worker._relation_surface_failure = AsyncMock(return_value=None)
        worker._has_visible_relation_loading_indicator = AsyncMock(return_value=False)
        clock = SimpleNamespace(now=0.0, calls=0, tail_elapsed=0.0)
        def tick():
            # Real page latency is virtual in this fixture. Retain fine ticks
            # through every delayed repaint, then simulate idle time once all
            # real rows are saved and a recommendation tail has painted. The
            # 20s production grace still elapses; thousands of zero-delay debug
            # awaits must not make that logical wait depend on Windows speed.
            frame = surface.frames[surface.index]
            stable_tail = (not surface.pending and len(saved) == 12
                and frame['recommendations_reached']
                and frame['measurement'].get('valid') is not False)
            step = 1.0 if stable_tail and grace >= 1 else .01
            clock.now += step
            clock.calls += 1
            if stable_tail:
                clock.tail_elapsed += step
            return clock.now
        worker._collection_monotonic = tick
        worker._fixture_clock = clock
        async def sink(batch):
            saved.update(batch)
            return len(saved)
        return worker, surface, saved, sink

    async def read(self, worker, surface, sink):
        return await asyncio.wait_for(worker._read_visible_account_dialog(surface, None,
            surface_kind='followers', expected_source_username='source', candidate_sink=sink,
            expected_minimum=19), 8)

    async def test_new_positioned_recommendations_acknowledge_repaint_without_becoming_candidates(self):
        for delayed, grace in ((0, .02), (3, .02), (3, 20)):
            with self.subTest(delayed=delayed, grace=grace):
                worker, surface, saved, sink = self.setup_reader(delayed=delayed, grace=grace)
                self.assertEqual([], await self.read(worker, surface, sink))
                self.assertEqual({f'truth_{n}' for n in range(12)}, saved)
                self.assertEqual(7, surface.moves)
                self.assertTrue(surface.frames[surface.index]['measurement']['bottom'])
                self.assertEqual([], surface.frames[surface.index]['hrefs'])
                self.assertGreaterEqual(surface.delayed_reads, delayed)
                self.assertTrue(worker._relation_recommendations_reached)
                if grace == 20:
                    self.assertGreaterEqual(worker._fixture_clock.tail_elapsed, grace)
                    self.assertLess(worker._fixture_clock.calls, 500)

    async def test_higher_header_deadline_crossing_does_not_reclassify_recommendation_tail_as_blank(self):
        # With three stale reads per scroll, the .01-step clock crosses the
        # deadline between the old semantic and idle checks at the empty tail.
        # A single expiry decision must keep their classification consistent.
        worker, surface, saved, sink = self.setup_reader(delayed=3, grace=.02)
        self.assertEqual([], await self.read(worker, surface, sink))
        self.assertEqual(12, len(saved))  # Header19 is a hint, never seven invented rows.
        self.assertEqual([], surface.frames[surface.index]['hrefs'])
        self.assertTrue(worker._relation_recommendations_reached)

    async def test_recommendations_with_missing_or_wrong_position_anchor_do_not_acknowledge(self):
        for changed in ('missing', 'wrong_position'):
            worker, surface, saved, sink = self.setup_reader()
            frame = surface.frames[5]
            if changed == 'missing':
                frame['positioned_rows'] = [r for r in frame['positioned_rows'] if r['recommended']]
            else:
                for row in frame['positioned_rows']:
                    row['y'] += 200
            with self.subTest(changed=changed), self.assertRaises(WorkerExecutionError):
                await self.read(worker, surface, sink)
            self.assertEqual(5, surface.moves)
            self.assertEqual({f'truth_{n}' for n in range(12)}, saved)

    async def test_blank_repaint_after_saved_rows_cannot_prove_completion(self):
        worker, surface, saved, sink = self.setup_reader()
        surface.frames[5].update(hrefs=[], positioned_rows=[], recommendations_reached=False)
        with self.assertRaises(WorkerExecutionError):
            await self.read(worker, surface, sink)
        self.assertEqual(5, surface.moves)
        self.assertEqual({f'truth_{n}' for n in range(12)}, saved)

    async def test_pending_loader_or_clipped_real_tail_stays_incomplete(self):
        for blocked in ('loader', 'tail_clipped'):
            worker, surface, saved, sink = self.setup_reader()
            if blocked == 'loader':
                worker._has_visible_relation_loading_indicator = AsyncMock(return_value=True)
            else:
                surface.frames[-1]['measurement'].update(bottom=False, tail_clipped=True)
            with self.subTest(blocked=blocked), self.assertRaises(WorkerExecutionError):
                await self.read(worker, surface, sink)
            self.assertEqual({f'truth_{n}' for n in range(12)}, saved)

    async def test_uncommitted_rows_or_wrong_source_never_allow_semantic_end(self):
        for blocked in ('sink', 'source'):
            worker, surface, saved, sink = self.setup_reader()
            if blocked == 'sink':
                async def sink(_batch):
                    raise RuntimeError('durable write rejected')
                error = RuntimeError
            else:
                worker.page.url = 'https://www.instagram.com/unrelated/'
                error = WorkerExecutionError
            with self.subTest(blocked=blocked), self.assertRaises(error):
                await self.read(worker, surface, sink)
            self.assertEqual(0, surface.moves)
            self.assertEqual(set(), saved)
