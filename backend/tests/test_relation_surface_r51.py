"""Real Chromium relation-surface selection; every request is a local fixture."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError

STYLE = '<style>[role=dialog],main{width:440px;min-height:100px;padding:8px} .list{width:400px;height:90px;overflow-y:auto} .row{height:44px}</style>'
def dialog(title='Followers', *, identity='followers', hidden=False, rows=True):
    return (f'<section role="dialog" id="{identity}"'+(' hidden' if hidden else '')+'>'
            f'<h2>{title}</h2><input placeholder="Search"><div class="list">'+
            ('<div class="row"><a href="/one/">one</a></div><div class="row"><a href="/two/">two</a></div>' if rows else '')+
            '</div></section>')
def page_html(body='', *, relation='followers', click=None):
    click = click or "event.preventDefault();document.getElementById('followers').hidden=false"
    return '<!doctype html><meta charset="utf-8">'+STYLE+f'<main><header><a id="trigger" href="/source/{relation}/" onclick="{click}">710 {relation}</a></header></main>'+body

class RelationSurfaceR51Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.p = await async_playwright().start()
        self.addAsyncCleanup(self.p.stop)
        path = os.environ.get('IGAC_TEST_CHROMIUM_EXECUTABLE', '')
        if not path and not Path(self.p.chromium.executable_path).is_file():
            if os.environ.get('IGAC_REQUIRE_COLLECTION_BROWSER') == '1':
                self.fail('Required Chromium runtime is missing')
            self.skipTest('Chromium required for relation surface fixtures')
        self.browser = await self.p.chromium.launch(**({'executable_path':path} if path else {'channel':'chromium'}), headless=True, args=['--no-sandbox'])
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        self.html = page_html(dialog(hidden=True))
        self.requests = []
        async def fixture(route):
            self.requests.append(route.request.url)
            await route.fulfill(content_type='text/html', body=self.html)
        await self.context.route('**/*', fixture)
        self.page = await self.context.new_page()
        await self.page.goto('https://www.instagram.com/source/')
        self.worker = PlaywrightWorker(None)
        self.worker.page = self.page
        self.worker._guard = AsyncMock()
        self.worker.relation_surface_wait_seconds = .8

    async def show(self, html, url='https://www.instagram.com/source/'):
        self.html = html
        await self.page.goto(url)
        self.requests.clear()

    async def test_existing_relation_does_not_click_a_covered_profile_header(self):
        await self.show(page_html(dialog()))
        self.worker._find_relation_trigger=AsyncMock(side_effect=AssertionError('must not reopen visible relation'))
        surface=await self.worker._open_relation_surface('source','followers')
        self.assertEqual('followers',await surface.get_attribute('id'))
        self.worker._find_relation_trigger.assert_not_called()
        self.assertEqual([],self.requests)

    async def test_visible_relation_is_selected_before_unrelated_tail_dialog(self):
        await self.show(page_html(dialog(hidden=True)+dialog('Notifications', identity='unrelated')))
        surface = await self.worker._open_relation_surface('source', 'followers')
        self.assertEqual('followers', await surface.get_attribute('id'))
        self.assertEqual([], self.requests, 'A visible valid list must not be reloaded')

    async def test_hidden_tail_dialog_does_not_wait_or_reload_visible_list(self):
        await self.show(page_html(dialog(hidden=True)+dialog('Notifications', identity='hidden', hidden=True)))
        surface = await asyncio.wait_for(self.worker._open_relation_surface('source', 'followers'), 2)
        self.assertEqual('followers', await surface.get_attribute('id'))
        self.assertEqual([], self.requests)
        self.assertEqual(1, self.worker.last_relation_surface_diagnostics['visible_dialog_count'])

    async def test_nested_dialog_selects_relation_content_not_outer_wrapper(self):
        await self.show(page_html('<div role="dialog" id="wrapper">'+dialog(hidden=True)+'</div>'))
        surface = await self.worker._open_relation_surface('source', 'followers')
        self.assertEqual('followers', await surface.get_attribute('id'))

    async def test_locator_remains_bound_when_another_dialog_is_appended(self):
        surface = await self.worker._open_relation_surface('source', 'followers')
        await self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div role=dialog id=later><h2>Settings</h2></div>')")
        self.assertEqual('followers', await surface.get_attribute('id'))
        await self.page.locator('#followers').evaluate('(node) => node.remove()')
        self.assertEqual(0, await surface.count(), 'A removed surface must not retarget an unrelated dialog')

    async def test_delayed_relation_appears_without_fallback_navigation(self):
        html=page_html(dialog(hidden=True), click="event.preventDefault();setTimeout(()=>document.getElementById('followers').hidden=false,150)")
        await self.show(html)
        surface = await self.worker._open_relation_surface('source', 'followers')
        self.assertEqual('followers', await surface.get_attribute('id'))
        self.assertEqual([], self.requests)
        self.assertGreater(self.worker.last_relation_surface_diagnostics['polls'], 1)

    async def test_following_and_translated_titles_are_scoped_to_requested_relation(self):
        for relation, title in [('followers','粉丝'),('followers','ผู้ติดตาม'),('followers','フォロワー'),('following','关注'),('following','กำลังติดตาม'),('following','Following')]:
            with self.subTest(relation=relation, title=title):
                await self.show(page_html(dialog(title, hidden=True)+dialog('Followers' if relation=='following' else 'Following', identity='wrong'), relation=relation))
                surface = await self.worker._open_relation_surface('source', relation)
                self.assertEqual('followers', await surface.get_attribute('id'))
                self.assertEqual([], self.requests)

    async def test_relation_aria_label_and_loading_shell_are_recognized(self):
        await self.show(STYLE+'<div role="dialog" id="shell" aria-label="Followers"><div role="progressbar">Loading</div></div>')
        surface = await self.worker._select_relation_surface('source', 'followers')
        self.assertEqual('shell', await surface.get_attribute('id'))

    async def test_localized_empty_list_remains_a_valid_relation_surface(self):
        for title, empty in [('粉丝','尚无粉丝'),('ผู้ติดตาม','ยังไม่มีผู้ติดตาม'),('フォロワー','フォロワーはいません')]:
            with self.subTest(title=title):
                await self.show('<!doctype html><meta charset="utf-8">'+STYLE+f'<div role="dialog" id="empty"><h2>{title}</h2><p>{empty}</p></div>')
                surface=await self.worker._select_relation_surface('source','followers')
                self.assertEqual('empty',await surface.get_attribute('id'))

    async def test_hidden_ancestor_relation_is_not_selected(self):
        await self.show(STYLE+'<div aria-hidden="true">'+dialog()+'</div>')
        self.assertIsNone(await self.worker._select_relation_surface('source','followers'))

    async def test_unrelated_dialog_with_account_rows_is_rejected(self):
        await self.show(STYLE+dialog('Notifications', identity='unrelated'))
        self.assertIsNone(await self.worker._select_relation_surface('source','followers'))

    async def test_account_named_followers_is_not_a_relation_title(self):
        await self.show(STYLE+'<div role="dialog"><h2>Notifications</h2><div class="list"><a href="/followers/"><span>Followers</span></a></div></div>')
        self.assertIsNone(await self.worker._select_relation_surface('source','followers'))

    async def test_display_contents_account_named_followers_cannot_name_notifications_modal(self):
        for contents in ['Followers', '<span>Followers</span>']:
            with self.subTest(contents=contents):
                await self.show(STYLE+'<div role="dialog" id="notifications"><h2>Notifications</h2><input>'
                    '<div class="list"><div><a href="/followers/" style="display:contents">'+contents+'</a></div></div></div>')
                self.assertIsNone(await self.worker._select_relation_surface('source','followers'))

    async def test_valid_relation_selects_and_reads_all_display_contents_accounts(self):
        from app.collection_surface import RELATION_ROWS_SCRIPT
        await self.show(STYLE+'<div role="dialog" id="contents-list"><h2>Followers</h2><input>'
            '<div class="list"><div class="row"><a href="/one/" style="display:contents">one</a></div>'
            '<div class="row"><a href="/two/" style="display:contents"><span>two</span></a></div></div></div>')
        surface=await self.worker._select_relation_surface('source','followers')
        self.assertEqual('contents-list',await surface.get_attribute('id'))
        self.assertEqual(['/one/','/two/'],(await surface.evaluate(RELATION_ROWS_SCRIPT))['hrefs'])
        self.assertEqual(1,self.worker.last_relation_surface_diagnostics['row_surface_count'])

    async def test_two_independent_matching_dialogs_wait_for_unambiguous_surface(self):
        await self.show(STYLE+dialog(identity='old')+dialog(identity='current'))
        self.assertIsNone(await self.worker._select_relation_surface('source','followers'))
        self.assertEqual(2,self.worker.last_relation_surface_diagnostics['ambiguous_surface_count'])
        await self.page.evaluate("setTimeout(()=>document.getElementById('old').hidden=true,150)")
        surface=await self.worker._wait_for_relation_surface('source','followers')
        self.assertEqual('current',await surface.get_attribute('id'))
        self.assertEqual([],self.requests)

    async def test_same_title_on_wrong_profile_or_host_is_rejected(self):
        for url in ['https://www.instagram.com/other/', 'https://example.test/source/', 'https://www.instagram.com/source/followers_backup/']:
            with self.subTest(url=url):
                await self.show(STYLE+dialog(), url)
                self.assertIsNone(await self.worker._select_relation_surface('source','followers',include_main=True))

    async def test_standalone_main_requires_exact_relation_route_and_list_evidence(self):
        await self.show(STYLE+'<main id="relation"><h2>Followers</h2><input><div class="list"><a href="/one/">one</a></div></main>', 'https://www.instagram.com/source/followers/?variant=1')
        surface=await self.worker._select_relation_surface('source','followers',include_main=True)
        self.assertEqual('relation',await surface.get_attribute('id'))
        await self.show(STYLE+'<main><h2>Profile</h2><p>Profile is ready</p></main>', 'https://www.instagram.com/source/followers/')
        self.assertIsNone(await self.worker._select_relation_surface('source','followers',include_main=True))

    async def test_unlabelled_main_variant_requires_search_rows_and_scroll(self):
        await self.show(STYLE+'<main id="relation"><input><div class="list"><a href="/one/">one</a></div></main>', 'https://www.instagram.com/source/followers/')
        surface=await self.worker._select_relation_surface('source','followers',include_main=True)
        self.assertEqual('relation',await surface.get_attribute('id'))
        await self.page.locator('input').evaluate('(node)=>node.remove()')
        self.assertIsNone(await self.worker._select_relation_surface('source','followers',include_main=True))

    async def test_failure_diagnostics_are_bounded_counts_without_account_content(self):
        await self.show(STYLE+'<main><header>Profile loaded</header></main>'+dialog('Notifications',identity='secret-account'))
        self.worker._find_relation_trigger=AsyncMock(return_value=None)
        self.worker._page_surface_failure=AsyncMock(return_value=None)
        self.worker.relation_surface_wait_seconds=.15
        with self.assertRaises(WorkerExecutionError) as raised:
            await self.worker._open_relation_surface('source','followers')
        self.assertEqual('instagram_followers_list_not_rendered',raised.exception.code)
        diagnostics=raised.exception.details['surface_diagnostics']
        self.assertEqual(1,diagnostics['visible_dialog_count'])
        self.assertLessEqual(diagnostics['polls'],3)
        self.assertNotIn('secret-account',json.dumps(diagnostics))
        self.assertNotIn('/one/',json.dumps(diagnostics))

    async def test_header_trigger_accepts_exact_absolute_url_but_ignores_dialog_copy(self):
        await self.show(STYLE+'<div role="dialog"><a id="stale" href="/source/followers/">Followers</a></div><main><header><a id="correct" href="https://www.instagram.com/source/followers/?a=1">Followers</a></header></main>')
        trigger=await self.worker._find_relation_trigger('source','followers')
        self.assertEqual('correct',await trigger.get_attribute('id'))

    async def test_text_trigger_ignores_whole_stats_container(self):
        await self.show(STYLE+'<main><header><span id="whole">165 posts 710 followers <button id="following">461 following</button></span></header></main>')
        trigger=await self.worker._find_relation_trigger('source','following')
        self.assertEqual('following',await trigger.get_attribute('id'))

if __name__ == '__main__':
    unittest.main()
