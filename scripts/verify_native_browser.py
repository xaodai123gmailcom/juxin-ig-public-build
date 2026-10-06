"""Real-browser release gate: isolated/persistent cookies and localStorage.
Only a local HTTP fixture is opened. No Instagram account is accessed.
"""
import asyncio
import argparse
import json
import inspect
import os
import shutil
import sys
import tempfile
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from browser_sandbox_permissions import ensure_browser_sandbox_access
from diagnose_native_startup import bounded_log, windows_events
from app.database import Database
from app.service import CoreService
from app.native_browser import NativeBrowser
from app.playwright_worker import PlaywrightWorker
from app.bitbrowser_v2 import BitBrowserClientV2
from app.native_browser import BrowserHub
from app.browser_runtime import select_browser_runtime, pinned_chromium, bundled_executable, bundled_chromium, installed_chrome
from browser_build_policy import requirement_from_report, save_json

NETWORK_CHECK_TIMEOUT_SECONDS = 15.0
VERIFICATION_TIMEOUT_SECONDS = 180.0


def verification_headless(requested=None, *, windows=None):
    # Windows installers must exercise the same visible Chrome launch as the
    # product. Headless remains an explicit diagnostic/CI option.
    if requested is not None:return requested
    return not (os.name == 'nt' if windows is None else windows)


def fixture_browser(db,root,headless):
    options={'headless':verification_headless(headless)}
    # REPAIR_NATIVE_BROWSER can target a previous complete project. Do not
    # replace the real launch failure with an unsupported constructor argument.
    if 'startup_file_logging' in inspect.signature(NativeBrowser).parameters:
        options['startup_file_logging']=True
    return NativeBrowser(db,root,**options)

class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body=b'<html><body>Local browser isolation fixture</body></html>'
        self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass

async def verify_network_page(page,url):
    async def probe():
        response=await page.goto(url)
        assert response and response.status==200, 'Local HTTP fixture did not load successfully'
        assert await page.locator('body').inner_text()=='Local browser isolation fixture', 'Browser displayed an error page'
        fetched=await page.evaluate("async () => { const response = await fetch(location.href, {cache:'no-store'}); return {status:response.status, text:await response.text()}; }")
        assert fetched['status']==200 and 'Local browser isolation fixture' in fetched['text'], 'Browser network service failed the local fetch check'
    # CDP evaluation has no implicit response deadline. A network service that
    # stays alive but never answers fetch must not leave the build hanging.
    try:
        await asyncio.wait_for(probe(), timeout=NETWORK_CHECK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as error:
        raise RuntimeError(f'Local browser page/network check timed out after {NETWORK_CHECK_TIMEOUT_SECONDS:g} seconds') from error


async def verify_with_timeout(root,url,*,headless=None):
    # Covers cookie/localStorage CDP requests and reconnect stages as well.
    # wait_for allows verify's finally block to release its fixture windows;
    # cleanup completes before repair may replace browser files.
    try:
        return await asyncio.wait_for(verify(root,url,headless=headless), timeout=VERIFICATION_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as error:
        raise RuntimeError(f'Native browser verification timed out after {VERIFICATION_TIMEOUT_SECONDS:g} seconds; fixture cleanup completed') from error

async def verify(root,url,*,headless=None):
    db=Database(root/'test.sqlite3');db.initialize();service=CoreService(db)
    owner=service.register_user('native-runtime-check','local-runtime-check-password')['id']
    browser=fixture_browser(db,root,headless)
    # Hub must support the real worker's protected CDP relay without BitBrowser running.
    hub=BrowserHub(browser,BitBrowserClientV2('http://127.0.0.1:54345'))
    with db.write() as c:
        first=browser.create(c,owner,'One','Verification','');second=browser.create(c,owner,'Two','Verification','')
    workers=[]
    try:
        for ident in (first,second):
            w=PlaywrightWorker(hub);workers.append(w);await w.connect(ident)
            await verify_network_page(w.page,url)
        a,b=workers
        assert browser.directory(first)!=browser.directory(second)
        assert a._connected_endpoint!=b._connected_endpoint
        await a.page.evaluate("document.cookie='account=one; max-age=86400; path=/';localStorage.setItem('account','one')")
        assert 'account=one' in await a.page.evaluate('document.cookie'), 'Fixture cookie was not accepted before close'
        assert await b.page.evaluate('document.cookie')==''
        assert await b.page.evaluate("localStorage.getItem('account')") is None
        await b.page.evaluate("document.cookie='account=two; max-age=86400; path=/';localStorage.setItem('account','two')")
        await a.disconnect();await asyncio.to_thread(browser.close_profile,first)
        # Only locally generated fixture profiles are inspected in this gate.
        close_report=browser.directory(first)/'juxin-startup.json'
        print('Native fixture close: '+close_report.read_text(encoding='utf-8'),flush=True)
        await a.connect(first);await a.page.goto(url)
        cookie_preserved='account=one' in await a.page.evaluate('document.cookie')
        storage_preserved=await a.page.evaluate("localStorage.getItem('account')")=='one'
        print('Native fixture persistence: '+json.dumps({'cookie':cookie_preserved,'localStorage':storage_preserved}),flush=True)
        assert cookie_preserved, 'Persistent cookie missing after reopening the same browser profile'
        assert await a.page.evaluate("localStorage.getItem('account')")=='one'
        assert 'account=two' in await b.page.evaluate('document.cookie')
        async with a._destructive_action_lease():
            from app.errors import ConflictError
            try:await asyncio.to_thread(browser.close_profile,first)
            except ConflictError:pass
            else:raise AssertionError('Window closed during current action')
        actual_versions = [worker._browser.version for worker in workers]
        return {'actual_browser_versions': actual_versions, 'network_verified': True,
                'isolation_verified': True, 'persistence_verified': True, 'ownership_verified': True}
    finally:
        try:
            for worker in workers:
                try:await worker.disconnect()
                except Exception:traceback.print_exc()
        finally:
            try:await asyncio.to_thread(browser.shutdown)
            finally:hub.legacy.shutdown()


def collect_fixture_diagnostics(root, run, *, started_utc, executable=None):
    """Copy only generated localhost fixture logs, not profiles/account data.

    Fixtures live outside installer-output to avoid Windows path limits. Merely
    listing their paths in result.json leaves the repair ZIP without evidence.
    A failed read is recorded per file and cannot replace the launch exception.
    """
    names=('juxin-startup.json','juxin-startup.stderr.log','juxin-startup.chrome.log')
    records=[];pids=[]
    base=root.resolve()
    for profile in sorted((root/'browser-profiles').glob('*/*')):
        if not profile.is_dir() or profile.is_symlink() or not profile.resolve().is_relative_to(base):
            continue
        target=run/'launches'/profile.parent.name/profile.name
        target.mkdir(parents=True,exist_ok=True)
        for name in names:
            source=profile/name
            entry={'file':str(source.relative_to(root))}
            try:
                if source.is_symlink() or not source.resolve().is_relative_to(base):
                    entry['error']='linked diagnostic file omitted'
                else:
                    entry.update(bounded_log(source,target/name))
                    if name=='juxin-startup.json' and entry.get('exists') and not entry.get('truncated'):
                        payload=json.loads((target/name).read_text(encoding='utf-8'))
                        if type(payload.get('pid')) is int:pids.append(payload['pid'])
            except (OSError,ValueError) as error:entry['error']=str(error)
            records.append(entry)
    result={'files':records,'fixture_only':True}
    if executable:
        events=windows_events(started_utc,Path(executable),pids)
        (run/'windows-events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf-8')
    (run/'diagnostic-files.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--headless',action=argparse.BooleanOptionalAction,default=None)
    engine=parser.add_mutually_exclusive_group()
    engine.add_argument('--require-bundled',action='store_true',
                        help='Verify the exact packaged Chromium even when system Chrome is installed')
    engine.add_argument('--require-installed-chrome',action='store_true',
                        help='Require installed Chrome; never substitute the bundled engine')
    parser.add_argument('--policy-output',type=Path)
    parser.add_argument('--diagnostics-dir',type=Path,default=Path(__file__).resolve().parents[1]/'installer-output'/'native-browser-diagnostics')
    options=parser.parse_args(argv)
    if options.policy_output:
        # Never leave a previous successful dependency seal after a failed run.
        options.policy_output.unlink(missing_ok=True)
    options.diagnostics_dir.mkdir(parents=True,exist_ok=True)
    # Keep failed fixtures and bounded launch logs for diagnosis. These profiles
    # contain only localhost test data, not real app accounts or user cookies.
    run=Path(tempfile.mkdtemp(prefix='run-',dir=options.diagnostics_dir)).resolve()
    # Keep Chrome's mutable profile outside the deeply nested source checkout.
    # Chrome/LevelDB still contains MAX_PATH-limited Windows file operations;
    # the source path plus owner/profile UUIDs exceeded that limit in CI and
    # made cookies/localStorage silently remain memory-only. Preserve the
    # Unicode fixture and all isolation/persistence assertions in a short temp
    # directory, as installed apps keep user data outside the install tree.
    fixture=Path(tempfile.mkdtemp(prefix='juxin-native-')).resolve()
    root=fixture/'窗口 验证 (1)';root.mkdir()
    headless=verification_headless(options.headless)
    report={'status':'running','headless':headless,'data_dir':str(root),'require_bundled':options.require_bundled,
            'require_installed_chrome':options.require_installed_chrome,
            'started_utc':datetime.now(timezone.utc).isoformat(),
            'fixture_file_logging_supported':'startup_file_logging' in inspect.signature(NativeBrowser).parameters}
    success=False;server=None
    previous_override=os.environ.get('IGAC_NATIVE_BROWSER_EXECUTABLE')
    try:
        if options.require_installed_chrome:
            if os.name != 'nt' or installed_chrome() is None:
                raise RuntimeError('The installed-Chrome build requires Google Chrome on Windows')
            os.environ.pop('IGAC_NATIVE_BROWSER_EXECUTABLE',None)
        if options.require_bundled:
            browser_root=os.environ.get('IGAC_BROWSER_DIR')
            if not browser_root:
                raise RuntimeError('IGAC_BROWSER_DIR is required to verify the packaged browser')
            bundled_path=bundled_executable(browser_root).resolve()
            report['sandbox_permissions']=ensure_browser_sandbox_access(bundled_path)
            # Applies only to this build-check process and its new local fixture
            # profiles. Installed application selection rules are unchanged.
            os.environ['IGAC_NATIVE_BROWSER_EXECUTABLE']=str(bundled_path)
        driver=pinned_chromium()
        revision,version=(bundled_chromium(os.environ['IGAC_BROWSER_DIR'], driver)
                          if os.environ.get('IGAC_BROWSER_DIR') and not options.require_installed_chrome else driver)
        report.update(driver_revision=driver[0], driver_browser_version=driver[1])
        report.update(bundled_revision=revision,bundled_version=version,
                      selected_runtime=select_browser_runtime())
        if options.require_installed_chrome and report['selected_runtime']['source']!='installed-chrome':
            raise RuntimeError('Installed-Chrome verification selected the wrong browser')
        if options.require_bundled:
            selected=report['selected_runtime']
            if (Path(selected['executable']).resolve()!=bundled_path
                    or selected['version'] is not None and selected['version']!=version):
                raise RuntimeError('Release gate did not select the exact pinned bundled browser')
        print('Native Chromium verification: '+json.dumps(report,ensure_ascii=True),flush=True)
        server=ThreadingHTTPServer(('127.0.0.1',0),Page)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        report.update(asyncio.run(verify_with_timeout(root,f'http://127.0.0.1:{server.server_port}/',headless=headless)))
        selected_version = version if options.require_bundled else report['selected_runtime']['version']
        if selected_version and any(version != selected_version for version in report['actual_browser_versions']):
            raise RuntimeError('Browser version changed during verification; rerun with a stable browser installation')
        print('PASS: two real Chromium processes, separate CDP endpoints, cookies/storage isolated, persistent after reopen, close blocked during action')
        report['status']='passed'
        if options.policy_output:
            policy=requirement_from_report(report,installed=options.require_installed_chrome)
            save_json(options.policy_output,policy)
        success=True
    except BaseException as error:
        report.update(status='failed',error=str(error),details=getattr(error,'details',{}))
        (run/'traceback.txt').write_text(traceback.format_exc(),encoding='utf-8')
        print('Native browser failed. Diagnostics: '+json.dumps(str(run),ensure_ascii=True),flush=True)
        try:
            report['diagnostics']=collect_fixture_diagnostics(root,run,started_utc=report['started_utc'],
                executable=report.get('selected_runtime',{}).get('executable'))
        except Exception as diagnostic_error:
            report['diagnostic_error']=str(diagnostic_error)
        raise
    finally:
        if options.require_bundled or options.require_installed_chrome:
            if previous_override is None:os.environ.pop('IGAC_NATIVE_BROWSER_EXECUTABLE',None)
            else:os.environ['IGAC_NATIVE_BROWSER_EXECUTABLE']=previous_override
        if server:server.shutdown();server.server_close()
        if success:
            try:
                shutil.rmtree(fixture)
            except OSError as error:
                # Only the disposable localhost fixture is retained. Browser
                # shutdown/ownership errors above still fail verification.
                report['cleanup_warning'] = str(error)
                print(f'Native verification passed; local test fixture retained at {fixture}: {error}', flush=True)
        (run/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
