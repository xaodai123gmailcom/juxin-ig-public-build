"""Real original task adapter + desktop bridge integration on local fixtures."""
import asyncio,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.service import CoreService
from app.account_workspace import AccountWorkspace
from app.embedded_browser import EmbeddedBrowser
from app.native_browser import BrowserHub
from app.bitbrowser_api import BitBrowserClient
from app.playwright_worker import PlaywrightWorker
from app.account_platforms import PLATFORMS
from unittest.mock import patch

async def main():
    with tempfile.TemporaryDirectory() as root:
        db=Database(Path(root)/'test.sqlite');db.initialize();service=CoreService(db)
        owner=service.register_user('embedded-task','correct horse battery staple')['id']
        native=EmbeddedBrowser(db,root);hub=BrowserHub(native,BitBrowserClient('http://127.0.0.1:59999',''))
        accounts=AccountWorkspace(service,hub)
        plan=accounts.save(owner,{'name':'内置任务验证','native':True});row=accounts.get(owner,plan['id']);profile=row['profile_id']
        worker=PlaywrightWorker(hub)
        lease=service.acquire_browser_lease(owner,profile,operation_type='collection',entity_id='local-fixture')
        try:
            await worker.connect(profile,open_if_needed=True)
            await worker.page.goto(os.environ['JUXIN_FIXTURE_URL']+'/python-worker')
            await worker.page.locator('#action').click()
            assert await worker.page.locator('#result').text_content()=='collected'
            await worker.page.evaluate("localStorage.setItem('worker','same-session')")
            await worker.disconnect()
        finally:
            await worker.disconnect();service.release_browser_lease(profile,lease)
        await worker.connect(profile,open_if_needed=False)
        assert await worker.page.evaluate("localStorage.getItem('worker')")=='same-session'
        await worker.disconnect()
        # Exercise actual account toolbar recovery through Core -> Playwright
        # -> the per-account desktop bridge. Only local fixture URLs are used.
        recovered=accounts.save(owner,{'name':'空白页恢复验证','native':True})
        recovered_id=recovered['id'];recovered_profile=accounts.get(owner,recovered_id)['profile_id']
        with patch.dict(PLATFORMS['instagram'],url=os.environ['JUXIN_FIXTURE_URL']+'/preview-denied'):
            denied=await asyncio.to_thread(accounts.command,owner,{'id':recovered_id,'action':'home'})
        assert denied['page_loaded'] is False and denied['http_status']==403, denied
        assert recovered_profile not in accounts.snapshot(owner)['locks']
        with patch.dict(PLATFORMS['instagram'],url=os.environ['JUXIN_FIXTURE_URL']+'/recovered-home'):
            recovered=await asyncio.to_thread(accounts.command,owner,{'id':recovered_id,'action':'home'})
        assert recovered['page_loaded'] is True, recovered
        assert recovered_profile not in accounts.snapshot(owner)['locks']
        native.close_profile(recovered_profile)
        print('PASS original account toolbar recovers blank and HTTP error pages with released management leases')
        native.close_profile(profile)
        print('PASS original Python PlaywrightWorker, strict relay, tickets, task lease and same-session reconnect')
asyncio.run(main())
