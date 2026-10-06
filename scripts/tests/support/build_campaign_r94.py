"""900 seeded build-component scenarios; no native Windows installer is built.

Uses actual archive/publication/dependency decision code and local Core probe
processes. Downloads, pip exit codes and Windows-only faults are controlled.
Run: python scripts/tests/support/build_campaign_r94.py --output PATH
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import random
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import Mock, patch
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/tests')]
import install_python_dependencies as dependencies
import upgrade_build_pip as upgrade
import test_release_packaging_r94 as release
from test_build_recovery_r94 import load_browser_fixture

archive = release.archive_tool
core = release.core_tool
FAMILIES = ('windows_paths', 'archive_roundtrip', 'archive_write_fault',
            'archive_corruption', 'archive_source_change', 'dependency_sequence',
            'pip_sequence', 'cleanup_lock', 'browser_publication', 'core_probe')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def expect_error(action, kind, fragment=None):
    try:
        action()
    except kind as error:
        if fragment is not None:
            assert fragment in str(error), repr(error)
        return type(error).__name__
    raise AssertionError('Expected fault was not rejected')


def staged(root, seed):
    source = root / f'便携 APP [{seed}] (完整)'
    source.mkdir()
    payloads = {}
    rng = random.Random(seed)
    for i in range(2 + seed % 7):
        path = source / ('.runtime' if i % 2 else '资源') / ('子目录' * (i % 3 + 1)) / f'文件 {i}.bin'
        path.parent.mkdir(parents=True, exist_ok=True)
        data = rng.randbytes(1 + (seed * 137 + i * 79) % 16384)
        path.write_bytes(data)
        os.utime(path, (seed * 123, seed * 123))
        payloads[path.relative_to(root).as_posix()] = data
    (source / '空目录').mkdir()
    return source, root / f'输出 [{seed}].zip', payloads


def run_case(family, seed, root):
    params = {'seed': seed}
    if family == 'windows_paths':
        spellings = [f'D:/IG实验室/新建文件夹 ({seed})/.venv/Scripts/python.exe',
                     f'//build-server/共享目录/构建 [{seed}]/.venv/Scripts/python.exe',
                     f'/fixture/new-venv-{seed}/Scripts/python.exe']
        executable = (PureWindowsPath if seed % 3 else PurePosixPath)(spellings[seed % 3])
        base = str(PureWindowsPath(f'C:/Python {seed}/python.exe'))
        deadline = 30 + seed
        with patch.object(core, 'IS_WINDOWS', True), patch.object(sys, '_base_executable', base), \
                patch.dict(os.environ, {'__PYVENV_LAUNCHER__': 'stale-env'}), \
                patch.object(subprocess, 'run', return_value=Mock(returncode=0)) as launch:
            release.run_offline_suite(executable, deadline)
        assert launch.call_count == 1
        assert launch.call_args.args[0][0] == base
        assert launch.call_args.kwargs['env']['__PYVENV_LAUNCHER__'] == str(executable)
        assert launch.call_args.kwargs['timeout'] == deadline
        params.update(path=str(executable), deadline=deadline)

    elif family.startswith('archive_'):
        source, destination, payloads = staged(root, seed)
        params.update(files=len(payloads), input_bytes=sum(map(len, payloads.values())))
        previous = b'previous verified archive ' + str(seed).encode()
        destination.write_bytes(previous)
        if family == 'archive_roundtrip':
            with patch.object(zipfile, 'ZIP64_LIMIT', 128 if seed % 2 else zipfile.ZIP64_LIMIT):
                result = archive.package_archive(source, destination)
            assert result['verified'] and result['files'] == len(payloads)
            assert result['sha256'] == digest(destination.read_bytes())
            with zipfile.ZipFile(destination) as zipped:
                assert zipped.testzip() is None
                for name, data in payloads.items():
                    assert zipped.read(name) == data
                assert source.name + '/空目录/' in zipped.namelist()
            params['zip64_forced'] = bool(seed % 2)
        elif family == 'archive_write_fault':
            stage = ('write', 'fsync', 'publish')[seed % 3]
            params['fault_stage'] = stage
            with ExitStack() as patches:
                if stage == 'write':
                    original, calls = zipfile.ZipFile.open, []
                    nth = 1 + seed % len(payloads)
                    def fail_write(self, name, mode='r', *args, **kwargs):
                        if mode == 'w' and isinstance(name, zipfile.ZipInfo) and not name.is_dir():
                            calls.append(name.filename)
                            if len(calls) == nth:
                                raise OSError(28, 'injected disk full')
                        return original(self, name, mode, *args, **kwargs)
                    patches.enter_context(patch.object(zipfile.ZipFile, 'open', fail_write))
                    params['file_index'] = nth
                else:
                    patches.enter_context(patch.object(archive.os, 'fsync' if stage == 'fsync' else 'replace',
                        side_effect=OSError(28 if stage == 'fsync' else 13, 'injected publication failure')))
                expect_error(lambda: archive.package_archive(source, destination), OSError, 'injected')
            assert destination.read_bytes() == previous
        elif family == 'archive_corruption':
            mode = ('missing', 'duplicate', 'changed', 'extra', 'truncated')[seed % 5]
            params['fault'] = mode
            keys = list(payloads)
            victim = keys[seed % len(keys)]
            expected = {name: digest(data) for name, data in payloads.items()}
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                with zipfile.ZipFile(destination, 'w') as zipped:
                    for name, data in payloads.items():
                        if mode == 'missing' and name == victim:
                            continue
                        zipped.writestr(name, data + b'x' if mode == 'changed' and name == victim else data)
                    if mode == 'duplicate':
                        zipped.writestr(victim, payloads[victim])
                    elif mode == 'extra':
                        zipped.writestr('unexpected.bin', b'not staged')
            if mode == 'truncated':
                destination.write_bytes(destination.read_bytes()[:-22])
            expect_error(lambda: archive.verify_archive(destination, expected), (RuntimeError, zipfile.BadZipFile))
        elif family == 'archive_source_change':
            mode = ('modify', 'delete', 'add')[seed % 3]
            original, scans = archive.inventory, []
            victim = root / list(payloads)[seed % len(payloads)]
            def changed(path):
                scans.append(1)
                if len(scans) == 2:
                    if mode == 'modify':
                        victim.write_bytes(victim.read_bytes() + b'changed')
                    elif mode == 'delete':
                        victim.unlink()
                    else:
                        (source / f'late-{seed}.dll').write_bytes(b'late')
                return original(path)
            with patch.object(archive, 'inventory', changed):
                expect_error(lambda: archive.package_archive(source, destination), RuntimeError, 'source changed')
            assert len(scans) == 2 and destination.read_bytes() == previous
            params['fault'] = mode
        assert not list(root.glob('*.tmp')), 'temporary archive leaked'

    elif family in ('dependency_sequence', 'pip_sequence'):
        deps = family == 'dependency_sequence'
        options = ([([0,0,0],0), ([1,0,0,0],0), ([1,1],1), ([0,1,0,0,0],0),
                    ([0,0,1,0,0,1],1), ([0,1,0,1],1)] if deps else
                   [([0,0,0],0), ([0,1,0,0],0), ([0,1,1,0],0), ([1],1),
                    ([0,0,1,0,0],0), ([0,0,1,0,1,1],1)])
        codes, expected = options[seed % len(options)]
        inherited = {('pip_no_index' if seed % 2 else 'PIP_NO_INDEX'): '1',
                     'PIP_INDEX_URL': f'https://user:fixture-secret-{seed}@stale.invalid/simple',
                     'PIP_TARGET': f'old target {seed}', 'HTTPS_PROXY': f'http://proxy-{seed}.invalid',
                     'REQUESTS_CA_BUNDLE': f'CA 证书 {seed}.pem'}
        saved, calls, logs, outcomes = dict(inherited), [], [], iter(codes)
        for directory in ('scripts', 'backend'):
            (root / directory).mkdir()
        packaging = bool(seed % 2)
        requirement = root / ('scripts/windows-packaging-requirements.txt' if packaging else 'backend/requirements.txt')
        requirement.write_text('fixture-package==1.2.3\n', encoding='utf-8')
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, next(outcomes))
        function = dependencies.install_dependencies if deps else upgrade.upgrade_pip
        result = function(root, runner=runner, environ=inherited,
                          output=lambda text, **_: logs.append(text), **({'packaging': packaging} if deps else {}))
        assert result == expected and len(calls) == len(codes)
        assert inherited == saved and requirement.read_text() == 'fixture-package==1.2.3\n'
        assert f'fixture-secret-{seed}' not in '\n'.join(logs)
        for command, kwargs in calls:
            assert command[0] == sys.executable and command[1:4] == ['-I','-X','utf8']
            if '--index-url' in command:
                assert kwargs['env'] == dependencies.official_environment(inherited)
                assert '--trusted-host' not in command
        params.update(return_codes=codes, expected_exit=expected, packaging=packaging if deps else False)

    elif family == 'cleanup_lock':
        attempts = (seed - 1) % 9
        code = (32, 33, 5)[((seed - 1) // 9) % 3]
        failure = PermissionError(f'injected lock {seed}')
        failure.winerror = code
        directory = Mock()
        directory.cleanup.side_effect = [failure] * attempts + [None]
        fails = attempts > 0 and (code == 5 or attempts >= 6)
        with patch.object(core.time, 'sleep') as delay:
            if fails:
                expect_error(lambda: core.cleanup_probe_directory(directory), PermissionError, 'injected lock')
            else:
                core.cleanup_probe_directory(directory)
        expected_calls = 1 if code == 5 and attempts else min(attempts + 1, 6)
        assert directory.cleanup.call_count == expected_calls
        assert len(delay.call_args_list) == expected_calls - 1
        assert sum(c.args[0] for c in delay.call_args_list) <= 1.55
        params.update(winerror=code, locked_attempts=attempts, rejected=fails)

    elif family == 'browser_publication':
        modes = ('ok', 'missing', 'empty', 'verify_failure', 'cancel', 'publish_failure')
        mode = modes[seed % len(modes)]
        browsers = root / 'build/browsers'
        revision = str(1000 + seed)
        destination = browsers / f'chromium-{revision}'
        destination.mkdir(parents=True)
        previous = f'original runtime {seed}'.encode()
        (destination / 'old-runtime').write_bytes(previous)
        registry = root / 'playwright/driver/package'
        registry.mkdir(parents=True)
        (registry / 'browsers.json').write_text(json.dumps({'browsers': [{
            'name':'chromium','revision':revision,'browserVersion':f'1.2.{seed}.4'}]}))
        install = load_browser_fixture(root, browsers)
        names = ('chrome.exe','chrome.dll','icudtl.dat','resources.pak','locales/en-US.pak')
        victim = names[(seed // 6) % len(names)]
        payload = f'new fixture browser {seed}'.encode()
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as zipped:
            for name in names:
                if mode == 'missing' and name == victim:
                    continue
                zipped.writestr('chrome-win64/' + name, b'' if mode == 'empty' and name == victim else payload)
        checks = []
        def verify(path):
            checks.append(path)
            assert path == destination and (path / 'INSTALLATION_COMPLETE').is_file()
            assert (path / 'chrome-win64/chrome.exe').read_bytes() == payload
            if mode == 'verify_failure':
                raise RuntimeError('injected native check failure')
            if mode == 'cancel':
                raise KeyboardInterrupt('injected cancellation')
        original_rename = Path.rename
        def rename(path, target):
            if mode == 'publish_failure' and path.name == 'extracted':
                raise PermissionError('injected publication lock')
            return original_rename(path, target)
        with patch.object(install.__globals__['urllib'].request, 'urlopen', return_value=io.BytesIO(content.getvalue())) as download, \
                patch.object(Path, 'rename', rename):
            if mode == 'ok':
                install(verify=verify)
                assert len(checks) == 1 and not (destination / 'old-runtime').exists()
            else:
                kind = KeyboardInterrupt if mode == 'cancel' else PermissionError if mode == 'publish_failure' else RuntimeError
                expect_error(lambda: install(verify=verify), kind)
                assert (destination / 'old-runtime').read_bytes() == previous
                assert not (destination / 'chrome-win64').exists()
            assert download.call_count == 1
        params.update(mode=mode, resource=victim, revision=revision)

    elif family == 'core_probe':
        modes = ('ok', 'exit', 'old', 'no-auth', 'missing-db', 'stuck-shutdown')
        mode = modes[seed % len(modes)]
        case = release.FrozenCoreProbeTests('test_auth_database_and_orderly_shutdown_with_contaminated_parent_env')
        case.setUp()
        try:
            case.fixture = case.root / f'Core 中文 [{seed}] (probe).py'
            case.fixture.write_text(f'import time;time.sleep({seed % 5 * .01})\n' + release.FAKE_CORE, encoding='utf-8')
            user_data = case.root / 'existing.sqlite3'
            user_data.write_bytes(f'existing user database {seed}'.encode())
            expected = user_data.read_bytes()
            with patch.dict(os.environ, {'IGAC_PARENT_PID':str(seed),'PYTHONPATH':f'wrong-{seed}',
                                        'IGAC_DB_PATH':str(user_data)}):
                if mode == 'ok':
                    result = case.run_probe(mode)
                    assert result['verified'] and result['orderly_shutdown']
                else:
                    fragments = {'exit':'exit 23','old':'payload is invalid','no-auth':'without its startup token',
                                 'missing-db':'isolated database','stuck-shutdown':'did not exit'}
                    expect_error(lambda: case.run_probe(mode, shutdown_timeout=.2 + seed % 3 * .03),
                                 RuntimeError, fragments[mode])
            assert user_data.read_bytes() == expected
        finally:
            case.doCleanups()
        params.update(mode=mode, startup_delay=seed % 5 * .01)
    else:
        raise AssertionError(family)
    return params


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', help='Comma-separated scenario numbers for focused reproduction')
    args = parser.parse_args()
    selected = set(map(int, args.cases.split(','))) if args.cases else set(range(1, 901))
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    with (args.output / 'campaign.jsonl').open('w', encoding='utf-8') as stream:
        for number in sorted(selected):
            assert 1 <= number <= 900
            family, seed = FAMILIES[(number - 1) // 90], (number - 1) % 90 + 1
            row = {'id':f'M{number:04}', 'family':family, 'seed':seed}
            started = time.monotonic()
            output = io.StringIO()
            try:
                with tempfile.TemporaryDirectory(prefix=f'构建 [{number}] (离线) ') as temporary, redirect_stdout(output):
                    row['parameters'] = run_case(family, seed, Path(temporary))
                row['status'] = 'passed'
            except BaseException:
                row.update(status='failed', traceback=traceback.format_exc(), output=output.getvalue())
            row['seconds'] = round(time.monotonic() - started, 4)
            stream.write(json.dumps(row, ensure_ascii=False) + '\n'); stream.flush()
            records.append(row)
            if row['status'] != 'passed':
                print(json.dumps(row, ensure_ascii=False), flush=True)
                return 1
            if len(records) % 30 == 0:
                print(json.dumps({'passed':len(records),'planned':len(selected),'last':row['id'],'family':family}), flush=True)
    summary = {'planned':len(selected),'passed':len(records),'failed':0,
               'seconds':round(sum(r['seconds'] for r in records), 4),
               'native_windows_build':False,'real_browser_download':False,'installer_created':False}
    (args.output / 'campaign-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
