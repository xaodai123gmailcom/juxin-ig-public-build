"""Standalone Reels route compatibility; no network or real account actions."""
import unittest
import json
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, patch

import test_standalone_nurture_r6 as fixtures
from app.errors import ConflictError, ValidationError
from app.standalone_nurture import StandaloneNurture, REEL


class StandaloneReelsRoutesR62Tests(unittest.IsolatedAsyncioTestCase):
    def browser(self):
        return fixtures.PreparationAndClockTests().make_browser([
            {'key':'/reel/Ab_12-x','source':'blob:a','state':'unliked'}])[0]

    async def test_canonical_plural_reel_redirect_finishes_startup_after_profile(self):
        b=self.browser()
        original=b.page.goto.side_effect
        async def goto(url, **kwargs):
            await original(url,**kwargs)
            if url.endswith('/reels/'):
                b.page.url='https://www.instagram.com/reels/Ab_12-x/?igsh=fixture'
        b.page.goto.side_effect=goto
        n=StandaloneNurture(b)
        with patch('app.instagram_home.prepare_instagram_home',new=AsyncMock()), \
             patch('app.standalone_nurture.asyncio.sleep',new=AsyncMock()):
            await n.prepare()
        b.account_snapshot.assert_awaited_once()
        b.nurture_ready.assert_awaited_once()
        self.assertEqual('ok',b.account_snapshot.await_args.args[0]['status'])

    async def test_feed_root_and_both_shortcode_routes_are_valid(self):
        b=self.browser();n=StandaloneNurture(b)
        for path in ('/reels','/reels/','/reel/Ab_12-x','/reel/Ab_12-x/',
                     '/reels/Ab_12-x','/reels/Ab_12-x/?utm_source=fixture'):
            for host in ('www.instagram.com','instagram.com'):
                with self.subTest(path=path,host=host):
                    b.page.url='https://'+host+path
                    self.assertEqual('/reel/Ab_12-x',(await n.current())['key'])

    async def test_other_routes_and_foreign_hosts_never_probe_video(self):
        b=self.browser();n=StandaloneNurture(b)
        for url in ('https://www.instagram.com/','https://www.instagram.com/owner/',
                    'https://www.instagram.com/p/Ab_12-x/','https://www.instagram.com/reel/',
                    'https://www.instagram.com/reels/Ab_12-x/comments/',
                    'https://www.instagram.com/reels/%2fowner/',
                    'https://instagram.com.evil.example/reels/Ab_12-x/',
                    'https://evil.example/reels/Ab_12-x/',
                    'http://www.instagram.com/reels/Ab_12-x/'):
            with self.subTest(url=url):
                b.page.url=url;b.page.evaluate.reset_mock()
                with self.assertRaises(ValidationError):await n.current()
                b.page.evaluate.assert_not_awaited()

    async def test_wrong_actor_and_lost_lease_stop_before_reel_probe(self):
        for failure in ('actor','lease','username','unknown'):
            with self.subTest(failure=failure):
                b=self.browser();b.page.url='https://www.instagram.com/reels/Ab_12-x/'
                n=StandaloneNurture(b);n.identity={'username':'owner','instagram_user_id':'123'}
                if failure=='actor':b.page.context.cookies.return_value=[{'name':'ds_user_id','value':'456'}]
                if failure=='lease':b.guard.side_effect=ConflictError('lease lost')
                if failure=='username':b.page.evaluate.side_effect=lambda *_:'someone_else'
                if failure=='unknown':b.page.context.cookies.return_value=[]
                with self.assertRaises((ValidationError,ConflictError)):await n.current()
                self.assertFalse(any(c.args[0]==REEL for c in b.page.evaluate.await_args_list))


class StandaloneReelDOMRoutesR62Tests(unittest.TestCase):
    def test_permalink_aliases_normalize_without_broadening_like_identity(self):
        script=Path(__file__).parent/'fixtures'/'standalone_reels_routes_r62.cjs'
        result=subprocess.run(['node',str(script)],input=REEL,text=True,capture_output=True,check=True)
        self.assertEqual({'passed':15,'synthetic_dom':True,'native_browser':False},json.loads(result.stdout))
