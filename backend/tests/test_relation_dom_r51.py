"""Real Chromium regressions for visible relation rows and their metadata.

Only locally generated pages are used. These fixtures exercise the production
DOM scripts, including scroll reset before collection may submit candidates.
"""
import os
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.collection_surface import RELATION_ROWS_SCRIPT, _RELATION_VISIBLE, relation_scroll_script


STYLE = '''<style>
#surface { width:500px; height:420px; }
#viewport { height:280px; overflow-y:auto; }
.row { height:72px; display:flex; align-items:center; gap:10px; }
.info { display:flex; flex-direction:column; }
a { display:inline-block; }
img { width:44px; height:44px; }
</style>'''


def account_row(name, *, metadata='', recommendation=False, avatar=True):
    picture = f'<a href="/{name}/"><img alt="avatar"></a>' if avatar else ''
    recommendation = '<span>Suggested for you</span>' if recommendation else ''
    return (f'<div class="row">{picture}<div class="info">'
            f'<a href="/{name}/">{name}</a><span>Display name</span>'
            f'{recommendation}{metadata}</div><button>关注</button></div>')


def document(rows, *, before='', after='', css=''):
    return (STYLE + '<style>' + css + '</style><div id="surface" role="dialog">'
            '<h2>粉丝</h2><input placeholder="搜索"><section id="viewport">'
            + before + ''.join(rows) + after + '</section></div>')


class RelationClippedVisibilityTests(unittest.TestCase):
    def test_clipped_inline_recommendation_keeps_semantic_account_card(self):
        for overflow in ('hidden', 'clip'):
            with self.subTest(overflow=overflow):
                rows = []
                for index, name in enumerate(('first_real', 'suggested', 'last_real')):
                    top = index * 40
                    children = [{'tag': 'a', 'attrs': {'href': f'/{name}/', 'top': top + 4, 'height': 20}, 'text': name},
                                {'tag': 'button', 'attrs': {'top': top + 4, 'height': 20}, 'text': 'Follow'}]
                    if name == 'suggested':
                        children.append({'tag': 'span', 'attrs': {'top': top + 20, 'height': 16},
                                         'text': 'Suggested for you'})
                    rows.append({'attrs': {'top': top, 'height': 40, 'clientHeight': 40, 'overflowY': overflow},
                                 'children': children})
                result = subprocess.run(['node', str(Path(__file__).parent / 'fixtures/following_rows_dom.mjs')],
                    input=json.dumps({'script': RELATION_ROWS_SCRIPT, 'fixtures': [{'children': rows}]}),
                    capture_output=True, encoding='utf-8', timeout=5, check=True)
                snapshot = json.loads(result.stdout)[0]
                self.assertEqual(['/first_real/', '/last_real/'], snapshot['hrefs'])
                self.assertFalse(snapshot['recommendations_reached'])

    def test_preloaded_offscreen_link_is_not_a_visible_row(self):
        script = _RELATION_VISIBLE + r"""
          globalThis.getComputedStyle = node => node.style;
          const viewport = {
            parentElement: null,
            style: {display:'block', visibility:'visible', overflowY:'auto', overflowX:'visible'},
            getBoundingClientRect: () => ({left:0, right:300, top:0, bottom:100, width:300, height:100})
          };
          const link = (top, bottom) => ({
            parentElement: viewport,
            style: {display:'block', visibility:'visible', overflowY:'visible', overflowX:'visible'},
            getBoundingClientRect: () => ({left:5, right:105, top, bottom, width:100, height:bottom-top})
          });
          if (!visibleRow(link(20,40))) throw Error('onscreen row missing');
          if (!visibleRow(link(78,102))) throw Error('row with visible center missing');
          if (visibleRow(link(90,110))) throw Error('row with clipped center admitted');
          if (visibleRow(link(120,140))) throw Error('offscreen row admitted');
          if (visibleRow(link(-30,-10))) throw Error('above-viewport row admitted');
        """
        result = subprocess.run(['node', '-e', script], capture_output=True, encoding="utf-8", timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)


class RelationDOMR51Tests(unittest.IsolatedAsyncioTestCase):
    async def test_clipped_inline_recommendation_cannot_hide_later_real_account(self):
        for overflow in ('hidden', 'clip'):
            with self.subTest(overflow=overflow):
                result = await self.inspect(document([
                    account_row('first_real'), account_row('suggested', recommendation=True),
                    account_row('last_real'),
                ], css=f'.row{{overflow-y:{overflow}}}'))
                self.assertEqual({'first_real', 'last_real'}, self.names(result))
                self.assertFalse(result['recommendations_reached'])

    async def test_avatar_and_username_keep_the_same_row_position_at_clip_boundary(self):
        await self.page.set_content(document([
            account_row(f'person{i}', metadata='<span>Followed by <a href="/mutual/">mutual</a></span>')
            for i in range(8)
        ], css='a{display:contents}'))
        await self.page.locator('#viewport').evaluate('el => el.scrollTop = 180')
        before = await self.page.locator('#surface').evaluate(RELATION_ROWS_SCRIPT)
        await self.page.locator('#viewport').evaluate('el => el.scrollTop = 296')
        after = await self.page.locator('#surface').evaluate(RELATION_ROWS_SCRIPT)
        before_y = next(row['y'] for row in before['positioned_rows'] if row['username'] == 'person6')
        after_y = next(row['y'] for row in after['positioned_rows'] if row['username'] == 'person6')
        self.assertAlmostEqual(116, before_y - after_y, delta=1,
            msg='switching between a username and avatar must not move the account anchor')

    async def test_virtualized_290_rows_preserve_neighbours_and_collect_each_identity_once(self):
        await self.collect_virtualized_source(290)

    async def test_screenshot_sized_virtualized_sources_preserve_every_visible_account(self):
        for total in (537, 369):
            with self.subTest(total=total):
                await self.collect_virtualized_source(total)

    async def collect_virtualized_source(self, total):
        from app.playwright_worker import PlaywrightWorker
        await self.page.set_content(document([], before=f'<div id="spacer" style="height:{total * 72}px;position:relative"></div>'))
        await self.page.evaluate("""total => {
          const viewport = document.querySelector('#viewport'), spacer = document.querySelector('#spacer');
          let timer;
          const render = () => {
            const first = Math.max(0, Math.floor(viewport.scrollTop / 72) - 1);
            spacer.innerHTML = Array.from({length: Math.min(8, total - first)}, (_, offset) => {
              const i = first + offset;
              return `<div class="row" style="position:absolute;top:${i * 72}px;width:100%"><a href="/person${i}/">person${i}</a><button>Follow</button></div>`;
            }).join('');
          };
          viewport.addEventListener('scroll', () => {clearTimeout(timer); timer = setTimeout(render, 25)});
          render();
        }""", total)
        worker = PlaywrightWorker(None)
        worker.page = self.page
        worker.collection_poll_interval_seconds = .005
        worker.collection_loading_grace_seconds = 2
        worker.collection_settled_idle_rounds = 2
        worker._guard = AsyncMock()
        worker._relation_surface_failure = AsyncMock(return_value=None)
        worker._has_visible_relation_loading_indicator = AsyncMock(return_value=False)
        saved, submitted = set(), []
        async def sink(batch):
            saved.update(batch); submitted.extend(batch)
            return len(saved)
        await worker._read_visible_account_dialog(self.page.locator('#surface'), None,
            surface_kind='followers', candidate_sink=sink, expected_minimum=total)
        self.assertEqual({f'person{i}' for i in range(total)}, saved)
        self.assertEqual(total, len(submitted))
        self.assertGreater(worker.last_relation_scroll['movements'], 50)

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

    async def inspect(self, content):
        await self.page.set_content(content)
        return await self.page.locator('#surface').evaluate(RELATION_ROWS_SCRIPT)

    def names(self, snapshot):
        return {href.strip('/') for href in snapshot['hrefs']}

    async def test_screenshot_footer_and_plain_mutual_text_keep_real_accounts(self):
        result = await self.inspect(document([
            account_row(f'person{i}', metadata='<span>Followed by avulims</span>')
            for i in range(4)
        ], after='<div>查看所有推荐用户</div>'))
        self.assertEqual({f'person{i}' for i in range(4)}, self.names(result))
        self.assertFalse(result['recommendations_reached'])

    async def test_visible_display_contents_profile_links_are_read_and_scrollable(self):
        result = await self.inspect(document([
            account_row(f'person{i}') for i in range(8)
        ], css='a {display:contents}'))
        # Only the first four rows fit the 280 px viewport. The rest must be
        # discovered after scrolling, even when their anchors use display:contents.
        seen = self.names(result)
        self.assertEqual({f'person{i}' for i in range(4)}, seen)
        surface = self.page.locator('#surface')
        reset = await surface.evaluate(relation_scroll_script('reset'))
        self.assertTrue(reset['valid'])
        self.assertEqual(0, reset['top'])
        movement = await surface.evaluate(relation_scroll_script('advance'))
        self.assertTrue(movement['moved'])
        self.assertFalse(movement['bottom'])
        self.assertEqual(182, movement['top'])
        for _ in range(4):
            seen.update(self.names(await surface.evaluate(RELATION_ROWS_SCRIPT)))
            if movement['bottom']:
                break
            movement = await surface.evaluate(relation_scroll_script('advance'))
            self.assertTrue(movement['valid'])
            self.assertTrue(movement['moved'])
        self.assertTrue(movement['bottom'])
        self.assertEqual({f'person{i}' for i in range(8)}, seen)

    async def test_display_contents_direct_text_is_read_without_an_avatar(self):
        result = await self.inspect(document([account_row('plain', avatar=False)], css='a{display:contents}'))
        self.assertEqual({'plain'}, self.names(result))

    async def test_display_contents_hidden_children_are_not_source_accounts(self):
        result = await self.inspect(document([
            account_row('real'), account_row('hidden'), account_row('concealed')
        ], css='a{display:contents} .row:nth-child(2){display:none} .row:nth-child(3){visibility:hidden}'))
        self.assertEqual({'real'}, self.names(result))

    async def test_mutual_account_link_is_metadata_not_a_source_candidate(self):
        result = await self.inspect(document([
            account_row(f'person{i}', metadata='<span>Followed by <a href="/avulims/">avulims</a></span>')
            for i in range(4)
        ], after='<div>查看所有推荐用户</div>'))
        self.assertEqual({f'person{i}' for i in range(4)}, self.names(result))
        self.assertFalse(result['recommendations_reached'])

    async def test_interleaved_recommendation_with_mutual_links_cannot_hide_later_real_row(self):
        result = await self.inspect(document([
            account_row('real0'),
            account_row('suggestion', recommendation=True,
                        metadata='<span>Followed by <a href="/avulims/">avulims</a></span>'),
            account_row('real1'),
        ]))
        self.assertEqual({'real0', 'real1'}, self.names(result))
        self.assertFalse(result['recommendations_reached'])

    async def test_explicit_suggestion_section_and_mutual_accounts_stay_excluded(self):
        result = await self.inspect(document([
            account_row('suggestion0', metadata='<span>Followed by <a href="/avulims/">avulims</a></span>'),
            account_row('suggestion1'),
        ], before='<h3>Suggested for you</h3>', after='<div>查看所有推荐用户</div>'))
        self.assertEqual(set(), self.names(result))
        self.assertTrue(result['recommendations_reached'])

    async def test_mutual_username_still_collected_when_it_is_its_own_source_row(self):
        result = await self.inspect(document([
            account_row('real', metadata='<span>Followed by <a href="/avulims/">avulims</a></span>'),
            account_row('avulims'),
        ]))
        self.assertEqual({'real', 'avulims'}, self.names(result))

    async def test_mutual_text_does_not_turn_display_name_into_a_global_rule(self):
        result = await self.inspect(document([
            account_row('real').replace('Display name', 'Followed by a dream'),
            account_row('followed_by'),
        ]))
        self.assertEqual({'real', 'followed_by'}, self.names(result))

    async def test_localized_multiple_mutual_links_are_not_multiple_candidates(self):
        for prefix in ('Followed by ', 'Seguido por ', 'Suivi par ', 'Abonniert von ', '关注者包括'):
            with self.subTest(prefix=prefix):
                result = await self.inspect(document([account_row('real', metadata=(
                    f'<span>{prefix}<a href="/mutual1/">mutual1</a>, '
                    '<a href="/mutual2/">mutual2</a></span>'
                ))]))
                self.assertEqual({'real'}, self.names(result))

    async def test_read_diagnostics_are_counts_without_profile_or_page_text(self):
        result = await self.inspect(document([
            account_row('real', metadata='<span>Followed by <a href="/mutual/">mutual</a></span>'),
            account_row('suggestion', recommendation=True),
        ]))
        self.assertEqual({
            'profile_links': 5, 'visible_profile_links': 5, 'context_links': 1,
            'candidate_accounts': 1, 'recommended_accounts': 1,
        }, result['diagnostics'])

    async def test_complete_worker_read_with_contents_links_commits_all_candidates(self):
        from app.playwright_worker import PlaywrightWorker
        await self.page.set_content(document([
            account_row(f'person{i}', metadata='<span>Followed by <a href="/mutual/">mutual</a></span>')
            for i in range(8)
        ], css='a{display:contents}'))
        surface = self.page.locator('#surface')
        self.assertTrue((await surface.evaluate(relation_scroll_script('reset')))['valid'])
        worker = PlaywrightWorker(None)
        worker.page = self.page
        worker.collection_poll_interval_seconds = .01
        # Keep the loading grace above the production 1.5 s virtual-row settle
        # interval, so this integration case exercises the real scroll sequence.
        worker.collection_loading_grace_seconds = 2
        worker.collection_settled_idle_rounds = 2
        worker._guard = AsyncMock()
        worker._relation_surface_failure = AsyncMock(return_value=None)
        worker._has_visible_relation_loading_indicator = AsyncMock(return_value=False)
        saved = set()
        batches = []
        async def sink(batch):
            batches.append(batch)
            saved.update(batch)
            return len(saved)
        await worker._read_visible_account_dialog(
            surface, None, surface_kind='followers', candidate_sink=sink,
            expected_minimum=8,
        )
        self.assertEqual({f'person{i}' for i in range(8)}, saved)
        self.assertGreaterEqual(len(batches), 2)
        self.assertGreaterEqual(worker.last_relation_scroll['movements'], 2)


if __name__ == '__main__':
    unittest.main()
