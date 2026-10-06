from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collection_surface import relation_scroll_script
from app.execution_manager import ExecutionManager
from app.playwright_worker import PlaywrightWorker, VisibleProfile, WorkerExecutionError, classify_guard_state


class GuardEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def test_profile_and_caption_words_are_not_account_restrictions(self):
        for phrase in ("security check tips", "try again later", "how to log in to Instagram", "action blocked"):
            for text in (f"Alice\n25 posts 125 followers 196 following\n{phrase}", f"My motto: {phrase}"):
                with self.subTest(text=text):
                    self.assertIsNone(classify_guard_state("https://www.instagram.com/alice/", text))

    def test_genuine_routes_and_standalone_notices_still_stop(self):
        cases = [("/accounts/login/", "", "instagram_login_required"),
                 ("/challenge/123/", "", "instagram_challenge"),
                 ("/alice/", "Try again later\nPlease wait a few minutes before you try again.", "instagram_rate_limited"),
                 ("/alice/", "Action blocked", "instagram_action_blocked"),
                 ("/alice/", "抱歉，无法访问此页面", "instagram_content_not_visible")]
        for path, text, expected in cases:
            self.assertEqual(expected, classify_guard_state("https://www.instagram.com" + path, text))
        self.assertIsNone(classify_guard_state("https://www.instagram.com/alice/?next=/accounts/login", "Alice"))

    async def test_real_blocking_dialog_overrides_healthy_background_profile(self):
        class Body:
            async def evaluate(self, script):
                return ["Try again later\nPlease wait a few minutes before you try again."]
        class Page:
            def locator(self, selector):
                return Body()
        result = await PlaywrightWorker(None)._classify_page_guard(Page(), "https://www.instagram.com/alice/", "Alice\n25 posts 125 followers 196 following")
        self.assertEqual("instagram_rate_limited", result)


class ScrollGeometryTests(unittest.TestCase):
    def geometry(self, *, outer_overflow="visible", stuck=False, empty=False, header_link=False):
        # Plain geometry interface doubles. This launches no browser or DOM engine.
        program = r'''
const fs = require('node:fs'), input = JSON.parse(fs.readFileSync(0, 'utf8'));
global.getComputedStyle = el => ({overflowY: el.overflow, display:'block', visibility:'visible'});
const rect = (top=0, height=600) => ({top,bottom:top+height,left:0,right:400,width:400,height});
const row = {getBoundingClientRect:()=>rect(500-inner.scrollTop,40), getAttribute:()=>'/alice/'};
const header = {getBoundingClientRect:()=>rect(20,40), getAttribute:()=>'/source/'};
const inner = {overflow:'auto',clientHeight:600,scrollHeight:6000,_top:0,
  get scrollTop(){return this._top},set scrollTop(v){if(!input.stuck)this._top=v},
  getBoundingClientRect:()=>rect(),contains:el=>el===inner||el===row,
  querySelector:()=>null,
  querySelectorAll:()=>input.empty?[]:[row]};
const outer = {overflow:input.outer_overflow,clientHeight:690,scrollHeight:701,scrollTop:0,
  getBoundingClientRect:()=>rect(0,690),contains:el=>el===outer||el===row||el===inner||el===header,
  querySelector:()=>null,
  querySelectorAll:selector=>selector==='div'?[inner]:selector==='*'?[inner,...(input.empty?[]:input.header_link?[header,row]:[row])]:input.empty?[]:input.header_link?[header,row]:[row]};
row.parentElement = inner;
inner.parentElement = outer;
header.parentElement = outer;
const state = eval('(' + input.script + ')')(outer);
process.stdout.write(JSON.stringify({state,outer:outer.scrollTop,inner:inner.scrollTop}));
'''
        result = subprocess.run(["node", "-e", program], input=json.dumps({"script": relation_scroll_script("advance"), "outer_overflow": outer_overflow, "stuck": stuck, "empty": empty, "header_link": header_link}), encoding="utf-8", capture_output=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_outer_overflow_does_not_steal_list_scroll(self):
        for overflow in ("visible", "auto"):
            result = self.geometry(outer_overflow=overflow)
            self.assertEqual(0, result["outer"])
            self.assertEqual(390, result["inner"])
            self.assertTrue(result["state"]["moved"])
            self.assertFalse(result["state"]["bottom"])
            self.assertEqual([{"username": "alice", "y": 520, "expected_y": 130}], result["state"]["anchor_rows"])
        self.assertEqual(390, self.geometry(outer_overflow="auto", header_link=True)["inner"])

    def test_failed_scroll_and_missing_rows_are_not_completion(self):
        self.assertFalse(self.geometry(stuck=True)["state"]["moved"])
        result = self.geometry(empty=True)["state"]
        self.assertFalse(result["valid"])
        self.assertFalse(result["bottom"])


class Dialog:
    async def evaluate(self, script):
        return {"valid": True, "moved": False, "top": 0}


class LogicalWorker(PlaywrightWorker):
    def __init__(self, batches, *, spinner=False, grace=20):
        super().__init__(None)
        self.batches = batches
        self.reads = 0
        self.logical = 0.0
        self.spinner = spinner
        self.collection_loading_grace_seconds = grace
        self.collection_poll_interval_seconds = 0

    def _collection_monotonic(self):
        return self.logical + super()._collection_monotonic()

    async def _guard(self):
        pass

    async def _read_visible_account_hrefs(self, *args, **kwargs):
        self.reads += 1
        self.logical += .5
        if self.reads > 200:
            raise AssertionError("scan has no bounded progress check")
        return self.batches(self.reads)

    async def _has_visible_relation_loading_indicator(self, dialog):
        return self.spinner

    async def _confirm_relation_list_end(self, dialog):
        return True

    async def _raise_incomplete_relation_list(self, *args, **kwargs):
        raise WorkerExecutionError("incomplete", reason="instagram_followers_list_incomplete")


class CollectionTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_batch_without_loader_gets_full_grace(self):
        worker = LogicalWorker(lambda n: ['/alpha/'] if n < 13 else ['/alpha/', '/beta/'])
        stored = set()
        async def sink(batch):
            stored.update(batch)
            return len(stored)
        await worker._read_visible_account_dialog(Dialog(), None, candidate_sink=sink, candidate_total_limit=2, expected_minimum=2)
        self.assertEqual({'alpha', 'beta'}, stored)
        self.assertEqual(13, worker.reads)

    async def test_short_physical_bottom_finishes_only_after_full_grace(self):
        worker = LogicalWorker(lambda n: ['/alpha/'])
        async def sink(batch):
            return 1
        # r43 removes the header quota while preserving the time given to slow
        # missing-loader responses and the independent physical-end proof.
        result = await worker._read_visible_account_dialog(Dialog(), None, candidate_sink=sink, expected_minimum=100)
        self.assertEqual([], result)
        self.assertGreaterEqual(worker.logical, 20)

    async def test_recycled_old_rows_do_not_reset_progress_forever(self):
        worker = LogicalWorker(lambda n: ['/alpha/'] if n % 2 else ['/beta/'])
        progress = []
        async def sink(batch):
            return 2
        async def report(payload):
            progress.append(payload)
        with self.assertRaises(WorkerExecutionError):
            await worker._read_visible_account_dialog(Dialog(), None, candidate_sink=sink, initial_candidate_count=2, expected_minimum=100, scan_progress_sink=report)
        self.assertEqual([], progress)
        self.assertLess(worker.reads, 50)

    async def test_real_forward_replay_can_reach_new_rows(self):
        class Moving(Dialog):
            top = 0
            async def evaluate(self, script):
                if "relation-action: advance" not in script:
                    raise AssertionError("Only forward scrolling may change the replay frame")
                self.top += 400
                return {"valid": True, "moved": True, "top": self.top}
        dialog = Moving()
        # Reading the same DOM cannot scroll it. Replay 24 saved frames before
        # reaching a new account, even with no new database inserts meanwhile.
        worker = LogicalWorker(lambda _: [f'/saved_{dialog.top // 400}/'] if dialog.top < 9600 else ['/new/'], grace=2)
        async def sink(batch):
            return 101 if batch == ['new'] else 100
        await worker._read_visible_account_dialog(dialog, None, candidate_sink=sink, initial_candidate_count=100, candidate_total_limit=101, expected_minimum=101)
        self.assertEqual(9600, dialog.top)
        self.assertEqual(49, worker.reads)

    async def test_quiet_scan_pause_blocks_reads_and_excludes_pause_duration(self):
        class ControlledClockWorker(LogicalWorker):
            # Exercise the production clock, including its pause subtraction.
            _collection_monotonic = PlaywrightWorker._collection_monotonic

        worker = ControlledClockWorker(lambda n: ['/alpha/'] if n < 13 else ['/alpha/', '/beta/'])
        # Advance only the worker's wall clock, not asyncio's real scheduler or
        # timeout clock. Mixing real scheduling overhead into a 25 ms grace made
        # this test expire before beta on slower/debug Windows event loops.
        worker_asyncio = SimpleNamespace(**vars(asyncio))
        worker_asyncio.get_running_loop = lambda: SimpleNamespace(time=lambda: worker.logical)
        gate, entered = asyncio.Event(), asyncio.Event()
        gate.set()
        async def checkpoint():
            if not gate.is_set(): entered.set()
            await gate.wait()
        worker.collection_checkpoint = checkpoint
        stored = set()
        async def sink(batch):
            stored.update(batch)
            return len(stored)
        async def progress(payload):
            if worker.reads == 1: gate.clear()
        with patch('app.playwright_worker.asyncio', worker_asyncio):
            running = asyncio.create_task(worker._read_visible_account_dialog(Dialog(), None, candidate_sink=sink, candidate_total_limit=2, expected_minimum=2, scan_progress_sink=progress))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                pause_seconds = 60.0
                self.assertGreater(pause_seconds, worker.collection_loading_grace_seconds)
                worker.logical += pause_seconds
                await asyncio.sleep(0)  # Let runnable work proceed while the gate stays shut.
                self.assertFalse(running.done())
                self.assertEqual(1, worker.reads)
                self.assertEqual({'alpha'}, stored)
                gate.set()
                await asyncio.wait_for(running, 5)
                self.assertEqual({'alpha', 'beta'}, stored)
                self.assertEqual(13, worker.reads)
                self.assertEqual(pause_seconds, worker._collection_paused_seconds)
                self.assertEqual(6.5, worker._collection_monotonic())
            finally:
                gate.set()
                if not running.done(): running.cancel()
                await asyncio.gather(running, return_exceptions=True)


class RecoveryBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_retained_profile_retry_opens_new_page_before_repeating_old_read(self):
        class Worker(PlaywrightWorker):
            opened = False
            reads = 0
            async def _recover_stalled_profile_page(self, username):
                self.opened = True
                return True
            async def _read_visible_profile_once(self, target, **kwargs):
                if not self.opened:
                    raise AssertionError('explicit retry returned to the failed old page')
                self.reads += 1
                return VisibleProfile(username=target, visibility='public', followers=1, following=2, posts=3)
        worker = Worker(None)
        worker.request_page_replacement('alpha')
        result = await worker.read_visible_profile('alpha')
        self.assertEqual('alpha', result.username)
        self.assertEqual(1, worker.reads)
        self.assertEqual({}, worker._fresh_page_retry_targets)

    async def test_explicit_retry_rearms_retained_children_without_reopening_completed_source(self):
        class Manager(ExecutionManager):
            async def _set_network_waiting(self, *args, **kwargs): return 1
            async def _wait_retry_delay(self, *args, **kwargs): return True
            async def _wait_for_network_probe_slot(self, *args, **kwargs): return True
        worker = PlaywrightWorker(None)
        worker.connection_healthy = lambda: asyncio.sleep(0, result=True)
        for name in ('alpha', 'beta'):
            child = PlaywrightWorker(None)
            child._page_recovery_targets[name] = None
            worker._deferred_screening_workers[name] = child
        pause = asyncio.Event(); pause.set()
        control = SimpleNamespace(stop_event=asyncio.Event(), pause_event=pause, network_waiters={}, network_retry_gate=asyncio.Semaphore(1))
        error = WorkerExecutionError('candidate failed', reason='instagram_page_recovery_exhausted')
        error.details.update(original_reason='instagram_profile_not_ready', recovery_target='alpha', source_discovery_complete=True, auto_retry=False)
        generation = await Manager(None, None)._recover_network_connection(control, worker, 'same-profile', error, target={'id':'source-id','username':'source'}, mode='followers')
        self.assertEqual(1, generation)
        self.assertEqual({}, worker._fresh_page_retry_targets)
        self.assertTrue(all(not child._page_recovery_targets for child in worker._deferred_screening_workers.values()))
        self.assertTrue(all(name in child._fresh_page_retry_targets for name, child in worker._deferred_screening_workers.items()))

    async def test_retained_screening_page_retry_and_cleanup_keep_account_ownership(self):
        class Child:
            retries = []
            closed = 0
            def prepare_page_retry(self, target): self.retries.append(target)
            async def disconnect(self): self.closed += 1
        worker = PlaywrightWorker(None)
        child = Child()
        worker._deferred_screening_workers['alpha'] = child
        worker.prepare_page_retry('alpha')
        self.assertEqual(['alpha'], child.retries)
        self.assertEqual(0, child.closed)
        await worker.disconnect()
        self.assertEqual(1, child.closed)
        self.assertEqual({}, worker._deferred_screening_workers)

    async def test_success_resets_budget_but_failed_replacement_does_not(self):
        class Page:
            closed = False
            async def close(self): self.closed = True
        class Worker(PlaywrightWorker):
            openings = 0
            async def _recover_stalled_profile_page(self, target):
                self.openings += 1
                self._pending_recovery_old = (self.page, None, self.page)
                self.page = Page()
                return True
        worker = Worker(None); worker.page = Page()
        await worker._replace_stuck_page_once('alpha')
        await worker._finish_page_recovery(progressed=True)
        await worker._replace_stuck_page_once('alpha')
        self.assertEqual(2, worker.openings)
        await worker._finish_page_recovery(progressed=False)
        with self.assertRaises(WorkerExecutionError):
            await worker._replace_stuck_page_once('alpha')
        self.assertEqual(2, worker.openings)
