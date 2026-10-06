"""One-click, local-only Windows startup diagnosis; does not build the app.

Uses fresh disposable profiles. Never opens or closes a business-task window.
File logging is required because Windows stderr alone can omit Chrome output:
https://www.chromium.org/for-testers/enable-logging/
"""
from __future__ import annotations

import base64
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.request import build_opener, ProxyHandler
import uuid
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
REPORT_LIMIT = 1024 * 1024
LIVE_LOG_LIMIT = 32 * 1024 * 1024


def resolve_project(path):
    project = Path(path).resolve()
    required = ('package.json', 'START_HERE_NEWGEN.bat', '.venv/Scripts/python.exe',
                'backend/app/browser_runtime.py')
    missing = [name for name in required if not (project / name).is_file()]
    if missing:
        raise ValueError('请选择之前构建过的完整聚鑫国际项目目录；该目录缺少：' + ', '.join(missing))
    metadata = json.loads((project / 'package.json').read_text(encoding='utf-8-sig'))
    if metadata.get('name') != 'juxin-ig-audience-collector-newgen':
        raise ValueError('所选文件夹不是聚鑫国际项目目录')
    if not (project / 'build' / 'browsers').is_dir():
        raise ValueError('所选项目缺少 build/browsers；请选择上次已经下载浏览器并构建失败的目录')
    return project


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def command(executable, profile, log, *, minimal=False):
    args = [str(executable), '--user-data-dir=' + str(profile),
            '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
            '--no-first-run', '--no-default-browser-check',
            '--enable-logging', '--log-file=' + str(log), '--v=1']
    if not minimal:
        args += ['--lang=zh-CN', '--disable-background-mode', '--disable-background-timer-throttling',
                 '--disable-backgrounding-occluded-windows', '--disable-renderer-backgrounding']
    return args + ['about:blank']


def read_endpoint(profile):
    try:
        lines = (profile / 'DevToolsActivePort').read_text(encoding='utf-8').splitlines()
        if len(lines) < 2 or not lines[0].isdigit() or not 0 < int(lines[0]) < 65536:
            return None
        if not re.fullmatch(r'/devtools/browser/[A-Za-z0-9._:-]+', lines[1]):return None
        # Bypass any system HTTP proxy only for this fresh localhost test server.
        opener = build_opener(ProxyHandler({}))
        with opener.open('http://127.0.0.1:' + lines[0] + '/json/version', timeout=2) as response:
            version = json.loads(response.read(65536))
        expected = 'ws://127.0.0.1:' + lines[0] + lines[1]
        if version.get('webSocketDebuggerUrl') != expected:return None
        return {'ws': expected, 'browser': version.get('Browser'), 'protocol': version.get('Protocol-Version')}
    except (OSError, ValueError):return None


def stop_owned_process(process, endpoint):
    if process.poll() is not None:return
    if endpoint:
        try:
            from websockets.sync.client import connect
            with connect(endpoint['ws'], open_timeout=2, close_timeout=1, proxy=None) as ws:
                ws.send('{"id":1,"method":"Browser.close"}')
            process.wait(timeout=3)
            return
        except Exception:pass
    # Only the subprocess handle created by this probe; never taskkill by name.
    process.terminate()
    try:process.wait(timeout=3)
    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)


def bounded_log(source, target):
    info = {'exists': source.is_file(), 'bytes': 0, 'truncated': False}
    if not info['exists']:return info
    size = source.stat().st_size
    info.update(bytes=size, truncated=size > REPORT_LIMIT)
    with source.open('rb') as stream:
        if size <= REPORT_LIMIT:data = stream.read(REPORT_LIMIT)
        else:
            beginning = stream.read(REPORT_LIMIT // 2)
            stream.seek(-REPORT_LIMIT // 2, 2)
            data = beginning + b'\n[LOG MIDDLE OMITTED]\n' + stream.read(REPORT_LIMIT // 2)
    target.write_bytes(data)
    return info


def classify_startup_logs(output, result):
    # A responding browser CDP port does not prove that sandboxed child/network
    # processes can run. Match the concrete Chromium error, not a Win32 number
    # in an unrelated GPU cache warning or a normal crash histogram name.
    for name in ('chrome.log', 'console.log'):
        path = output / name
        if path.is_file():
            try:text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:continue
            if 'Sandbox cannot access executable' in text:
                result['startup_status'] = result['status']
                result['status'] = 'sandbox_access_denied'
                result['fault'] = 'executable_sandbox_read_execute_denied'
                result['fault_log'] = name
                return


def probe(executable, profile, output, *, minimal=False, timeout=35):
    profile.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    if not minimal:
        preferences=profile/'Default'/'Preferences'
        if not preferences.exists() and not (profile/'Local State').exists():
            preferences.parent.mkdir(parents=True,exist_ok=True)
            save_json(preferences,{'intl':{'accept_languages':'zh-CN,zh','selected_languages':'zh-CN,zh'}})
    log = profile / 'chrome_debug.log'
    stdio = profile / 'console.log'
    args = command(executable, profile, log, minimal=minimal)
    env = os.environ.copy()
    # Both mechanisms point to the same fresh test-only file. Do not alter the
    # parent environment, nor dump it (it may contain API keys).
    env['CHROME_LOG_FILE'] = str(log)
    result = {'status': 'starting', 'profile': str(profile), 'profile_path_length': len(str(profile)),
              'args': args, 'started_utc': datetime.now(timezone.utc).isoformat()}
    process = None;endpoint = None
    started = time.monotonic()
    try:
        with stdio.open('wb') as stream:
            process = subprocess.Popen(args, cwd=str(executable.parent), env=env,
                                       stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT)
            result['pid'] = process.pid
            while time.monotonic() - started < timeout:
                if process.poll() is not None:
                    result.update(status='startup_exited', exit_code=process.returncode)
                    break
                if any(p.exists() and p.stat().st_size > LIVE_LOG_LIMIT for p in (log, stdio)):
                    result['status'] = 'log_limit';break
                endpoint = read_endpoint(profile)
                if endpoint:
                    result.update(status='cdp_responded', endpoint=endpoint)
                    break
                time.sleep(.2)
            else:result['status'] = 'startup_timeout'
    except Exception as error:
        result.update(status='probe_error', error=str(error))
        (output / 'traceback.txt').write_text(traceback.format_exc(), encoding='utf-8')
    finally:
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        if process:
            try:stop_owned_process(process, endpoint)
            except Exception as error:result['cleanup_error'] = str(error)
            result['exit_after_cleanup'] = process.poll()
        for name, source in [('chrome', log), ('console', stdio)]:
            try:result[name + '_log'] = bounded_log(source, output / (name + '.log'))
            except OSError as error:result[name + '_log'] = {'error': str(error)}
        classify_startup_logs(output, result)
        result['crash_reports'] = len(list((profile / 'Crashpad' / 'reports').glob('*')))
        save_json(output / 'result.json', result)
    return result


def windows_events(start, executable, pids):
    if os.name != 'nt':return {'status': 'not_windows'}
    # Only recent Application Error/WER events matching our exact executable
    # path or created process IDs. No unrelated Windows event history is read.
    script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$start = [DateTime]::Parse($env:JUXIN_DIAGNOSTIC_START).ToUniversalTime()
$exe = $env:JUXIN_DIAGNOSTIC_EXE
$pids = @($env:JUXIN_DIAGNOSTIC_PIDS.Split(',') | Where-Object { $_ } | ForEach-Object { [long]$_ })
try {
  $events = @(Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000,1001; StartTime=$start} -MaxEvents 100 -ErrorAction Stop)
  $found = @()
  foreach ($event in $events) {
    $xml = [xml]$event.ToXml()
    $matched = $event.Message -and $event.Message.IndexOf($exe, [StringComparison]::OrdinalIgnoreCase) -ge 0
    foreach ($item in $xml.Event.EventData.Data) {
      if ($item.Name -eq 'ProcessId') {
        $value = [string]$item.'#text'
        try {
          if ($value.StartsWith('0x')) { $number = [Convert]::ToInt64($value.Substring(2),16) }
          else { $number = [long]$value }
          if ($pids -contains $number) { $matched = $true }
        } catch {}
      }
    }
    if ($matched) { $found += @{time=$event.TimeCreated.ToUniversalTime().ToString('o'); id=$event.Id; provider=$event.ProviderName; message=$event.Message; xml=$event.ToXml()} }
  }
  @{status='collected'; events=$found} | ConvertTo-Json -Depth 6 -Compress
} catch {
  if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { @{status='no_matching_events'; events=@()} | ConvertTo-Json -Compress }
  else { @{status='unavailable'; error=$_.Exception.Message} | ConvertTo-Json -Compress }
}
'''
    env = os.environ.copy()
    env.update(JUXIN_DIAGNOSTIC_START=start, JUXIN_DIAGNOSTIC_EXE=str(executable),
               JUXIN_DIAGNOSTIC_PIDS=','.join(str(pid) for pid in pids))
    shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    try:
        response = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-EncodedCommand',
                                   base64.b64encode(script.encode('utf-16le')).decode('ascii')],
                                  env=env, capture_output=True, timeout=15)
        if response.returncode:return {'status': 'unavailable', 'error': response.stderr.decode('utf-8', errors='replace')[-8000:]}
        return json.loads(response.stdout.decode('utf-8-sig'))
    except Exception as error:return {'status': 'unavailable', 'error': str(error)}


def make_bundle(run, destination):
    # Allowlist reports; never package browser profiles, Cookies, PMA, SQLite,
    # credentials, or unrelated installer-output files.
    with ZipFile(destination, 'w', ZIP_DEFLATED) as bundle:
        for path in sorted(run.rglob('*')):
            if path.is_file() and path.name in {'result.json', 'summary.json', 'windows-events.json',
                                               'chrome.log', 'console.log', 'traceback.txt'}:
                bundle.write(path, path.relative_to(run).as_posix())


def installed_comparison_browser():
    if os.name != 'nt':return None
    for relative in ('Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe'):
        for variable in ('LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)'):
            base = os.environ.get(variable)
            if base:
                candidate = Path(base) / relative
                if candidate.is_file():return candidate.resolve()
    return None


def main(project_dir=None):
    global ROOT
    if hasattr(sys.stdout, 'reconfigure'):sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    if project_dir is not None:
        try:ROOT = resolve_project(project_dir)
        except (OSError, ValueError) as error:
            print('[ERROR] ' + str(error), flush=True)
            return 2
        # Imports and output belong to the selected project, even when the
        # diagnostic ZIP was extracted to an unrelated folder.
        sys.path.insert(0, str(ROOT / 'backend'))
    output = ROOT / 'installer-output'
    output.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    run = output / ('startup-report-' + stamp);run.mkdir()
    summary = {'purpose': 'startup diagnosis only; not installation or full isolation verification',
               'started_utc': datetime.now(timezone.utc).isoformat(), 'python': sys.version,
               'cases': [], 'status': 'collecting'}
    work = None;short = None
    try:
        from app.browser_runtime import bundled_executable, pinned_chromium
        executable = bundled_executable(ROOT / 'build' / 'browsers')
        driver = pinned_chromium()
        from app.browser_runtime import bundled_chromium
        revision, version = bundled_chromium(ROOT / 'build' / 'browsers', driver)
        summary['driver_expected_version'] = driver[1]
        summary.update(executable=str(executable), revision=revision, expected_version=version,
                       executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
        profile_parent = output / 'native-browser-diagnostics'
        profile_parent.mkdir(exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix='run-', dir=profile_parent))
        short = Path(tempfile.mkdtemp(prefix='jx-'))
        def long_profile():return work / '窗口 验证 (1)' / 'browser-profiles' / str(uuid.uuid4()) / str(uuid.uuid4())
        cases = [('app-settings', long_profile(), False), ('short-data-path', short / 'p', False),
                 ('minimal-settings', long_profile(), True)]
        for label, profile, minimal in cases:
            print('[CHECK] ' + label + ' (local blank window only)', flush=True)
            result = probe(executable, profile, run / label, minimal=minimal)
            summary['cases'].append({'name': label, **result})
            print('[RESULT] ' + label + ': ' + result['status'] + ', exit=' + str(result.get('exit_code')), flush=True)
        comparison = installed_comparison_browser()
        if comparison:
            print('[CHECK] installed-browser-comparison (fresh temporary profile)', flush=True)
            result = probe(comparison, long_profile(), run / 'installed-browser-comparison')
            summary['cases'].append({'name': 'installed-browser-comparison', **result})
            print('[RESULT] installed-browser-comparison: ' + result['status'], flush=True)
        else:summary['installed_comparison'] = 'not_found; no browser installed or downloaded by this tool'
        events = {}
        for label, target in [('bundled', executable), ('installed_comparison', comparison)]:
            if target:
                pids = [r['pid'] for r in summary['cases'] if r.get('pid') and r['args'][0] == str(target)]
                events[label] = windows_events(summary['started_utc'], target, pids)
        save_json(run / 'windows-events.json', events)
        summary['status'] = 'collected'
    except Exception as error:
        summary.update(status='diagnostic_error', error=str(error))
        (run / 'traceback.txt').write_text(traceback.format_exc(), encoding='utf-8')
    finally:
        # These two locations were freshly allocated above by this process.
        for profile_root in (work, short):
            if profile_root:
                try:shutil.rmtree(profile_root)
                except OSError as error:summary.setdefault('cleanup_notes', []).append(str(error))
        save_json(run / 'summary.json', summary)
        destination = output / ('native-startup-check-' + stamp + '.zip')
        make_bundle(run, destination)
        print('\n诊断已保存，请上传这个 ZIP：\n' + str(destination), flush=True)
        print('这里只完成启动诊断，不代表浏览器或安装构建已经通过。', flush=True)
    return 0 if summary['status'] == 'collected' else 1


def cli(argv=None):
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--project-dir', type=Path)
    source.add_argument('--project-dir-env', action='store_true',
                        help='Read the selected project from JUXIN_DIAGNOSTIC_PROJECT_DIR')
    options = parser.parse_args(argv)
    project = options.project_dir
    if options.project_dir_env:
        # Windows PowerShell 5.1 re-quotes native arguments. A selected directory
        # ending in a backslash can become a literal trailing quote in argv.
        # Inherit the exact Unicode path instead of serializing it into argv.
        value = os.environ.get('JUXIN_DIAGNOSTIC_PROJECT_DIR')
        if not value or not value.strip():
            parser.error('诊断入口未传入项目目录，请重新运行 DIAGNOSE_NATIVE_BROWSER.bat')
        project = Path(value)
    return main(project)


if __name__ == '__main__':
    sys.exit(cli())
