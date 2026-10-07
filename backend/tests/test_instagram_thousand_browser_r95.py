"""Mandatory release-browser complements to the 500-case synthetic queue campaign.

Each case uses production DOM extraction, scrolling, continuity and collection on
at least 1,200 independently generated identities. Only local HTML is visited.
Three bounded 90-second cases allow thousands of real CDP operations plus delayed
repaints on Windows; this is not a relaxed production loading deadline.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.playwright_worker import PlaywrightWorker
from app.collection_surface import RELATION_ROWS_SCRIPT, relation_scroll_script


class InstagramThousandBrowserR95Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.runtime = await async_playwright().start()
        self.addAsyncCleanup(self.runtime.stop)
        executable = os.environ.get('IGAC_TEST_CHROMIUM_EXECUTABLE', self.runtime.chromium.executable_path)
        if not Path(executable).is_file() and os.name != 'nt':
            if os.environ.get('IGAC_REQUIRE_COLLECTION_BROWSER') == '1':
                self.fail('Required Chromium runtime is missing')
            self.skipTest('Chromium is not installed in this non-Windows environment')
        self.browser = await self.runtime.chromium.launch(executable_path=executable)
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()

    async def collect_fixture(self, kind, *, total, row_height, client, clip, header, scale, avatars,
                              link_height=None, recommendation_only_tail=False):
        config = dict(total=total, rowHeight=row_height, client=client, clip=clip,
                      header=header, scale=scale, avatars=avatars, kind=kind, linkHeight=link_height)
        await self.page.set_content('''<style>body{margin:0}a{font:16px/17px Arial}</style>
          <div role="dialog" id="surface" style="width:420px;transform-origin:top left">
            <div id="header">Local synthetic relationship list</div>
            <section id="viewport" style="overflow-y:auto;width:400px">
              <div id="rows" style="position:relative"></div>
            </section>
          </div><script>
          (() => {
            const config = CONFIG;
            const surface = document.querySelector('#surface'), viewport = document.querySelector('#viewport');
            const rows = document.querySelector('#rows');
            surface.style.height = config.clip+'px'; surface.style.overflowY='auto';
            surface.style.transform=`scale(${config.scale})`;
            document.querySelector('#header').style.height=config.header+'px';
            viewport.style.height=config.client+'px';
            rows.style.height=(config.total+3)*config.rowHeight+'px';
            const stats = window.fixtureStats = {scrolls:0,renders:0,delayed:0};
            function render() {
              const first = Math.floor(viewport.scrollTop/config.rowHeight);
              const count = Math.ceil(config.client/config.rowHeight)+1;
              rows.innerHTML = Array.from({length:Math.min(count,config.total+3-first)},(_,index)=>{
                const number = first+index, recommended = number>=config.total;
                const name = recommended ? `suggested_${number-config.total}` : `truth_${number}`;
                const avatar = config.avatars ? `<a href="/${name}/" style="display:inline-block;width:24px;height:24px;vertical-align:top"><img alt="" style="width:24px;height:24px" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></a>` : '';
                const linkStyle = config.linkHeight ? `display:inline-block;height:${config.linkHeight}px;line-height:${config.linkHeight}px;vertical-align:top` : '';
                return `<div style="position:absolute;top:${number*config.rowHeight}px;height:${config.rowHeight}px;width:100%;overflow:hidden">${avatar}<a href="/${name}/" style="${linkStyle}">${name}</a><button>Following</button>${recommended?'<span>Suggested for you</span>':''}</div>`;
              }).join(''); stats.renders++;
            }
            viewport.addEventListener('scroll',()=>{
              stats.scrolls++;
              const delay=stats.scrolls%61===0?320:16;
              if(delay>16)stats.delayed++;
              setTimeout(render,delay);
            });
            render();
          })();</script>'''.replace('CONFIG', json.dumps(config)))
        worker = PlaywrightWorker(None)
        worker.page = self.page
        worker.collection_poll_interval_seconds = .01
        worker.collection_loading_grace_seconds = .5
        worker.collection_settled_idle_rounds = 2
        worker._guard = AsyncMock()
        worker._relation_surface_failure = AsyncMock(return_value=None)
        worker._has_visible_relation_loading_indicator = AsyncMock(return_value=False)
        saved = set()
        batches = 0
        async def sink(batch):
            nonlocal batches
            batches += 1
            saved.update(batch)
            return {'total':len(saved)}
        started = time.monotonic()
        try:
            # The displayed-count hint is deliberately higher than ground truth.
            # It cannot fabricate seven accounts or force an automatic rewind.
            result = await asyncio.wait_for(worker._read_visible_account_dialog(
                self.page.locator('#surface'), None, surface_kind=kind,
                candidate_sink=sink, expected_minimum=total+7), 90)
        except BaseException:
            print('IG_THOUSAND_FAILURE', json.dumps({'kind':kind,'committed':len(saved),
                'missing_first':[f'truth_{n}' for n in range(total) if f'truth_{n}' not in saved][:20],
                'unexpected':sorted(saved-{f'truth_{n}' for n in range(total)})[:20],
                'scroll':worker.last_relation_scroll,
                'diagnostics':getattr(worker,'last_relation_scroll_diagnostics',{})}))
            raise
        self.assertEqual([], result, 'durable sink owns the result batch')
        self.assertEqual({f'truth_{n}' for n in range(total)}, saved)
        self.assertEqual(0, worker.last_relation_scroll['invalid'])
        self.assertGreater(worker.last_relation_scroll['movements'],100)
        self.assertLess(worker.last_relation_scroll['movements'],total)
        stats = await self.page.evaluate('fixtureStats')
        self.assertGreater(stats['delayed'],0)
        self.assertGreater(stats['renders'],100)
        if recommendation_only_tail:
            surface = self.page.locator('#surface')
            tail = await surface.evaluate(RELATION_ROWS_SCRIPT)
            self.assertEqual([], tail['hrefs'])
            self.assertTrue(tail['recommendations_reached'])
            self.assertEqual({'suggested_0', 'suggested_1', 'suggested_2'},
                {row['username'] for row in tail['positioned_rows']})
            self.assertTrue((await surface.evaluate(relation_scroll_script('measure')))['bottom'])
            self.assertEqual(client-clip, await surface.evaluate('node => node.scrollTop'))
        print('IG_THOUSAND_BROWSER',json.dumps({'kind':kind,'ground_truth':total,'committed':len(saved),
            'seconds':round(time.monotonic()-started,3),'batches':batches,**stats}))

    async def test_followers_1200_short_links_clipped_virtual_rows_delayed_tail(self):
        await self.collect_fixture('followers',total=1200,row_height=40,client=600,
                                   clip=260,header=20,scale=1,avatars=False)

    async def test_following_1237_avatar_name_overlap_scaled_recommendations(self):
        await self.collect_fixture('following',total=1237,row_height=48,client=480,
                                   clip=264,header=24,scale=1.25,avatars=True)

    async def test_followers_1200_case13_recommendation_repaint_then_empty_real_tail(self):
        await self.collect_fixture('followers', total=1200, row_height=60, client=280,
            clip=220, header=0, scale=1.5, avatars=False, link_height=18,
            recommendation_only_tail=True)
