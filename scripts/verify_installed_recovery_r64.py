"""Actual installed Windows desktop + frozen Core, authenticated R6.4 recovery.

The release CLI has no source-mode switch. Source tests exercise the exact API
scenario against a separate normal-lifespan Core process and an offline bridge.
The desktop gate uses only the real main renderer's existing preload API; it
never imports candidate application code, changes production methods or opens
an Instagram account. It seeds a fresh legacy DB before launching either EXE.
"""
from __future__ import annotations
import argparse
from contextlib import closing, contextmanager
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


def load_sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


fixture = load_sibling('recovery_upgrade_fixture_r64')
core_probe = load_sibling('verify_frozen_core_service')
require = fixture.require
SOURCE_ROOT = Path(__file__).resolve().parents[1]
PROOF_FILES = ('scripts/verify_installed_recovery_r64.py', 'scripts/recovery_upgrade_fixture_r64.py',
    'scripts/nurture_cleanup_upgrade_fixture.py', 'scripts/retired_posting_archive_oracle.py',
    'scripts/fixtures/nurture_cleanup_upgrade_r62.sql')


def source_binding():
    commit = os.environ.get('GITHUB_SHA', '')
    require(re.fullmatch('[a-f0-9]{40}', commit), 'Exact GITHUB_SHA is required for installed release proof')
    require(os.path.lexists(SOURCE_ROOT / 'CI_SOURCE_PROVENANCE.json'), 'Explicit flat-Git CI source marker is required')
    provenance = load_sibling('ci_source_binding').verify_ci_source_binding(SOURCE_ROOT)
    require(provenance['source_commit'] == commit, 'Installed proof must match the actual Git HEAD')
    return {'source_commit': commit, 'source_provenance': provenance, 'source_manifest_sha256': fixture.legacy.file_sha256(SOURCE_ROOT / 'SOURCE_SHA256.json'),
        'proof_files_sha256': {name: fixture.legacy.file_sha256(SOURCE_ROOT / name) for name in PROOF_FILES}}


FLAGS = ('authenticated_api', 'unauthenticated_refused', 'foreign_owner_refused',
    'posting_endpoints_unavailable', 'legacy_archive_verified', 'idle_association_retired',
    'active_owner_preserved',
    'hidden_task_absent_from_list', 'exact_hidden_task_located', 'stale_stop_refused',
    'normal_safe_stop', 'safe_collection_cleanup', 'material_history_dedup_preserved', 'no_share')


# The desktop waits up to 180s for its first frozen Core start before creating
# the window. Keep that allowance distinct from renderer/preload/API evidence.
INSTALLED_PHASE_SECONDS = {'debugger': 30, 'renderer': 210, 'preload': 15,
    'readiness': 30, 'api': 60, 'shutdown': 60}
INSTALLED_TIMEOUT_SECONDS = 420  # 405s of phase caps plus 15s bookkeeping.


class InstalledRecoveryDebuggerTimeout(RuntimeError): pass
class InstalledRecoveryRendererTimeout(RuntimeError): pass
class InstalledRecoveryRendererAmbiguous(RuntimeError): pass
class InstalledRecoveryPreloadTimeout(RuntimeError): pass
class InstalledRecoveryReadinessTimeout(RuntimeError): pass
class InstalledRecoveryApiTimeout(RuntimeError): pass
class InstalledRecoveryShutdownTimeout(RuntimeError): pass
class InstalledRecoveryProcessExited(RuntimeError): pass
class InstalledRecoveryTimeout(RuntimeError): pass


def require_owned_process(process):
    if process.poll() is not None:
        raise InstalledRecoveryProcessExited('Owned installed desktop exited before proof completion')


@contextmanager
def installed_phase(process, end, seconds, error_type, *, cleanup):
    """Bound even synchronous CDP/evaluate calls, which have no API timeout."""
    require(math.isfinite(end) and math.isfinite(seconds) and seconds > 0,
        'Installed phase deadline must be finite and positive')
    now = time.monotonic()
    deadline = min(end, now + seconds)
    category = InstalledRecoveryTimeout if end < now + seconds else error_type
    if deadline <= now:
        raise category('Installed recovery phase deadline expired')
    expired = threading.Event()
    gate = threading.Lock()
    armed = True
    def terminate():
        with gate:
            if not armed:
                return
            expired.set()
        try: cleanup()
        except Exception: pass  # The enclosing finally retries owned cleanup.
    watchdog = threading.Timer(deadline - now, terminate)
    watchdog.daemon = True; watchdog.start()
    try:
        yield deadline
        if expired.is_set() or time.monotonic() >= deadline:
            raise category('Installed recovery phase deadline expired')
    except Exception:
        if expired.is_set():
            raise category('Installed recovery phase deadline expired') from None
        if error_type is not InstalledRecoveryShutdownTimeout:
            require_owned_process(process)
        raise
    finally:
        with gate:
            armed = False
        watchdog.cancel()
        # cancel() alone cannot stop an already-entered Timer callback. Join it
        # before another phase can begin; recheck expiry after that boundary.
        watchdog.join(timeout=45)
        if watchdog.is_alive() or expired.is_set() or time.monotonic() >= deadline:
            raise category('Installed recovery phase deadline expired') from None


def environment():
    env = {key: value for key, value in os.environ.items()
        if not key.upper().startswith(('IGAC_', 'COLLECTOR_', 'PLAYWRIGHT_', 'PYTHON', 'OPENAI_', 'PEXELS_', 'ELECTRON_'))
        and key.upper() not in {'OPENVINO_LIB_PATHS', '__PYVENV_LAUNCHER__'}}
    env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    return env


def reserve_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class HttpApi:
    def __init__(self, url, startup):
        self.url, self.startup, self.session = url, startup, None
        self.opener = build_opener(ProxyHandler({}))

    def __call__(self, path, body=None):
        headers = {'X-Startup-Token': self.startup}
        if self.session: headers['Authorization'] = 'Bearer ' + self.session
        if body is not None: headers['Content-Type'] = 'application/json'
        request = Request(self.url + path, headers=headers, method='GET' if body is None else 'POST',
            data=None if body is None else json.dumps(body).encode())
        try:
            with self.opener.open(request, timeout=10) as response:
                result = json.load(response)
                if path == '/api/session/resume': self.session = result['session_token']
                return {'ok': True, 'body': result}
        except HTTPError as error:
            try: result = json.loads(error.read())
            finally: error.close()
            return {'ok': False, 'status': error.code, 'error': result}


def exercise(api, directory, manifest, *, timeout=60):
    """All recovery transitions use authenticated APIs. SQLite stays read-only."""
    require(math.isfinite(timeout) and timeout > 0, 'API deadline must be finite and positive')
    deadline = time.monotonic() + timeout
    proof = {key: False for key in FLAGS}
    responses = []
    def request(path, body=None, status=200, refusal=None):
        require(time.monotonic() < deadline, 'Installed recovery API proof timed out')
        value = api(path, body)
        if status == 200:
            require(value.get('ok') is True, 'API failed: ' + path + ' ' + str(value))
        else:
            observed = value.get('status') == status
            # The production desktop intentionally exposes domain error text,
            # not HTTP status. Require the exact known refusal there; source
            # HTTP tests independently check the transport status as well.
            ipc_refusal = value.get('transport') == 'desktop-ipc' and refusal and re.search(refusal, value.get('error', ''))
            require(value.get('ok') is False and (observed or ipc_refusal), 'Expected guarded refusal: ' + path + ' ' + str(value))
        responses.append({'path': path, 'action': body.get('action') if body else None, 'expected_status': status, 'observed_status': (None if value.get('transport') == 'desktop-ipc' else 200) if value.get('ok') else value.get('status'), 'transport': value.get('transport', 'http')})
        return value.get('body')
    def studio(action, name, **values):
        return {'action': action, 'job_id': fixture.JOBS[name], **values}
    def resume(owner):
        result = request('/api/session/resume', {'session_token': manifest['tokens'][owner]})
        require(result['user']['id'] == owner, 'Authenticated identity mismatch')
    def wait_for(predicate, label):
        while not predicate():
            require(time.monotonic() < deadline, label + ' timed out')
            time.sleep(.05)
    request('/api/studio/snapshot', status=401, refusal='Missing application session token')
    proof['unauthenticated_refused'] = True
    resume(fixture.OTHER)
    request('/api/studio/command', studio('locate_cleanup_collection', 'collection'), status=404, refusal='任务不存在')
    resume(fixture.OWNER)
    proof['authenticated_api'] = proof['foreign_owner_refused'] = True
    startup = fixture.inspect(directory, manifest, phase='startup')
    proof['legacy_archive_verified'] = proof['idle_association_retired'] = proof['active_owner_preserved'] = True
    for endpoint in core_probe.REMOVED_POSTING_ENDPOINTS:
        body = None if endpoint['method'] == 'GET' else {'action': 'start', 'job_ids': [fixture.POST]}
        # The desktop allowlist must reject these paths before HTTP. The frozen
        # Core gate independently requires actual authenticated HTTP 404s.
        request(endpoint['path'], body, status=404, refusal='不允许访问该本机接口|Not Found')
    proof['posting_endpoints_unavailable'] = True
    listing = request('/api/workbench/snapshot?limit=1&history_limit=1&platform=instagram')
    require(not any(task['id'] == fixture.TASK for task in listing['tasks']), 'Dismissed historical task leaked back into the task list')
    proof['hidden_task_absent_from_list'] = True
    request('/api/studio/command', studio('control', 'collection', operation='retry_cleanup'), status=409, refusal='窗口仍有关联的采集任务')
    found = request('/api/studio/command', studio('locate_cleanup_collection', 'collection'))
    blocker = found['blocker']
    require(found['profile_id'] == fixture.PROFILES['collection'] and blocker['task_id'] == fixture.TASK and blocker['status'] == 'paused' and blocker['version'] == 7 and blocker['window_ids'] == [fixture.PROFILES['collection']] and blocker['dismissed'] is True and blocker['can_stop'] is True, 'Locator did not identify the exact hidden paused task/version/window')
    proof['exact_hidden_task_located'] = True
    stop = studio('stop_cleanup_collection', 'collection', task_id=fixture.TASK, version=blocker['version'])
    request('/api/studio/command', {**stop, 'version': blocker['version'] - 1}, status=409, refusal='关联任务已变化，请重新定位后再确认')
    request('/api/studio/command', {**stop, 'task_id': 'wrong-target'}, status=409, refusal='关联任务已变化，请重新定位后再确认')
    request('/api/studio/command', {**stop, 'version': True}, status=422, refusal='请先定位关联任务，再确认停止当前版本')
    fixture.inspect(directory, manifest, phase='startup')
    proof['stale_stop_refused'] = True
    result = request('/api/studio/command', stop)
    require(result['task_id'] == fixture.TASK and result['status'] == 'stopped' and result['cleanup_pending'] is True, 'Safe stop did not use normal task lifecycle')
    fixture.inspect(directory, manifest, phase='stopped')
    proof['normal_safe_stop'] = True
    require(request('/api/studio/command', studio('locate_cleanup_collection', 'collection'))['blocker'] is None, 'Stopped task remains an unfinished blocker')
    clean = request('/api/studio/command', studio('control', 'collection', operation='retry_cleanup'))
    require(clean.get('cleanup_reconciled') is True, 'Hidden collection cleanup did not reconcile')
    proof['safe_collection_cleanup'] = True
    final = fixture.inspect(directory, manifest, phase='complete')
    proof['material_history_dedup_preserved'] = proof['no_share'] = True
    require(not any(task['id'] == fixture.TASK for task in request('/api/workbench/snapshot?limit=1&history_limit=1&platform=instagram')['tasks']), 'Stop restored a dismissed task')
    return {'checks': proof, 'startup': startup, 'persisted_state': final, 'api_calls': responses,
        'located_task': blocker, 'safety_fence': 'posting-feature-removed-no-execution-or-owner-theft'}


def exact_renderer(browser, app_root, *, check_preload=True):
    """Select only the packaged main renderer, never first tab/IG account page."""
    from urllib.parse import unquote, urlparse
    candidates = []
    for context in browser.contexts:
        for page in context.pages:
            parsed = urlparse(page.url)
            path = unquote(parsed.path).replace('\\', '/').lower()
            expected = str(app_root).replace('\\', '/').lower().rstrip('/') + '/resources/app.asar/renderer/dist/index.html'
            if parsed.scheme == 'file' and not parsed.netloc and not parsed.query and path.lstrip('/') == expected.lstrip('/'):
                candidates.append(page)
    if len(candidates) > 1:
        raise InstalledRecoveryRendererAmbiguous('Multiple packaged main renderer targets')
    if not candidates:
        require(not check_preload, 'Expected exactly one packaged main renderer target')
        return None
    if check_preload:
        require(candidates[0].evaluate("typeof window.collectorCore?.request") == 'function', 'Main preload API is missing')
    return candidates[0]


def dispatch_browser_events(browser, deadline):
    """A bounded Playwright wait dispatches page AND navigation notifications.

    Reading contexts/pages/url followed by time.sleep only reads cached state.
    Waiting on the default owned context does not inspect or select any tab.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    require(bool(browser.contexts), 'Owned CDP browser has no default context')
    remaining_ms = (deadline - time.monotonic()) * 1000
    if remaining_ms <= 0:
        return
    try:
        browser.contexts[0].wait_for_event('page', timeout=min(100, remaining_ms))
    except PlaywrightTimeoutError:
        pass  # Expected short polling tick; all other protocol errors fail.


def wait_installed_renderer(browser, app_root, process, deadline):
    while time.monotonic() < deadline:
        require_owned_process(process)
        page = exact_renderer(browser, app_root, check_preload=False)
        if page is not None:
            return page
        dispatch_browser_events(browser, deadline)
    require_owned_process(process)
    raise InstalledRecoveryRendererTimeout('Packaged main renderer target was not observed before deadline')


def wait_installed_preload(browser, app_root, process, page, deadline):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    expected_url = page.url.split('#', 1)[0]
    require_owned_process(process)
    try:
        page.wait_for_function("url => location.href.split('#', 1)[0] === url && typeof window.collectorCore?.request === 'function'",
            arg=expected_url, timeout=max(1, (deadline - time.monotonic()) * 1000))
    except PlaywrightTimeoutError:
        require_owned_process(process)
        raise InstalledRecoveryPreloadTimeout('Packaged renderer preload API was not observed before deadline') from None
    require_owned_process(process)
    require(exact_renderer(browser, app_root) is page and page.url.split('#', 1)[0] == expected_url,
        'Packaged main renderer changed while waiting for preload')
    return expected_url


def installed_core_process(desktop_pid, executable):
    # Query only the child process tree we launched. Do not inspect command
    # lines, tokens, unrelated user processes, or select by executable basename.
    command = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Get-CimInstance Win32_Process -Filter \"ParentProcessId = " + str(desktop_pid) + "\" | Select-Object ProcessId,ParentProcessId,ExecutablePath | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], check=True, capture_output=True, text=True, encoding='utf-8', timeout=15)
    rows = json.loads(result.stdout or '[]'); rows = rows if isinstance(rows, list) else [rows]
    same = lambda a, b: os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    matches = [row for row in rows if row.get('ExecutablePath') and same(row['ExecutablePath'], executable)]
    require(len(matches) == 1, 'The launched desktop does not own exactly one selected installed Core')
    return {'pid': matches[0]['ProcessId'], 'parent_pid': desktop_pid, 'executable': str(executable), 'sha256': fixture.legacy.file_sha256(executable)}


def stop_owned_runtime(process, core=None):
    """Failure cleanup targets only our Popen tree and an identity-checked child."""
    if process.poll() is None:
        try:
            subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'], check=True, capture_output=True, timeout=15)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            process.kill()
        process.wait(timeout=10)
    if core is not None:
        require(type(core.get('pid')) is int and core['pid'] > 0 and type(core.get('parent_pid')) is int and core.get('parent_pid') == process.pid, 'Refusing cleanup of an unowned Core')
        target = str(core['executable']).replace("'", "''")
        command = ("$ErrorActionPreference='Stop'; $p=Get-CimInstance Win32_Process -Filter \"ProcessId = "
            + str(core['pid']) + "\"; if($p){if($p.ParentProcessId -ne " + str(process.pid)
            + " -or $p.ExecutablePath -ine '" + target + "'){throw 'Owned Core identity changed'}; "
            + "Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop}")
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
            check=True, capture_output=True, timeout=15)


def validate_proof(proof, *, executable, core_executable, require_windows=True):
    require(isinstance(proof, dict) and proof.get('verified') is True and proof.get('contract') == fixture.CONTRACT, 'Recovery proof contract is missing')
    expected_keys = {'verified', 'contract', 'platform', 'synthetic', 'live_accounts_tested', 'user_data_touched',
        'desktop', 'core', 'core_frozen', 'core_upgrade_manifest_sha256', 'seed_pid', 'seed_completed_perf_ns',
        'launch_perf_ns', 'ready_perf_ns', 'chronology_clock', 'manifest_sha256', 'input_database_sha256', 'nonce',
        'normal_shutdown', 'manifest_unchanged', 'checks', 'startup', 'persisted_state', 'api_calls',
        'located_task', 'safety_fence', 'source_commit', 'source_manifest_sha256', 'proof_files_sha256', 'desktop_app_asar_sha256'}
    binding = source_binding()
    if 'source_provenance' in binding:
        expected_keys.add('source_provenance')
    require(set(proof) == expected_keys, 'Recovery proof fields are missing or unexpected')
    for key, expected in binding.items():
        require(proof.get(key) == expected, 'Recovery source/proof file binding changed: ' + key)
        if key == 'source_provenance':
            # JSON equality is type-strict here: Python's True == 1 must not
            # accept a changed schema or a false numeric provenance claim.
            require(json.dumps(proof[key], sort_keys=True, allow_nan=False) ==
                    json.dumps(expected, sort_keys=True, allow_nan=False),
                    'Recovery source/proof file binding changed: ' + key)
    require(proof.get('desktop_app_asar_sha256') == fixture.legacy.file_sha256(Path(executable).resolve().parent / 'resources/app.asar'), 'Recovery installed app.asar binding changed')
    require(proof.get('synthetic') is True and proof.get('live_accounts_tested') is False and proof.get('user_data_touched') is False, 'Recovery isolation evidence is missing')
    require(set(proof.get('checks', {})) == set(FLAGS) and all(proof['checks'][flag] is True for flag in FLAGS), 'Recovery API coverage is incomplete')
    for label, selected in (('desktop', executable), ('core', core_executable)):
        runtime = proof.get(label, {})
        require(os.path.normcase(os.path.abspath(runtime.get('executable', ''))) == os.path.normcase(str(Path(selected).resolve())) and runtime.get('sha256') == fixture.legacy.file_sha256(selected), 'Recovery proof selected executable changed: ' + label)
        require(type(runtime.get('pid')) is int and runtime['pid'] > 0, 'Recovery process PID is invalid')
    require(set(proof['desktop']) == {'pid', 'executable', 'sha256'} and set(proof['core']) == {'pid', 'parent_pid', 'executable', 'sha256'}, 'Recovery runtime identity fields changed')
    require(proof['core']['pid'] != proof['desktop']['pid'], 'Core and desktop must be distinct processes')
    require(type(proof['core'].get('parent_pid')) is int and proof['core'].get('parent_pid') == proof['desktop']['pid'], 'Core is not owned by the selected desktop')
    require(proof.get('platform') == 'win32' or not require_windows, 'Installed recovery requires Windows')
    require(proof.get('core_frozen') is True, 'Core frozen proof binding is missing')
    require(proof.get('chronology_clock') == fixture.legacy.CHRONOLOGY_CLOCK and all(type(proof.get(k)) is int for k in ('seed_completed_perf_ns', 'launch_perf_ns', 'ready_perf_ns')) and 0 < proof['seed_completed_perf_ns'] < proof['launch_perf_ns'] < proof['ready_perf_ns'], 'Recovery launch chronology is not strict QPC ordering')
    require(type(proof.get('seed_pid')) is int and proof['seed_pid'] not in (proof['desktop']['pid'], proof['core']['pid']), 'Seed was not persisted by a separate process')
    require(proof.get('normal_shutdown') is True and proof.get('manifest_unchanged') is True, 'Recovery shutdown/input evidence is incomplete')
    require(proof.get('safety_fence') == 'posting-feature-removed-no-execution-or-owner-theft', 'No-Share safety fence is missing')
    for section in ('startup', 'persisted_state'):
        state = proof.get(section, {})
        numbers = {'historical_receipts': 1, 'protected_tables': len(fixture.PROTECTED),
            'login_files': 8, 'retired_idle_associations': 1, 'quarantined_jobs': 1, 'preserved_active_leases': 1}
        require(state.get('verified') is True and state.get('no_submission') is True and
            all(type(state.get(key)) is int and state[key] == value for key, value in numbers.items()), 'Recovery persisted-state evidence is incomplete')
        archive = state.get('archive', {})
        require(archive.get('verified') is True and type(archive.get('archived_rows')) is int and archive['archived_rows'] == 5
            and type(archive.get('copied_files')) is int and archive['copied_files'] == 1
            and isinstance(archive.get('archive_rows_sha256'), str) and re.fullmatch('[a-f0-9]{64}', archive['archive_rows_sha256']), 'Recoverable archive evidence is incomplete')
    require(proof['startup']['archive'] == proof['persisted_state']['archive'], 'Retired archive changed during recovery')
    require(proof['startup']['protected_sha256'] == proof['persisted_state']['protected_sha256'] and proof['startup']['material_sha256'] == proof['persisted_state']['material_sha256'], 'Material/history/dedup hashes differ')
    for key in ('manifest_sha256', 'input_database_sha256', 'nonce', 'core_upgrade_manifest_sha256'):
        require(isinstance(proof.get(key), str) and re.fullmatch('[a-f0-9]{64}', proof[key]), 'Recovery input binding missing: ' + key)
    require(isinstance(proof['api_calls'], list) and proof['api_calls'] and all(isinstance(call, dict) and set(call) == {'path', 'action', 'expected_status', 'observed_status', 'transport'} and call['transport'] == 'desktop-ipc' and call['expected_status'] in {200,401,404,409,422} and call['observed_status'] is None for call in proof['api_calls']), 'Recovery authenticated API transcript is invalid')
    located = proof['located_task']
    require(isinstance(located, dict) and located.get('task_id') == fixture.TASK and located.get('version') == 7 and located.get('status') == 'paused' and located.get('dismissed') is True and located.get('can_stop') is True and located.get('window_ids') == [fixture.PROFILES['collection']], 'Exact hidden blocker evidence changed')
    return proof


def probe_installed(executable, core_executable, core_report, log_path, *, timeout=INSTALLED_TIMEOUT_SECONDS):
    require(sys.platform == 'win32', 'Actual installed-user proof requires Windows')
    require(math.isfinite(timeout) and timeout > 0, 'Recovery process deadline must be finite and positive')
    executable, core_executable = Path(executable).resolve(strict=True), Path(core_executable).resolve(strict=True)
    require(core_executable == executable.parent / 'resources/backend/collector_core/collector_core.exe', 'Core path is not the selected app installation')
    require((executable.parent / 'resources/app.asar').is_file(), 'Packaged desktop app.asar is missing')
    prior = json.loads(Path(core_report).read_text(encoding='utf-8-sig'))['nurture_cleanup_upgrade']
    require(prior.get('verified') is True and prior.get('persisted_state', {}).get('verified') is True and prior.get('runtime', {}).get('frozen') is True and prior['runtime'].get('windows') is True and os.path.normcase(prior['runtime'].get('executable', '')) == os.path.normcase(str(core_executable)) and prior['runtime'].get('executable_sha256') == fixture.legacy.file_sha256(core_executable), 'Prior strict R6.3 frozen upgrade proof does not bind this installed Core')
    binding = source_binding()
    from playwright.sync_api import sync_playwright
    log_path = Path(log_path).resolve(); log_path.parent.mkdir(parents=True, exist_ok=True)
    with core_probe.probe_directory(prefix='Juxin-InstalledRecoveryR64-') as temporary:
        root = Path(temporary)
        user_data = root / 'roaming/juxin-ig-audience-collector-newgen'
        data = user_data / 'data'
        manifest_path, manifest = fixture.seed(data)
        manifest_hash = fixture.legacy.file_sha256(manifest_path)
        env = environment()
        # Preserve the OS profile so dependencies see the same LocalAppData as
        # the Windows privacy gate. The preseeded --user-data-dir stays first
        # in desktop database selection; IGAC_DB_PATH independently pins Core.
        port, debug_port, blocked_proxy = reserve_port(), reserve_port(), reserve_port()
        env.update(COLLECTOR_CORE_PORT=str(port), IGAC_DB_PATH=str(data / 'collector.sqlite3'))
        # All browser web traffic is redirected to an unused loopback port. The
        # retired feature has no publishing runtime; archival is checked separately.
        args = [str(executable), '--user-data-dir=' + str(user_data), '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + str(debug_port), '--proxy-server=http://127.0.0.1:' + str(blocked_proxy), '--proxy-bypass-list=localhost;127.0.0.1', '--disable-background-networking']
        with log_path.open('wb') as log:
            launch_perf_ns = time.perf_counter_ns()
            process = subprocess.Popen(args, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
            end = time.monotonic() + timeout
            timed_out = threading.Event()
            runtime = None
            shutdown_complete = False
            failed = False
            cleanup_lock = threading.Lock()
            cleaned_identity = object()
            def cleanup_owned():
                nonlocal cleaned_identity
                if not cleanup_lock.acquire(timeout=45):
                    raise RuntimeError('Owned runtime cleanup remained busy')
                try:
                    identity = None if runtime is None else runtime['pid']
                    if cleaned_identity != identity:
                        stop_owned_runtime(process, runtime)
                        cleaned_identity = identity
                finally:
                    cleanup_lock.release()
            watchdog_gate = threading.Lock()
            watchdog_armed = True
            def terminate_owned_tree():
                with watchdog_gate:
                    if not watchdog_armed:
                        return
                    timed_out.set()
                try: cleanup_owned()
                except Exception: pass  # Timeout always fails; finally retries bounded cleanup.
            watchdog = threading.Timer(timeout, terminate_owned_tree)
            watchdog.daemon = True; watchdog.start()
            try:
                with sync_playwright() as playwright:
                    from playwright.sync_api import Error as PlaywrightError
                    with installed_phase(process, end, INSTALLED_PHASE_SECONDS['debugger'], InstalledRecoveryDebuggerTimeout, cleanup=cleanup_owned) as deadline:
                        browser = None
                        while browser is None:
                            require_owned_process(process)
                            if time.monotonic() >= deadline:
                                raise InstalledRecoveryDebuggerTimeout('Owned loopback debugger was not observed before deadline')
                            try:
                                browser = playwright.chromium.connect_over_cdp('http://127.0.0.1:' + str(debug_port),
                                    timeout=max(1, min(1000, (deadline - time.monotonic()) * 1000)))
                            except PlaywrightError:
                                require_owned_process(process)
                                time.sleep(min(.1, max(0, deadline - time.monotonic())))
                    with installed_phase(process, end, INSTALLED_PHASE_SECONDS['renderer'], InstalledRecoveryRendererTimeout, cleanup=cleanup_owned) as deadline:
                        page = wait_installed_renderer(browser, executable.parent, process, deadline)
                    with installed_phase(process, end, INSTALLED_PHASE_SECONDS['preload'], InstalledRecoveryPreloadTimeout, cleanup=cleanup_owned) as deadline:
                        renderer_url = wait_installed_preload(browser, executable.parent, process, page, deadline)
                    def api(path, body=None):
                        require_owned_process(process)
                        require(exact_renderer(browser, executable.parent, check_preload=False) is page,
                            'Packaged main renderer changed before API request')
                        value = page.evaluate("""async ({url,path,body}) => {
                            if (location.href.split('#', 1)[0] !== url || typeof window.collectorCore?.request !== 'function')
                                throw new Error('Packaged main renderer/preload changed');
                            try { return {ok:true,transport:'desktop-ipc',body:await window.collectorCore.request(path,body===null?{}:{method:'POST',body})}; }
                            catch(error) { const text=String(error);const match=text.match(/(?:请求失败[:：]\\s*|status[^0-9]*)([45][0-9]{2})/);return {ok:false,transport:'desktop-ipc',status:match?Number(match[1]):null,error:text}; }
                        }""", {'url': renderer_url, 'path': path, 'body': body})
                        require_owned_process(process)
                        return value
                    with installed_phase(process, end, INSTALLED_PHASE_SECONDS['readiness'], InstalledRecoveryReadinessTimeout, cleanup=cleanup_owned) as deadline:
                        while True:
                            if time.monotonic() >= deadline:
                                raise InstalledRecoveryReadinessTimeout('Trusted renderer API session guard was not observed before deadline')
                            response = api('/api/studio/snapshot')
                            if response.get('ok') is False and 'Missing application session token' in str(response.get('error', '')):
                                break
                            dispatch_browser_events(browser, deadline)
                        runtime = installed_core_process(process.pid, core_executable)
                        ready_perf_ns = time.perf_counter_ns()
                    with installed_phase(process, end, INSTALLED_PHASE_SECONDS['api'], InstalledRecoveryApiTimeout, cleanup=cleanup_owned) as deadline:
                        result = exercise(api, data, manifest, timeout=deadline - time.monotonic())
                    # Browser.close reaches Electron's real before-quit path,
                    # which drains the Core and owns its shutdown deadline.
                    with installed_phase(process, end, INSTALLED_PHASE_SECONDS['shutdown'], InstalledRecoveryShutdownTimeout, cleanup=cleanup_owned) as deadline:
                        cdp = browser.new_browser_cdp_session()
                        try: cdp.send('Browser.close')
                        except PlaywrightError: pass  # CDP may disconnect before replying; process/child exit still required.
                        try: process.wait(timeout=max(.001, deadline - time.monotonic()))
                        except subprocess.TimeoutExpired:
                            raise InstalledRecoveryShutdownTimeout('Owned installed desktop did not shut down before deadline') from None
                        require(process.returncode == 0, 'Installed desktop shutdown was not clean')
                        # The desktop must have reaped its exact Core, not orphaned it.
                        query = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', 'if(Get-Process -Id ' + str(runtime['pid']) + ' -ErrorAction SilentlyContinue){exit 9}'], timeout=min(10, max(.001, deadline - time.monotonic())))
                        require(query.returncode == 0, 'Installed desktop left its Core alive')
                    shutdown_complete = True
            except Exception:
                failed = True
                if timed_out.is_set():
                    raise InstalledRecoveryTimeout('Installed recovery process deadline expired') from None
                raise
            finally:
                with watchdog_gate:
                    watchdog_armed = False
                watchdog.cancel()
                watchdog.join(timeout=45)
                if watchdog.is_alive() or time.monotonic() >= end:
                    timed_out.set()
                if not shutdown_complete or timed_out.is_set():
                    if runtime is None:
                        try: runtime = installed_core_process(process.pid, core_executable)
                        except Exception: pass
                    try: cleanup_owned()
                    except Exception:
                        if not failed and not timed_out.is_set():
                            raise
        if timed_out.is_set():
            raise InstalledRecoveryTimeout('Installed recovery process deadline expired')
        require(fixture.legacy.file_sha256(manifest_path) == manifest_hash, 'Installed app changed the external input manifest')
        result['persisted_state'] = fixture.inspect(data, manifest, phase='complete')
        proof = {**result, **binding, 'desktop_app_asar_sha256': fixture.legacy.file_sha256(executable.parent / 'resources/app.asar'), 'verified': True, 'contract': fixture.CONTRACT, 'platform': sys.platform,
            'synthetic': True, 'live_accounts_tested': False, 'user_data_touched': False,
            'desktop': {'pid': process.pid, 'executable': str(executable), 'sha256': fixture.legacy.file_sha256(executable)},
            'core': runtime, 'core_frozen': True, 'core_upgrade_manifest_sha256': prior['manifest_sha256'],
            'seed_pid': manifest['seed_pid'], 'seed_completed_perf_ns': manifest['seed_completed_perf_ns'],
            'launch_perf_ns': launch_perf_ns, 'ready_perf_ns': ready_perf_ns, 'chronology_clock': manifest['chronology_clock'],
            'manifest_sha256': manifest_hash, 'input_database_sha256': manifest['database_sha256'], 'nonce': manifest['nonce'],
            'normal_shutdown': True, 'manifest_unchanged': True}
        return validate_proof(proof, executable=executable, core_executable=core_executable)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--core-executable', type=Path, required=True)
    parser.add_argument('--core-report', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=INSTALLED_TIMEOUT_SECONDS,
        help='Overall owned-runtime deadline in seconds; phase caps remain independent')
    args = parser.parse_args()
    args.report.unlink(missing_ok=True)
    try:
        proof = probe_installed(args.executable, args.core_executable, args.core_report, args.log, timeout=args.timeout)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except Exception as error:
        # The public CI wrapper keeps this complete stream on the runner and
        # exports only sealed relative source locations/fixed error categories.
        # Retain the active traceback so a failed installed gate is diagnosable
        # without exporting exception messages, runtime paths or application data.
        try:
            traceback.print_exc(file=sys.stderr)
        except Exception:
            pass  # Diagnostic output cannot turn the failed proof into success.
        print('INSTALLED_RECOVERY_R64=FAIL ' + str(error), flush=True)
        return 1
    print('INSTALLED_RECOVERY_R64=PASS ' + json.dumps(proof, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
