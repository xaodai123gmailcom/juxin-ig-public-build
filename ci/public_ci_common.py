"""Public CI controls. Raw runtime evidence and logs stay on the Windows runner."""
from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import threading
import tempfile
import time
from types import TracebackType
import uuid

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = {'full_name': 'xaodai123gmailcom/juxin-ig-public-build',
              'id': '1406784621', 'owner_id': '337452708'}
STAGES = ('contracts', 'early', 'build', 'installed')
PUBLIC_FILES = ('run-summary.json', 'source-build-proof.json', 'installed-acceptance-proof.json')
EARLY_FILES = ('r64-early-verification.json', 'r62-posting-viewport-native.json',
    'r62-posting-viewport-native.png', 'r63-nurture-cleanup-native.json',
    'r63-nurture-cleanup-native.png', 'r64-crop-icon-native.json', 'r64-crop-icon-native.png',
    'r64-recovery-ui-native-proof.json', 'r64-recovery-ui-withdrawn-review.png',
    'r64-recovery-ui-hidden-blocker.png', 'r64-recovery-ui-stopped-feedback.png',
    'r64-recovery-ui-fresh-review.png', 'final-seed-fixtures/final-seed-browser-r62.json',
    'final-seed-fixtures/final-seed-browser-r62.png')
FINAL_IMAGES = ('r6-collection-counters.png', 'r6-work-report-summary.png', 'r6-work-report-data-overview.png',
    'r6-shell-wolf-accounts.png', 'r6-shell-wolf-collection.png', 'r6-shell-refresh-failure.png',
    'r6-nurture-settings.png', 'r6-nurture-history.png', 'r6-nurture-narrow.png', 'r6-nurture-read-error.png',
    'r6-posting-actual.png', 'r6-posting-stress.png', 'r62-posting-retry-actual.png')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def same_json(left, right):
    return json.dumps(left, sort_keys=True, allow_nan=False, separators=(',', ':')) == json.dumps(
        right, sort_keys=True, allow_nan=False, separators=(',', ':'))


def load_source_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def regular(path, *, directory=False):
    path = Path(path)
    info = path.lstat()
    require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, 'st_file_attributes', 0) & 0x400),
            'Linked evidence is not accepted')
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), 'Invalid evidence type')
    for parent in path.parents:
        info = parent.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not (getattr(info, 'st_file_attributes', 0) & 0x400),
                'Linked evidence parent is not accepted')
    return path


def digest(path):
    with regular(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    require(regular(path).stat().st_size <= 32 * 1024 * 1024, 'Evidence exceeds read bound')
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    data = (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    require(len(data) <= 65536, 'Summary exceeds export bound')
    path.parent.mkdir(parents=True, exist_ok=True)
    regular(path.parent, directory=True)
    with path.open('xb') as stream:
        stream.write(data)


def state_root():
    run, attempt = os.environ.get('GITHUB_RUN_ID', ''), os.environ.get('GITHUB_RUN_ATTEMPT', '')
    require(re.fullmatch(r'[1-9][0-9]*', run) and re.fullmatch(r'[1-9][0-9]*', attempt), 'Missing real run identity')
    base = Path(os.environ['RUNNER_TEMP'])
    require(base.is_absolute(), 'Runner temporary directory must be absolute')
    regular(base, directory=True)
    result = base / f'juxin-public-ci-{run}-{attempt}'
    require(result != ROOT and ROOT not in result.parents, 'Evidence must stay outside the source checkout')
    return result


def source_identity():
    result = load_source_module('ci_source_binding').verify_ci_source_binding(ROOT)
    require(type(result.get('schema')) is int and result.get('schema') == 2 and result.get('mode') == 'flat-git-ci' and
            result.get('representation') == 'public-sanitized-source' and result.get('repository') == REPOSITORY,
            'Public source representation mismatch')
    return result


def actual_run():
    require(os.name == 'nt' and os.environ.get('RUNNER_OS') == 'Windows' and
            os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted',
            'Actual standard hosted Windows Actions execution is required')
    for key, value in (('GITHUB_REPOSITORY', REPOSITORY['full_name']),
                       ('GITHUB_REPOSITORY_ID', REPOSITORY['id']),
                       ('GITHUB_REPOSITORY_OWNER_ID', REPOSITORY['owner_id'])):
        require(os.environ.get(key) == value, 'Unauthorized repository identity')
    event = read_json(os.environ['GITHUB_EVENT_PATH'])
    repository = event.get('repository', {})
    require(repository.get('private') is False and str(repository.get('id')) == REPOSITORY['id'] and
            str(repository.get('owner', {}).get('id')) == REPOSITORY['owner_id'] and
            repository.get('full_name') == REPOSITORY['full_name'], 'Actual public repository event required')
    require(os.environ.get('GITHUB_EVENT_NAME') in ('push', 'workflow_dispatch'), 'Unsupported workflow event')
    return {'repository': dict(REPOSITORY), 'run_id': os.environ['GITHUB_RUN_ID'],
            'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'], 'source_commit': os.environ['GITHUB_SHA']}


def telemetry_file():
    # Resolve the OS Local AppData folder, not a relocated HOME or a fabricated profile.
    require(os.name == 'nt', 'Telemetry consent is Windows-only')
    buffer = ctypes.create_unicode_buffer(32768)
    call = ctypes.WinDLL('shell32', use_last_error=True).SHGetFolderPathW
    call.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p]
    call.restype = ctypes.c_long
    require(call(None, 0x001C, None, 0, buffer) == 0, 'Cannot resolve actual Windows Local AppData')
    actual = Path(buffer.value)
    require(actual.is_absolute() and os.path.normcase(str(actual)) == os.path.normcase(os.environ['LOCALAPPDATA']),
            'LOCALAPPDATA must be the actual Windows profile location')
    regular(actual, directory=True)
    directory = actual / 'Intel Corporation'
    directory.mkdir(exist_ok=True)
    regular(directory, directory=True)
    path = directory / 'openvino_telemetry'
    if path.exists() or path.is_symlink():
        regular(path)
    return path


def establish_telemetry_consent():
    path = telemetry_file()
    path.write_bytes(b'0')
    require(regular(path).read_bytes() == b'0', 'Raw ASCII telemetry consent was not retained')


def check_telemetry_consent():
    require(regular(telemetry_file()).read_bytes() == b'0', 'Raw ASCII telemetry consent changed')


def scrubbed_environment(environment):
    # No Actions, GitHub, registry, cloud, signing or user credential reaches build children.
    return {key: value for key, value in environment.items()
            if not re.search(r'TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTHORIZATION|API_KEY|PRIVATE_KEY', key, re.I)
            and key.upper() not in {'NODE_AUTH_TOKEN', 'NPM_CONFIG_USERCONFIG', 'PIP_CONFIG_FILE'}}


def dependency_environment(environment):
    env = scrubbed_environment(environment)
    for key in list(env):
        if key.upper().startswith(('PIP_', 'NPM_CONFIG_')):
            del env[key]
    config = state_root() / 'configuration'
    config.mkdir(exist_ok=True)
    for filename in ('npm-user.conf', 'npm-global.conf'):
        path = config / filename
        if not path.exists():
            path.write_bytes(b'')
        require(regular(path).read_bytes() == b'', 'Package configuration changed')
    # os.devnull disables pip's global/site/user config loading. npm receives
    # two empty per-job files. No registry token or private mirror is forwarded.
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_INDEX_URL='https://pypi.org/simple',
        PIP_NO_INPUT='1', PIP_DISABLE_PIP_VERSION_CHECK='1',
        NPM_CONFIG_REGISTRY='https://registry.npmjs.org/',
        NPM_CONFIG_USERCONFIG=str(config / 'npm-user.conf'),
        NPM_CONFIG_GLOBALCONFIG=str(config / 'npm-global.conf'))
    canonical_temp = str(regular(Path(tempfile.gettempdir()).resolve(), directory=True))
    env.update(TEMP=canonical_temp, TMP=canonical_temp)
    return env


def run_owned(label, command, timeout, environment=None):
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,95}', label), 'Invalid fixed gate label')
    check_telemetry_consent()
    runner = load_source_module('owned_process')
    runner.validate_runtime()
    command = [str(value) for value in command]
    executable = shutil.which(command[0]) if not Path(command[0]).is_absolute() else command[0]
    require(executable and Path(executable).is_file(), 'Required command is unavailable')
    env = dependency_environment(dict(os.environ, **(environment or {})))
    command = runner.direct_child_command([executable, *command[1:]], env)
    changes = {key: None for key in os.environ if key not in env}
    changes.update(env)
    logs = state_root() / 'raw-logs'
    logs.mkdir(exist_ok=True)
    unique = label + '-' + uuid.uuid4().hex
    request = runner.Request(executable=command[0], arguments=command[1:], workingDirectory=str(ROOT),
        stdoutPath=str(logs / (unique + '.stdout.log')), stderrPath=str(logs / (unique + '.stderr.log')),
        timeoutSeconds=timeout, budgetLabel=label, environment=changes)
    completed = threading.Event()
    threading.Thread(target=runner.enforce_deadline, args=(completed, timeout + 40), daemon=True).start()
    print('PUBLIC_CI_GATE=' + label + ' START', flush=True)
    try:
        receipt = runner.supervise(request)
    finally:
        completed.set()
    write_json(logs / (unique + '.json'), receipt)
    if not (runner.terminal_receipt(receipt) and receipt['targetExitCode'] == 0 and receipt['outcome'] == 'completed'):
        save_failure_diagnostic(label, receipt, (Path(request.stdoutPath), Path(request.stderrPath)))
    require(runner.terminal_receipt(receipt) and receipt['targetExitCode'] == 0 and receipt['outcome'] == 'completed',
            'Required owned process gate failed')
    check_telemetry_consent()
    print('PUBLIC_CI_GATE=' + label + ' PASS', flush=True)
    return receipt


def bounded_log_tail(path):
    with regular(path).open('rb') as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 32768))
        return stream.read(32768).decode('utf-8', errors='replace')


DIAGNOSTIC_LIST_FIELDS = ('test_ids', 'failed_test_ids', 'source_locations', 'observed_exception_categories')
DIAGNOSTIC_WAIT_EXCEPTION_CATEGORIES = frozenset(('OwnedProcessWaitTimeout',
    'OwnedProcessWaitFailedInvalidHandle', 'OwnedProcessWaitFailedAccessDenied',
    'OwnedProcessWaitFailedOther', 'OwnedProcessWaitFailedErrorUnavailable',
    'OwnedProcessWaitUnexpected', 'OwnedProcessVerifiedEvidenceRejected',
    'OwnedProcessVerifiedSignalNotObservedInBudget'))
DIAGNOSTIC_EXCEPTION_CATEGORIES = frozenset(('AssertionError', 'TimeoutError', 'CancelledError',
    'OSError', 'RuntimeError', 'ValueError', 'TypeError', 'ImportError', 'ModuleNotFoundError')) | DIAGNOSTIC_WAIT_EXCEPTION_CATEGORIES


def diagnostic_test_symbols(manifest, root):
    symbols = set()
    for name in manifest:
        if name.endswith('.py') and ('test' in Path(name).name):
            data = regular(root / name).read_text(encoding='utf-8-sig')
            symbols.update(re.findall(r'\bdef\s+(test_[A-Za-z0-9_]{1,160})\s*\(', data))
    return symbols


def parse_diagnostic_tails(tails, manifest, root=ROOT, exception_info=None):
    """Observed bounded-tail context; only exact unittest headers identify failures.

    Exception categories can belong to handled/passing context, not the root cause.
    Omission counts cover unique allowlisted items in these tails and the supplied
    active traceback, not earlier logs or chained exceptions. Never format an
    exception or inspect its message, arguments, locals or custom type name.
    """
    symbols = diagnostic_test_symbols(manifest, root)
    found_tests, failed_tests, locations, categories = set(), set(), set(), set()
    ansi_color = re.compile(r'\x1b\[[0-9;]*m')
    failure_header = re.compile(r'^(?:FAIL|ERROR): (test_[A-Za-z0-9_]{1,160}) '
        r'\([A-Za-z_][A-Za-z0-9_.]*\)(?:$| )')
    exception_header = re.compile(r'^(?:(?:builtins|asyncio\.exceptions|concurrent\.futures\._base)\.)?('
        + '|'.join(sorted(DIAGNOSTIC_EXCEPTION_CATEGORIES)) + r')(?::(?: |$)|$)')
    # Direct script execution has no module prefix. These are the only native
    # fixture module spellings accepted when unittest imports the same source.
    wait_exception_header = re.compile(r'^(?:scripts\.tests\.)?test_owned_process_windows_r64\.('
        + '|'.join(sorted(DIAGNOSTIC_WAIT_EXCEPTION_CATEGORIES)) + r')(?::(?: |$)|$)')
    prefix = str(root).replace('\\', '/').rstrip('/') + '/'
    patterns = (re.compile(re.escape(prefix) + r'([A-Za-z0-9_./-]+)\", line ([1-9][0-9]{0,6})', re.I),
                re.compile(re.escape(prefix) + r'([A-Za-z0-9_./-]+):([1-9][0-9]{0,6})(?::|\b)', re.I),
                re.compile(r'File \"([A-Za-z0-9_./-]+)\", line ([1-9][0-9]{0,6})'))
    for tail in tails:
        for line in tail.splitlines():
            line = ansi_color.sub('', line)
            for name in re.findall(r'\btest_[A-Za-z0-9_]{1,160}\b', line):
                if name in symbols:
                    found_tests.add(name)
            failed = failure_header.match(line)
            if failed and failed[1] in symbols:
                failed_tests.add(failed[1])
            category = exception_header.match(line) or wait_exception_header.match(line)
            if category:
                categories.add(category[1])
            normalized = line.replace('\\', '/')
            for pattern in patterns:
                for name, number in pattern.findall(normalized):
                    if name in manifest:
                        locations.add((name, int(number)))
    if exception_info is not None:
        require(type(exception_info) is tuple and len(exception_info) == 3,
                'Invalid active exception context')
        kind, _, trace = exception_info
        if kind is not None:
            # Identity checks reject custom classes, including built-in lookalikes.
            category = next((name for expected, name in (
                (AssertionError, 'AssertionError'), (TimeoutError, 'TimeoutError'),
                (OSError, 'OSError'), (RuntimeError, 'RuntimeError'),
                (ValueError, 'ValueError'), (TypeError, 'TypeError'),
                (ImportError, 'ImportError'), (ModuleNotFoundError, 'ModuleNotFoundError'))
                if kind is expected), None)
            if category is not None:
                categories.add(category)
            frames = 0
            while trace is not None:
                frames += 1
                require(type(trace) is TracebackType and frames <= 65536,
                        'Active traceback exceeds diagnostic bound')
                try:
                    name = Path(trace.tb_frame.f_code.co_filename).relative_to(root).as_posix()
                except ValueError:
                    name = None
                if name in manifest and 0 < trace.tb_lineno < 10000000:
                    locations.add((name, trace.tb_lineno))
                trace = trace.tb_next
        require(len(locations) <= 65536, 'Diagnostic locations exceed omission bound')
    items = {'test_ids': sorted(found_tests), 'failed_test_ids': sorted(failed_tests),
        'source_locations': [{'file': name, 'line': number} for name, number in sorted(locations)],
        'observed_exception_categories': sorted(categories)}
    return {**{field: values[:20] for field, values in items.items()},
            **{field + '_omitted': max(0, len(values) - 20) for field, values in items.items()}}


def save_failure_diagnostic(label, receipt=None, paths=(), *, exception_info=None):
    """Best-effort diagnostics must never turn a failed gate into success."""
    try:
        outcomes = {'completed', 'target-exited-nonzero', 'execution-timeout', 'cancelled',
            'cancelled-before-launch', 'descendant-drain-timeout', 'supervision-error', 'log-size-limit'}
        record = {'gate': label, 'outcome': 'validation-failed', 'exit_code': None,
                  'diagnostic_parse_succeeded': False,
                  **{field: [] for field in DIAGNOSTIC_LIST_FIELDS},
                  **{field + '_omitted': 0 for field in DIAGNOSTIC_LIST_FIELDS}}
        require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,95}', label), 'Invalid diagnostic gate')
        if receipt is not None:
            record['outcome'] = receipt.get('outcome') if receipt.get('outcome') in outcomes else 'supervision-error'
            code = receipt.get('targetExitCode')
            record['exit_code'] = code if type(code) is int and -(2**31) <= code < 2**31 else None
        try:
            tails = [bounded_log_tail(path) for path in paths]
            record.update(parse_diagnostic_tails(tails, read_json(ROOT / 'SOURCE_SHA256.json'), ROOT,
                                                exception_info))
            record['diagnostic_parse_succeeded'] = True
        except Exception:
            pass
        write_json(state_root() / 'failures' / (uuid.uuid4().hex + '.json'), record)
    except Exception:
        pass


def failure_diagnostics():
    directory = state_root() / 'failures'
    if not directory.exists():
        return {'records': [], 'records_omitted': 0, 'detail_items_omitted': 0}
    regular(directory, directory=True)
    result = []
    files = sorted(directory.iterdir())
    manifest = read_json(ROOT / 'SOURCE_SHA256.json')
    symbols = diagnostic_test_symbols(manifest, ROOT)
    fields = {'gate', 'outcome', 'exit_code', 'diagnostic_parse_succeeded',
        *DIAGNOSTIC_LIST_FIELDS, *(field + '_omitted' for field in DIAGNOSTIC_LIST_FIELDS)}
    for path in files[:32]:
        require(re.fullmatch('[a-f0-9]{32}\\.json', path.name), 'Unexpected diagnostic file')
        value = read_json(path)
        require(type(value) is dict and set(value) == fields,
                'Unexpected diagnostic field')
        require(type(value['gate']) is str and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,95}', value['gate']), 'Invalid diagnostic gate')
        require(type(value['outcome']) is str and value['outcome'] in {'completed','target-exited-nonzero','execution-timeout','cancelled',
            'cancelled-before-launch','descendant-drain-timeout','supervision-error','log-size-limit','validation-failed'},
            'Invalid diagnostic outcome')
        require(value['exit_code'] is None or (type(value['exit_code']) is int and -(2**31) <= value['exit_code'] < 2**31), 'Invalid diagnostic exit')
        require(type(value['diagnostic_parse_succeeded']) is bool, 'Invalid diagnostic parser status')
        for field in DIAGNOSTIC_LIST_FIELDS:
            rows, omitted = value[field], value[field + '_omitted']
            require(type(rows) is list and len(rows) <= 20, 'Invalid diagnostic array')
            require(type(omitted) is int and 0 <= omitted <= 65536 and
                    (omitted == 0 or len(rows) == 20), 'Invalid diagnostic omission count')
            require(value['diagnostic_parse_succeeded'] or (not rows and omitted == 0),
                    'Failed parser cannot supply diagnostic detail')
            if field != 'source_locations':
                allowed = DIAGNOSTIC_EXCEPTION_CATEGORIES if field == 'observed_exception_categories' else symbols
                require(all(type(row) is str and row in allowed for row in rows) and
                        rows == sorted(set(rows)) and len(rows) + omitted <= len(allowed),
                        'Unsealed or unordered diagnostic symbol')
        for row in value['source_locations']:
            require(type(row) is dict and set(row) == {'file','line'} and type(row['file']) is str and
                    row['file'] in manifest and type(row['line']) is int and
                    0 < row['line'] < 10000000, 'Invalid source diagnostic location')
        locations = [(row['file'], row['line']) for row in value['source_locations']]
        require(locations == sorted(set(locations)), 'Unordered diagnostic locations')
        result.append(value)
    bounded = bound_diagnostic_records(result)
    bounded['records_omitted'] += max(0, len(files) - 32)
    return bounded


def bound_diagnostic_records(records, budget=24576):
    """Bound exported detail; record omissions are counted separately from items.

    detail_items_omitted includes parser and size omissions in inspected records.
    """
    result = {'records': [], 'records_omitted': 0, 'detail_items_omitted': 0}
    encoded_size = lambda value: len(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2).encode())
    for original in records:
        record = dict(original, **{field: list(original[field]) for field in DIAGNOSTIC_LIST_FIELDS})
        result['detail_items_omitted'] += sum(record[field + '_omitted'] for field in DIAGNOSTIC_LIST_FIELDS)
        while encoded_size(record) > 4096 and any(record[field] for field in DIAGNOSTIC_LIST_FIELDS):
            # Preserve actual failure names ahead of generic context when space is tight.
            field = next(field for field in ('source_locations', 'test_ids',
                'observed_exception_categories', 'failed_test_ids') if record[field])
            record[field].pop()
            record[field + '_omitted'] += 1
            result['detail_items_omitted'] += 1
        require(encoded_size(record) <= 4096, 'Diagnostic record byte bound exceeded')
        if encoded_size(dict(result, records=result['records'] + [record])) > budget:
            result['records_omitted'] += 1
        else:
            result['records'].append(record)
    require(encoded_size(result) <= budget, 'Diagnostic byte bound exceeded')
    return result


def verify_run_state():
    run = actual_run()
    state = read_json(state_root() / 'state.json')
    require(same_json(state['run'], run) and same_json(state['source_provenance'], source_identity()), 'Stale or changed source/run identity')
    require(re.fullmatch(r'[a-f0-9]{32}', state['nonce']) and type(state['started_ns']) is int, 'Invalid run freshness')
    check_telemetry_consent()
    return state


def bind_native(proof, identity):
    require(proof.get('source_commit') == identity['source_commit'] and
            proof.get('github_sha') == identity['source_commit'] and same_json(proof.get('source_provenance'), identity),
            'Native receipt is not bound to the full current source')


def validate_source_build(state):
    identity = source_identity()
    require(same_json(identity, state['source_provenance']), 'Build source changed')
    output = ROOT / 'installer-output'
    proof = read_json(output / 'build-source.json')
    bind_native(proof, identity)
    require(proof.get('verified') is True and proof.get('revision') == 'stability-r94' and
            proof.get('version') == '3.0.4' and proof.get('manifestSha256') == identity['source_manifest_sha256'],
            'Full build source gate is not current')
    installer = output / 'Juxin-IG-Audience-Collector-NewGen-Setup-3.0.4-x64.exe'
    require(list(output.glob('Juxin-IG-Audience-Collector-NewGen-Setup-3.0.4-*.exe')) == [installer],
            'Current version must have exactly the selected NSIS installer')
    marker = dict(line.split('=', 1) for line in regular(output / 'LATEST_SUCCESS.txt').read_text(encoding='utf-8-sig').splitlines() if '=' in line)
    require(marker.get('TYPE') == 'INSTALLER' and marker.get('VERSION') == '3.0.4' and
            Path(marker.get('PATH', '')).resolve() == installer.resolve() and
            marker.get('SHA256') == digest(installer) and marker.get('BROWSER_MODE') == 'installed-chrome',
            'Fresh NSIS marker is missing or invalid')
    started = read_json(state_root() / 'build-start.json')['started_ns']
    require(installer.stat().st_mtime_ns >= started and (output / 'LATEST_SUCCESS.txt').stat().st_mtime_ns >= started,
            'Installer predates this full build')
    sidecar = Path(str(installer) + '.sha256')
    require(Path(marker.get('SHA256_PATH', '')).resolve() == sidecar.resolve() and
            regular(sidecar).read_text(encoding='utf-8-sig').strip() == digest(installer) + ' *' + installer.name and
            sidecar.stat().st_mtime_ns >= started, 'Installer sidecar is stale or mismatched')
    policy = read_json(ROOT / 'build/browsers/juxin-runtime-requirement.json')
    require(policy.get('mode') == 'installed-chrome-required', 'Browser policy mismatch')
    return {'installer_sha256': digest(installer), 'build_source_sha256': digest(output / 'build-source.json'),
            'installer_sidecar_sha256': digest(sidecar),
            'browser_policy_sha256': digest(ROOT / 'build/browsers/juxin-runtime-requirement.json')}


def checked_evidence(path):
    path = regular(path)
    require(path.stat().st_size <= 32 * 1024 * 1024, 'Raw proof exceeds local validation bound')
    return read_json(path) if path.suffix == '.json' else path.read_bytes()


def preflight_early_evidence(*, include_aggregate=False):
    verify_run_state()
    started = read_json(state_root() / 'early-start.json')['started_ns']
    for name in EARLY_FILES:
        if name == 'r64-early-verification.json' and not include_aggregate:
            continue
        path = ROOT / 'installer-output' / name
        checked_evidence(path)
        require(path.stat().st_mtime_ns >= started, 'Early proof predates this stage')
    host = ROOT / 'dist-electron/embedded-browser.js'
    checked_evidence(host)
    require(host.stat().st_mtime_ns >= started, 'Early compiled host predates this stage')
    manifest = read_json(ROOT / 'SOURCE_SHA256.json')
    proof = read_json(ROOT / 'installer-output/r64-recovery-ui-native-proof.json')
    for name in proof.get('source_sha256', {}):
        require(name in manifest, 'Early native source reference is outside the sealed manifest')
        checked_evidence(ROOT / name)


def preflight_installed_evidence():
    """Bound all legacy validator inputs before any legacy path/read is used."""
    state = verify_run_state()
    output = ROOT / 'installer-output'
    current = tuple(name for name in EARLY_FILES if name not in (
        'r64-early-verification.json', 'r62-posting-viewport-native.json', 'r62-posting-viewport-native.png')) + FINAL_IMAGES + (
        'build-source.json', 'embedded-browser-check.json', 'installed-verification.json',
        'installed-scale-verification.json', 'installed-recovery-r64.json',
        'parent-reels-fixtures/parent-reels-proof-r98.json', 'r6-posting-stress.json', 'r6-posting-stress-bounds.json')
    started = read_json(state_root() / 'build-start.json')['started_ns']
    for name in current:
        checked_evidence(output / name)
        require((output / name).stat().st_mtime_ns >= started, 'Current raw proof predates the fresh build')
    for name in EARLY_FILES:
        checked_evidence(state_root() / 'early' / name)
    # The native cleanup oracle also hashes generated compiled host bytes.
    checked_evidence(ROOT / 'dist-electron/embedded-browser.js')
    manifest = read_json(ROOT / 'SOURCE_SHA256.json')
    for base in (output, state_root() / 'early'):
        proof = read_json(base / 'r64-recovery-ui-native-proof.json')
        for name in proof.get('source_sha256', {}):
            require(name in manifest, 'Native source reference is outside the sealed public manifest')
            checked_evidence(ROOT / name)
    installed = read_json(output / 'installed-verification.json')
    expected = Path(os.environ['LOCALAPPDATA']) / 'Programs/juxin-ig-audience-collector-newgen'
    actual = regular(Path(installed['installed_root']), directory=True)
    require(os.path.normcase(os.path.abspath(actual)) == os.path.normcase(os.path.abspath(expected)),
            'Installed proof selects another product directory')
    product = read_json(ROOT / 'package.json')['build']['productName']
    for path in (actual / (product + '.exe'), actual / 'resources/backend/collector_core/collector_core.exe',
                 actual / 'resources/app.asar'):
        regular(path)
    checked_evidence(actual / 'resources/browsers/juxin-runtime-requirement.json')
    return state
