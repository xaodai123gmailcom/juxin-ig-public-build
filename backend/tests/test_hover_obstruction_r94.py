"""Hover the visible identity and let the preceding card disappear first."""
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.playwright_worker import PlaywrightWorker


class HoverObstructionR94Tests(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_duplicate_anchor_cannot_scroll_the_list_during_hover(self):
        expected = {'username': 'target', 'posts': 12, 'followers': 31, 'following': 40}
        hidden = SimpleNamespace(get_attribute=AsyncMock(return_value='/target/'),
            hover=AsyncMock(side_effect=RuntimeError('hidden duplicate cannot be hovered')))
        visible = SimpleNamespace(get_attribute=AsyncMock(return_value='/target/'), hover=AsyncMock())
        async def evaluate_all(script):
            harness = '''const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
global.getComputedStyle=n=>({display:n.hidden?'none':'block',visibility:'visible'});
const nodes=[true,false].map(hidden=>({hidden,getAttribute:()=>'/target/',
getBoundingClientRect:()=>({top:10,bottom:30,left:0,right:60,width:hidden?0:60,height:hidden?0:20})}));
process.stdout.write(JSON.stringify(eval('('+source+')')(nodes)));'''
            result = subprocess.run(['node', '-e', harness], input=json.dumps(script),
                capture_output=True, text=True, timeout=5, check=True)
            return json.loads(result.stdout)
        links = SimpleNamespace(evaluate_all=evaluate_all, nth=lambda index: [hidden, visible][index])
        worker = PlaywrightWorker(None)
        worker._ensure_window_surface_stable = AsyncMock()
        worker.page = SimpleNamespace(evaluate=AsyncMock(return_value=expected))
        with patch('app.playwright_worker.asyncio.sleep', new=AsyncMock()):
            result = await worker._read_relation_hover_preview(SimpleNamespace(locator=lambda _: links), 'target')
        self.assertEqual(expected, result)
        hidden.hover.assert_not_awaited()
        visible.hover.assert_awaited_once()

    async def test_previous_account_card_must_disappear_before_next_hover(self):
        expected = {'username': 'next', 'posts': 12, 'followers': 31, 'following': 40}
        state = {'old_visible': True, 'moved': False, 'checks': 0, 'hovered': False}
        async def read(_script, username):
            if username == 'previous' and state['old_visible']:
                if state['moved']:
                    state['checks'] += 1
                    if state['checks'] >= 3:
                        state['old_visible'] = False
                        return None
                return {**expected, 'username': 'previous'}
            return expected if state['hovered'] and username == 'next' else None
        async def move(_x, _y):
            state['moved'] = True
        async def hover(**_kwargs):
            if state['old_visible']:
                raise RuntimeError('previous hover portal intercepts input')
            state['hovered'] = True
        row = SimpleNamespace(get_attribute=AsyncMock(return_value='/next/'), hover=AsyncMock(side_effect=hover))
        links = SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: row)
        worker = PlaywrightWorker(None)
        worker._last_relation_hover_username = 'previous'
        worker._ensure_window_surface_stable = AsyncMock()
        worker.page = SimpleNamespace(evaluate=read, mouse=SimpleNamespace(move=move))
        with patch('app.playwright_worker.asyncio.sleep', new=AsyncMock()):
            result = await worker._read_relation_hover_preview(SimpleNamespace(locator=lambda _: links), 'next')
        self.assertEqual(expected, result)
        self.assertEqual('next', worker._last_relation_hover_username)
        row.hover.assert_awaited_once()
        self.assertEqual(3, state['checks'])


if __name__ == '__main__':
    unittest.main()
