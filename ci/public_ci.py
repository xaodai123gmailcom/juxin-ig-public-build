"""Execute real Windows gates; export only freshly constructed summary fields."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import runpy
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_ci_runtime import collect_inventory, inventory_digest
from public_ci_common import (ROOT, STAGES, PUBLIC_FILES, EARLY_FILES, actual_run, bind_native,
    check_telemetry_consent, digest, establish_telemetry_consent, read_json,
    regular, require, run_owned, source_identity, state_root, validate_source_build,
    verify_run_state, write_json, save_failure_diagnostic, failure_diagnostics)


def initialize():
    run = actual_run()
    state = state_root()
    require(not state.exists(), 'Run state must be new for this attempt')
    state.mkdir()
    require(not (ROOT / 'installer-output').exists(), 'Initial checkout must contain no generated proof')
    establish_telemetry_consent()
    identity = source_identity()
    write_json(state / 'state.json', {'run': run, 'source_provenance': identity,
        'nonce': uuid.uuid4().hex, 'started_ns': time.time_ns(), 'telemetry_ascii0_verified': True})


def powershell(script):
    return [str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'),
            '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(ROOT / script)]


def contracts():
    run_owned('runner-existing-prerequisites', powershell('ci/public_ci_runner_prerequisites.ps1'), 90)
    run_owned('powershell7-native-command-logging', ['pwsh.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
              '-File', str(ROOT / 'ci/public_ci_pwsh_logging.ps1')], 180)
    run_owned('contract-flat-git-source-binding', [sys.executable, '-I', '-B', '-X', 'utf8',
              str(ROOT / 'scripts/tests/test_ci_source_binding.py'), '-v'], 180)
    for name in ('test-r63-native-proof.py', 'test-r63-upgrade-proof.py',
                 'test-r64-crop-proof.py', 'test-r64-recovery-ui-proof.py', 'test_public_ci.py',
                 'test_public_ci_runtime.py', 'test_public_ci_unicode.py', 'test_public_build_contract.py'):
        run_owned('contract-' + name.replace('_', '-').replace('.', '-'),
                  [sys.executable, '-I', '-X', 'utf8', str(ROOT / 'ci' / name), '-v'], 180)
    run_owned('unicode-resource-copy', ['node', '--test', 'scripts/tests/portable_resources_r94.test.mjs'], 180)


def early():
    run_owned('early-existing-prerequisites', powershell('ci/public_ci_runner_prerequisites.ps1'), 90)
    run_owned('early-create-venv', [sys.executable, '-I', '-X', 'utf8', '-m', 'venv', '.venv'], 180)
    python = str(ROOT / '.venv/Scripts/python.exe')
    run_owned('early-python-dependencies', [python, '-I', '-X', 'utf8', 'scripts/install_python_dependencies.py',
               '--project-root', str(ROOT)], 1800)
    run_owned('unicode-source-runtime', [sys.executable, '-I', '-X', 'utf8',
              str(ROOT / 'ci/public_ci_unicode.py'), 'source'], 3600)
    run_owned('early-node-dependencies', ['npm.cmd', 'ci', '--include=dev', '--no-audit', '--no-fund'], 900)
    for target in ('build:ui', 'build:electron'):
        run_owned('early-' + target.replace(':', '-'), ['npm.cmd', 'run', target], 600)
    for pattern in ('test_python_environment.py', 'test_timezone_data_r57.py'):
        run_owned('early-' + pattern.replace('_', '-').replace('.', '-'),
            [python, '-I', '-X', 'utf8', '-m', 'unittest', 'discover', '-s', 'scripts/tests', '-p', pattern, '-v'], 600)
    # Additive public configuration regression; never substitutes for original gates.
    run_owned('public-cloud-configuration', [python, '-I', '-X', 'utf8', 'scripts/run_backend_tests.py',
        '-p', 'test_cloud_configuration_public.py', '--case-timeout', '180', '-v'], 600)
    run_owned('early-all-required-regressions', [sys.executable, '-I', '-X', 'utf8', 'ci/public_ci_early.py'], 3600)
    return {name: digest(state_root() / 'early' / name) for name in EARLY_FILES}


def build():
    state = verify_run_state()
    require(read_json(state_root() / 'early-result.json')['status'] == 'passed', 'Early gates did not pass')
    require(not (ROOT / 'installer-output').exists(), 'Full build requires the fresh second checkout')
    run_owned('build-existing-prerequisites', powershell('ci/public_ci_runner_prerequisites.ps1'), 90)
    run_owned('full-original-windows-build', powershell('scripts/build_windows.ps1') +
              ['-BrowserMode', 'installed-chrome'], 10800)
    validate_source_build(state)
    run_owned('unicode-copied-frozen-runtime', [sys.executable, '-I', '-X', 'utf8',
              str(ROOT / 'ci/public_ci_unicode.py'), 'frozen'], 900)
    return validate_source_build(state)


def installed():
    state = verify_run_state()
    require(read_json(state_root() / 'build-result.json')['status'] == 'passed', 'Full build did not pass')
    validate_source_build(state)
    run_owned('actual-installed-product-acceptance', powershell('ci/public_ci_verify_installed.ps1'), 3600)
    runpy.run_path(str(ROOT / 'ci/public_ci_validate_installed.py'))
    return installed_hashes()


def installed_inventory():
    installed = read_json(ROOT / 'installer-output/installed-verification.json')
    installation = Path(installed['installed_root'])
    expected = Path(os.environ['LOCALAPPDATA']) / 'Programs/juxin-ig-audience-collector-newgen'
    require(installation.resolve() == expected.resolve(), 'Unexpected installed product root')
    return collect_inventory(installation, ROOT)


def installed_hashes(runtime_inventory=None):
    output = ROOT / 'installer-output'
    installed = read_json(output / 'installed-verification.json')
    installation = Path(installed['installed_root'])
    expected = Path(os.environ['LOCALAPPDATA']) / 'Programs/juxin-ig-audience-collector-newgen'
    require(installation.resolve() == expected.resolve(), 'Unexpected installed product root')
    product = read_json(ROOT / 'package.json')['build']['productName']
    return {'installed_desktop_sha256': digest(installation / (product + '.exe')),
        'installed_core_sha256': digest(installation / 'resources/backend/collector_core/collector_core.exe'),
        'installed_app_asar_sha256': digest(installation / 'resources/app.asar'),
        'installed_acceptance_sha256': digest(output / 'installed-verification.json'),
        'installed_scale_sha256': digest(output / 'installed-scale-verification.json'),
        'installed_recovery_sha256': digest(output / 'installed-recovery-r64.json'),
        'runtime_inventory_sha256': inventory_digest(installed_inventory() if runtime_inventory is None else runtime_inventory)}


def run_stage(name, action):
    state = verify_run_state()
    require(name in STAGES, 'Unknown stage')
    write_json(state_root() / (name + '-start.json'), {'started_ns': time.time_ns(), 'nonce': state['nonce']})
    result = {'stage': name, 'status': 'failed', 'nonce': state['nonce'], 'hashes': {}}
    try:
        hashes = action()
        verify_run_state()
        result.update(status='passed', hashes=hashes or {})
    finally:
        if result['status'] != 'passed':
            save_failure_diagnostic(name + '-validation')
        result['finished_ns'] = time.time_ns()
        write_json(state_root() / (name + '-result.json'), result)


def phase_status(state, name):
    path = state_root() / (name + '-result.json')
    if not path.exists():
        return 'interrupted' if (state_root() / (name + '-start.json')).exists() else 'not-run'
    result = read_json(path)
    require(result.get('nonce') == state['nonce'] and result.get('stage') == name and
            result.get('status') in ('passed', 'failed'), 'Stage freshness mismatch')
    return result['status']


def public_identity(identity):
    # Construct an explicit field allowlist. Never export a copied raw receipt.
    selected = {key: identity[key] for key in ('schema', 'mode', 'representation', 'repository',
        'source_commit', 'source_manifest_sha256', 'source_marker_sha256', 'git_tree',
        'git_blob_paths_sha256', 'tracked_files_verified', 'effective_files_verified')}
    require(selected['schema'] == 2 and selected['representation'] == 'public-sanitized-source', 'Wrong public identity')
    return selected


def export():
    state = verify_run_state()
    statuses = {name: phase_status(state, name) for name in STAGES}
    source_hashes = {}
    installed_evidence = {}
    runtime_inventory = {}
    if statuses['build'] == 'passed':
        source_hashes = validate_source_build(state)
        require(source_hashes == read_json(state_root() / 'build-result.json')['hashes'], 'Build bytes changed after acceptance')
    if statuses['installed'] == 'passed':
        runpy.run_path(str(ROOT / 'ci/public_ci_validate_installed.py'))
        runtime_inventory = installed_inventory()
        installed_evidence = installed_hashes(runtime_inventory)
        require(installed_evidence == read_json(state_root() / 'installed-result.json')['hashes'], 'Installed bytes changed after acceptance')
    common = {'schema': 1, 'run': state['run'], 'source': public_identity(source_identity()),
        'run_nonce': state['nonce'], 'started_ns': state['started_ns'], 'exported_ns': time.time_ns(),
        'execution': 'actual-github-hosted-windows', 'evidence_kind': 'synthetic-offline-summary',
        'telemetry_ascii0_verified': True, 'binary_exported': False,
        'raw_logs_exported': False, 'profile_or_source_exported': False}
    destination = state_root() / 'public'
    destination.mkdir(exist_ok=False)
    write_json(destination / 'run-summary.json', dict(common, stages=statuses, failure_diagnostics=failure_diagnostics(),
        all_required_stages_passed=all(value == 'passed' for value in statuses.values())))
    write_json(destination / 'source-build-proof.json', dict(common, stage='source-build',
        status=statuses['build'], hashes=source_hashes,
        original_entry='scripts/build_windows.ps1', browser_mode='installed-chrome'))
    write_json(destination / 'installed-acceptance-proof.json', dict(common, stage='installed-acceptance',
        status=statuses['installed'], hashes=installed_evidence, runtime_inventory=runtime_inventory,
        installer_sha256=source_hashes.get('installer_sha256'),
        prior_early_gates=statuses['early'], source_build=statuses['build']))
    require(set(path.name for path in destination.iterdir()) == set(PUBLIC_FILES), 'Unexpected export entry')
    for name in PUBLIC_FILES:
        require(regular(destination / name).stat().st_size <= 65536, 'Export exceeds size bound')
    print('PUBLIC_CI_SYNTHETIC_EXPORT=PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('initialize', *STAGES, 'export'))
    stage = parser.parse_args().stage
    try:
        if stage in STAGES:
            run_stage(stage, globals()[stage])
        else:
            globals()[stage]()
    except Exception:
        # Raw exceptions can contain runner paths or application data. They are
        # retained in bounded runner-only child logs, never printed or uploaded.
        print('PUBLIC_CI_STAGE=' + stage + ' FAILED', flush=True)
        return 1
    print('PUBLIC_CI_STAGE=' + stage + ' PASSED', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
