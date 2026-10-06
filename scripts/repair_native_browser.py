"""Verify the packaged engine, then attempt one transactional same-version repair and one bounded compatibility recovery.

Used inside the build mutex, or by REPAIR_NATIVE_BROWSER.bat holding that same
mutex. Only generated build/browsers files and localhost test profiles are used.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
import platform
from pathlib import Path
import shutil
import sys
import tempfile
import traceback
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

from diagnose_native_startup import resolve_project
from diagnose_native_startup import probe, installed_comparison_browser, windows_events
from browser_sandbox_permissions import ensure_browser_sandbox_access


@contextmanager
def runtime_environment(root):
    values = {'IGAC_BROWSER_DIR': str(root), 'PLAYWRIGHT_BROWSERS_PATH': str(root),
              'IGAC_NATIVE_BROWSER_EXECUTABLE': None}
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Tee:
    def __init__(self, original, log):
        self.original, self.log = original, log

    def write(self, value):
        self.log.write(value)
        self.log.flush()
        self.original.write(value)
        self.original.flush()
        return len(value)

    def flush(self):
        self.log.flush()
        self.original.flush()


def verify_runtime(project, browser_root, output):
    # The verifier belongs to this repair tool. Application modules/dependencies
    # belong to the selected original project, not the tool's extraction folder.
    sys.path.insert(0, str(project / 'backend'))
    path = Path(__file__).with_name('verify_native_browser.py')
    spec = importlib.util.spec_from_file_location('juxin_repair_verification', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with runtime_environment(browser_root):
        module.main(['--no-headless', '--require-bundled', '--diagnostics-dir', str(output)])
    results = list(output.glob('run-*/result.json'))
    if len(results) != 1:
        raise RuntimeError('Browser verifier did not produce exactly one result')
    result = json.loads(results[0].read_text(encoding='utf-8'))
    if result.get('status') != 'passed' or result.get('require_bundled') is not True:
        raise RuntimeError('The exact packaged browser did not pass verification')
    versions = result.get('actual_browser_versions')
    if not isinstance(versions, list) or len(versions) != 2 or any(v != result.get('bundled_version') for v in versions):
        raise RuntimeError('The two verified browsers do not match the pinned version')
    if result.get('network_verified') is not True:
        raise RuntimeError('Browser HTTP loading and JavaScript network requests were not verified')
    return result


def runtime_fingerprint(root):
    from app.browser_runtime import bundled_executable
    try:
        executable = bundled_executable(root)
        return {'executable': str(executable), 'sha256': hashlib.sha256(executable.read_bytes()).hexdigest()}
    except Exception as error:
        return {'error': str(error)}


def prepare_runtime_permissions(root):
    if os.name != 'nt':
        return {'status': 'not_windows'}
    from app.browser_runtime import bundled_executable
    from app.errors import UpstreamUnavailableError
    try:
        executable = bundled_executable(root)
    except UpstreamUnavailableError as error:
        if error.details.get('reason') in {'native_runtime_missing', 'native_runtime_incomplete'}:
            return {'status': 'runtime_missing_or_incomplete', 'reason': error.details['reason']}
        raise
    return ensure_browser_sandbox_access(executable)


def filesystem_corruption(error):
    """Read structured Windows errors, including permission-wrapper causes.

    Do not infer corruption from translated text or treat access denial as a
    download problem. 1392/1393 are file/directory and disk-structure corruption.
    """
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        details = getattr(error, 'details', {})
        code = getattr(error, 'winerror', None)
        if code is None and isinstance(details, dict):code = details.get('winerror')
        if type(code) is int and code in {1392, 1393}:return code
        # Only explicit causes wrap the current operation. An implicit context
        # may merely be the earlier failure being repaired; following it would
        # mislabel a new network/access error as old filesystem corruption.
        error = error.__cause__
    return None


def recover(project, run, *, verifier=verify_runtime, installer=None, preparer=None):
    root = project / 'build' / 'browsers'
    preparer = prepare_runtime_permissions if preparer is None else preparer
    result = {'status': 'checking', 'repair_attempts': 0, 'compatibility_attempts': 0, 'checks': [], 'permissions': []}

    def check(label):
        print('[CHECK] ' + label + ': exact bundled browser, two isolated windows and persistent data', flush=True)
        try:
            verified = verifier(project, root, run / label)
        except Exception as error:
            details=getattr(error,'details',{})
            details={key:value for key,value in details.items()
                     if key in {'reason','exit_code','winerror','errno','startup_log','stderr_log'}
                     and isinstance(value,(str,int,type(None)))} if isinstance(details,dict) else {}
            result['checks'].append({'name': label, 'status': 'failed', 'error': str(error),
                                     'details': details,
                                     'winerror': filesystem_corruption(error)})
            raise
        result['checks'].append({'name': label, 'status': 'passed', 'result': verified})

    try:
        # Access denial must be addressed before deciding that the binary needs
        # replacing. A failed permission update is not fixed by downloading.
        print('[CHECK] Prepare sandbox read/execute access for the bundled runtime', flush=True)
        corrupt_preparation = None
        try:
            result['permissions'].append(preparer(root))
        except Exception as error:
            code = filesystem_corruption(error)
            if code is None:raise
            # Inspection can fail before Chrome is launched when generated files
            # are corrupt. Permit one transactional replacement, then enforce the
            # same sandbox preparation and full verifier on the replacement.
            corrupt_preparation = error
            result['permissions'].append({'status': 'corrupt_runtime', 'winerror': code, 'error': str(error)})
        try:
            if corrupt_preparation is not None:
                result['checks'].append({'name': 'before-repair', 'status': 'failed',
                    'stage': 'runtime-inspection', 'error': str(corrupt_preparation),
                    'winerror': filesystem_corruption(corrupt_preparation)})
                raise corrupt_preparation
            check('before-repair')
        except Exception as initial_error:
            print('[REPAIR] Current packaged browser failed. Downloading the selected version once from Google.', flush=True)
            result['repair_attempts'] = 1
            if installer is None:
                from install_native_browser import main as installer

            def install_and_check(label, **options):
                verified = False
                def verify_replacement(destination):
                    nonlocal verified
                    result['permissions'].append(preparer(root))
                    check(label)
                    verified = True
                installer(browser_root=root, verify=verify_replacement, **options)
                if not verified:
                    raise RuntimeError('Downloaded browser was not verified; repair cannot be accepted')

            def startup_failure(error):
                details = getattr(error, 'details', {})
                return isinstance(details, dict) and details.get('reason') in {
                    'native_browser_exited', 'native_browser_timeout'}

            try:
                install_and_check('after-repair')
                result['status'] = 'repaired'
            except Exception as repaired_error:
                # A freshly extracted verified archive that exits at startup may
                # need compatibility recovery even if the original executable
                # was too corrupt to launch. Repeated corruption, download,
                # permission, persistence and rollback errors must still stop.
                if not (startup_failure(repaired_error) and
                        (startup_failure(initial_error) or filesystem_corruption(initial_error))):
                    raise
                result['compatibility_attempts'] = 1
                print('[REPAIR] Fresh replacement still fails at native startup. Trying one newer official Stable browser; full verification is mandatory.', flush=True)
                install_and_check('after-compatibility', compatibility=True)
                result['status'] = 'compatibility_repaired'
        else:
            result['status'] = 'already_verified'
    except Exception as error:
        result.update(status='failed', error=str(error))
        code = filesystem_corruption(error)
        if code is not None:
            result.update(failure_kind='filesystem_corruption', winerror=code,
                next_action='Windows cannot read the generated browser files. Extract the complete source into a new directory on another healthy local drive and rebuild. If this persists, check the affected filesystem. Account data is not modified by this repair.')
            print('[FAILED] ' + result['next_action'], flush=True)
        traceback.print_exc()
    return result


def collect_failure_comparison(project, run):
    """Bounded local-only probes after failed recovery; never a release pass.

    Test the rolled-back exact runtime and one standard installed browser using
    disposable profiles. No downloads, browser-file mutation or account login.
    """
    from app.browser_runtime import bundled_executable
    output=run/'startup-comparison';output.mkdir(parents=True,exist_ok=True)
    result={'purpose':'diagnosis only; does not satisfy the release gate',
            'started_utc':datetime.now(timezone.utc).isoformat(),
            'system':{'os':platform.platform(),'machine':platform.machine(),
                      'python_bits':64 if sys.maxsize>2**32 else 32},'cases':[]}
    work=None
    try:
        executable=bundled_executable(project/'build/browsers')
        result['runtime']=runtime_fingerprint(project/'build/browsers')
        work=Path(tempfile.mkdtemp(prefix='jx-')).resolve()
        cases=[('short-data-path',executable,work/'p',False),
               ('minimal-settings',executable,work/'minimal',True)]
        comparison=installed_comparison_browser()
        if comparison:cases.append(('installed-browser-comparison',comparison,work/'installed',False))
        else:result['installed_comparison']='not_found'
        for label,target,profile,minimal in cases:
            print('[DIAGNOSE] '+label+'; fresh local-only profile',flush=True)
            try:
                measured=probe(target,profile,output/label,minimal=minimal,timeout=20)
                result['cases'].append({'name':label,**measured})
                if measured.get('cleanup_error'):
                    result['remaining_cases']='skipped because an owned diagnostic child did not close'
                    break
            except Exception as error:
                result['cases'].append({'name':label,'status':'diagnostic_error','error':str(error)})
        events={}
        for target in dict.fromkeys(case[1] for case in cases):
            pids=[case['pid'] for case in result['cases']
                  if case.get('pid') and case.get('args',[None])[0]==str(target)]
            if pids:events[str(target)]=windows_events(result['started_utc'],target,pids)
        (output/'windows-events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf-8')
        result['status']='collected'
    except Exception as error:
        result.update(status='diagnostic_error',error=str(error))
    finally:
        if work:
            # If a child could not be closed, preserve its fixture for diagnosis
            # rather than deleting data underneath a live owned browser.
            if any(case.get('cleanup_error') for case in result['cases']):
                result['retained_fixture']=str(work)
            else:
                try:shutil.rmtree(work)
                except OSError as error:result['cleanup_warning']=str(error)
        (output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def make_repair_bundle(run,destination):
    # Include bounded copies of fixture logs, never the actual Chrome profiles.
    names={'summary.json','result.json','traceback.txt','repair.log','diagnostic-files.json',
           'windows-events.json','juxin-startup.json','juxin-startup.stderr.log',
           'juxin-startup.chrome.log','chrome.log','console.log'}
    with ZipFile(destination,'w',ZIP_DEFLATED) as archive:
        for path in sorted(run.rglob('*')):
            if (path.is_file() and not path.is_symlink() and path.name in names
                    and path.resolve().is_relative_to(run.resolve())):
                archive.write(path,path.relative_to(run).as_posix())


def main(project):
    project = resolve_project(project)
    sys.path.insert(0, str(project / 'backend'))
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6]
    output = project / 'installer-output'
    run = output / ('native-repair-' + stamp)
    run.mkdir(parents=True)
    result = {'status': 'failed', 'project': str(project), 'before': runtime_fingerprint(project / 'build/browsers')}
    with (run / 'repair.log').open('w', encoding='utf-8') as log:
        with redirect_stdout(Tee(sys.stdout, log)), redirect_stderr(Tee(sys.stderr, log)):
            try:
                result.update(recover(project, run))
            except Exception as error:
                result['error'] = str(error)
                traceback.print_exc()
            finally:
                result['after'] = runtime_fingerprint(project / 'build/browsers')
                if result['status']=='failed' and any(
                    item.get('details',{}).get('reason') in {'native_browser_exited','native_browser_timeout'}
                    for item in result.get('checks',[])):
                    # Diagnostic failure must never mask the original failure
                    # or convert a CDP-only comparison into a release success.
                    try:result['startup_comparison']=collect_failure_comparison(project,run)
                    except Exception as error:result['comparison_error']=str(error)
                (run / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    destination = output / ('native-browser-repair-' + stamp + '.zip')
    make_repair_bundle(run,destination)
    passed = result['status'] in {'already_verified', 'repaired', 'compatibility_repaired'}
    print('\nBUNDLED_BROWSER_REPAIR=' + ('PASS' if passed else 'FAIL'), flush=True)
    print('修复与验证报告：' + str(destination), flush=True)
    print('此结果只针对浏览器启动、隔离与持久化检查，不代表整个安装包已构建完成。', flush=True)
    return 0 if passed else 1


def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-dir-env', action='store_true')
    args = parser.parse_args()
    project = os.environ.get('JUXIN_DIAGNOSTIC_PROJECT_DIR') if args.project_dir_env else str(Path(__file__).resolve().parents[1])
    if not project or not project.strip():
        parser.error('未传入原项目目录，请重新运行 REPAIR_NATIVE_BROWSER.bat')
    if os.name != 'nt':
        parser.error('此修复入口仅用于 Windows 原项目')
    return main(Path(project))


if __name__ == '__main__':
    sys.exit(cli())
