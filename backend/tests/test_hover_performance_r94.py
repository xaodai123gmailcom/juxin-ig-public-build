"""Bound repeated layout work without caching evidence across DOM polls."""
import json
from pathlib import Path
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.profile_hover_preview import HOVER_PREVIEW_SCRIPT
from app.playwright_worker import PlaywrightWorker


class HoverPerformanceR94Tests(unittest.TestCase):
    def read(self, **options):
        result = subprocess.run(
            ['node', str(Path(__file__).parent / 'support/hover_performance.cjs')],
            input=json.dumps({'script': HOVER_PREVIEW_SCRIPT, **options}),
            capture_output=True, text=True, timeout=10, check=True,
        )
        return json.loads(result.stdout)

    def test_unrelated_accounts_do_not_require_layout_or_rendered_text_reads(self):
        for rows in (200, 2000):
            with self.subTest(rows=rows):
                result = self.read(rows=rows)
                self.assertEqual((12, 345, 67), tuple(result['first'][key]
                    for key in ('posts', 'followers', 'following')))
                self.assertLessEqual(result['counts']['rects'], 8, result['counts'])
                self.assertLessEqual(result['counts']['texts'], 8, result['counts'])

    def test_card_metric_descendants_are_read_once_for_all_three_counts(self):
        result = self.read(rows=0)
        self.assertEqual(1, result['counts']['descendants'])
        self.assertLessEqual(result['counts']['texts'], 4)

    def test_next_poll_observes_new_counts_after_dom_repaint(self):
        result = self.read(kind='repaint')
        self.assertEqual(345, result['first']['followers'])
        self.assertEqual(987, result['second']['followers'])

    def test_raw_hidden_identity_cannot_certify_a_different_visible_card(self):
        self.assertIsNone(self.read(kind='hidden-identity')['first'])

    def test_hidden_card_remains_unusable(self):
        self.assertIsNone(self.read(kind='hidden')['first'])

    def test_hidden_inline_text_cannot_hide_a_valid_visible_username(self):
        result = self.read(kind='hidden-split-identity')
        self.assertIsNotNone(result['first'])
        self.assertEqual('target.user', result['first']['username'])


class HoverDismissalPerformanceR94Tests(unittest.IsolatedAsyncioTestCase):
    async def dismissal(self, after):
        expected = {'username': 'next', 'posts': 12, 'followers': 345, 'following': 67}
        state = {'moved': False, 'hovered': False, 'slept': 0.0}

        def old_visible():
            return not state['moved'] or state['slept'] + 1e-9 < after

        async def evaluate(_script, username):
            if username == 'previous' and old_visible():
                return {**expected, 'username': 'previous'}
            return expected if username == 'next' and state['hovered'] else None

        async def move(_x, _y):
            state['moved'] = True

        async def hover(**_kwargs):
            self.assertFalse(old_visible(), 'the previous portal must not cover the next account')
            state['hovered'] = True

        async def sleep(seconds):
            state['slept'] += seconds

        row = SimpleNamespace(get_attribute=AsyncMock(return_value='/next/'), hover=hover)
        worker = PlaywrightWorker(None)
        worker._last_relation_hover_username = 'previous'
        worker._ensure_window_surface_stable = AsyncMock()
        worker.page = SimpleNamespace(evaluate=evaluate, mouse=SimpleNamespace(move=move))
        dialog = SimpleNamespace(locator=lambda _: SimpleNamespace(
            count=AsyncMock(return_value=1), nth=lambda _: row))
        with patch('app.playwright_worker.asyncio.sleep', side_effect=sleep):
            self.assertEqual(expected, await worker._read_relation_hover_preview(dialog, 'next'))
        return state

    async def test_immediate_dismissal_has_no_mandatory_polling_sleep(self):
        state = await self.dismissal(0)
        self.assertEqual(0, state['slept'])

    async def test_slow_dismissal_still_gets_the_full_existing_wait_budget(self):
        state = await self.dismissal(1.1)
        self.assertAlmostEqual(1.2, state['slept'])


if __name__ == '__main__':
    unittest.main()
