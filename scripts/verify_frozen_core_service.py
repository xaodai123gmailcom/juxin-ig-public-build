"""Launch the packaged Core with isolated data, verify HTTP auth and shutdown."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import math
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


IS_WINDOWS = os.name == 'nt'


def read_json_response(opener, request, *, timeout):
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as error:
        # HTTPError owns a response too. Consume and close expected 401/409
        # replies so Windows does not reset a socket with unread response data.
        # Preserve the HTTPError/status contract used by the negative checks.
        try:
            error.read()
        finally:
            error.close()
        raise
    with response:
        return response.status, json.load(response)


def direct_child_command(command: list[str], environment: dict) -> list[str]:
    """Own the interpreter, not the Windows venv redirector waiting for it.

    CPython's multiprocessing.popen_spawn_win32 uses this same base-executable
    plus __PYVENV_LAUNCHER__ pair. It retains the venv's sys.prefix/dependencies
    while making Popen.pid the actual interpreter that terminate()/wait() own.
    Only our exact current interpreter is eligible; frozen EXEs stay untouched.
    """
    result = list(command)
    base = getattr(sys, '_base_executable', sys.executable)
    same_path = lambda a, b: os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    if (IS_WINDOWS and result and base and not getattr(sys, 'frozen', False)
            and same_path(result[0], sys.executable) and not same_path(base, sys.executable)):
        result[0] = base
        environment['__PYVENV_LAUNCHER__'] = sys.executable
    return result


def cleanup_probe_directory(directory) -> None:
    # Handles are closed and our child has exited before this point. Windows
    # scanners may still hold a short-lived share lock: bounded retries only.
    for attempt, delay in enumerate((.05, .1, .2, .4, .8, 0)):
        try:
            directory.cleanup()
            return
        except OSError as error:
            if getattr(error, 'winerror', None) not in {32, 33} or attempt == 5:
                raise
            time.sleep(delay)


@contextmanager
def probe_directory(*, prefix='Juxin-CoreSmoke-'):
    directory = tempfile.TemporaryDirectory(prefix=prefix)
    primary = None
    try:
        yield directory.name
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            cleanup_probe_directory(directory)
        except OSError as cleanup_error:
            if primary is not None:
                # Preserve both causes; a failed cleanup must never turn an
                # expected negative test into a passing test or hide its cause.
                raise BaseExceptionGroup('Probe and temporary cleanup both failed',
                                         [primary, cleanup_error]) from None
            raise


def probe_work_report_summary(request, *, session: str, result_count: int) -> dict:
    """Check exact R6 report totals/wire on the untouched snapshot-scale fixture.

    That fixture creates one result per identity and overlapping review/exclusion
    copies, all on October 1. Its split-history rows are deliberately not marked
    completed; no follow successes or monitor additions are inserted. Therefore
    the four exact report totals are known independently of either report path.
    Latencies are observations, not speed gates. HTTP deadlines only bound hangs.
    """
    payload = {'kind': 'activity', 'platform': 'instagram',
               'start': '2026-10-01T00:00:00+00:00', 'end': '2026-10-02T00:00:00+00:00'}
    expected = {'collection': result_count, 'follow': 0, 'split': 0, 'added': 0}
    summaries = []
    times = []
    for _ in range(2):
        started = time.monotonic()
        status, summary = request('/api/reports/query', method='POST', session=session, budget=180,
                                  payload={**payload, 'summary_only': True})
        times.append(round(time.monotonic() - started, 6))
        if (status != 200 or set(summary) != {'platform', 'start', 'end', 'totals'}
                or any(summary.get(key) != payload[key] for key in ('platform', 'start', 'end'))
                or not isinstance(summary.get('totals'), dict) or set(summary['totals']) != set(expected)
                or any(type(summary['totals'].get(key)) is not int or summary['totals'][key] != value
                       for key, value in expected.items())):
            raise RuntimeError('Installed work-report summary changed the exact fixture totals or wire schema')
        summaries.append(summary)
    if summaries[0] != summaries[1]:
        raise RuntimeError('Installed work-report repeated summary is inconsistent')
    started = time.monotonic()
    status, full = request('/api/reports/query', method='POST', session=session, budget=180, payload=payload)
    full_seconds = round(time.monotonic() - started, 6)
    if (status != 200 or not isinstance(full.get('rows'), list)
            or type(full.get('unattributed')) is not int
            or any(full.get(key) != payload[key] for key in ('platform', 'start', 'end'))
            or not isinstance(full.get('totals'), dict)
            or {'posting', 'confirmed_posting'}.intersection(full['totals'])
            or any({'posting', 'confirmed_posting'}.intersection(row) for row in full['rows'])
            or any(type(full['totals'].get(key)) is not int or full['totals'][key] != value
                   for key, value in expected.items())):
        raise RuntimeError('Installed work-report legacy path disagrees with exact summary totals')
    encoded_size = lambda value: len(json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))
    full_bytes, summary_bytes = encoded_size(full), encoded_size(summaries[0])
    if summary_bytes >= full_bytes:
        raise RuntimeError('Installed work-report summary did not remove detailed row payloads')
    return {'verified': True, 'summary_legacy_totals_equal': True, 'fixture_totals_verified': True,
            'repeated_summary_equal': True, 'summary_only_wire': True, 'totals': expected,
            'summary_first_seconds': times[0], 'summary_repeat_seconds': times[1],
            'legacy_full_seconds': full_seconds, 'summary_bytes': summary_bytes, 'legacy_bytes': full_bytes,
            'collected': result_count, 'synthetic': True, 'live_accounts_tested': False,
            'user_data_touched': False}


def probe_report_index_totals(request, *, session, expected):
    """Four positive metric oracles, including the legacy CSV route."""
    payload = {'kind': 'activity', 'platform': 'instagram',
               'start': expected['start'], 'end': expected['end']}
    timings = []
    for summary_only in (True, True, False):
        started = time.monotonic()
        status, report = request('/api/reports/query', method='POST', session=session,
            budget=180, payload={**payload, 'summary_only': summary_only})
        timings.append(round(time.monotonic() - started, 6))
        if (status != 200 or not isinstance(report.get('totals'), dict)
                or {'posting', 'confirmed_posting'}.intersection(report['totals'])
                or any({'posting', 'confirmed_posting'}.intersection(row) for row in report.get('rows', []))
                or any(type(report['totals'].get(key)) is not int or report['totals'][key] != value
                       for key, value in expected['totals'].items())):
            raise RuntimeError('Installed legacy-index upgrade changed the four positive report totals')
        if summary_only and (set(report['totals']) != set(expected['totals']) or 'rows' in report):
            raise RuntimeError('Installed legacy-index summary response has the wrong wire shape')
        if not summary_only and (not isinstance(report.get('rows'), list)
                or any(sum(row.get(key, 0) for row in report['rows']) != value
                       for key, value in expected['totals'].items())):
            raise RuntimeError('Installed legacy-index CSV rows disagree with the four report totals')
    return {'summary_first_seconds': timings[0], 'summary_repeat_seconds': timings[1],
            'legacy_full_seconds': timings[2]}


REMOVED_POSTING_ENDPOINTS = (
    {'path': '/api/posting/snapshot', 'method': 'GET'},
    {'path': '/api/posting/command', 'method': 'POST'},
    {'path': '/api/internal/integrations/pexels', 'method': 'POST'},
)


def probe_posting_removed(request, *, session=None):
    """Actual selected Core HTTP route absence; a preload denial is insufficient."""
    for endpoint in REMOVED_POSTING_ENDPOINTS:
        options = {'method': endpoint['method'], 'session': session, 'budget': 10}
        if endpoint['method'] == 'POST':
            options['payload'] = {'action': 'start', 'job_ids': ['removed-offline-fixture']}
        try:
            status, body = request(endpoint['path'], **options)
        except HTTPError as error:
            try:
                if error.code != 404:
                    raise RuntimeError('Removed posting endpoint did not return HTTP 404') from error
            finally:
                error.close()
        else:
            if status != 404:
                raise RuntimeError('Removed posting endpoint remains available: ' + endpoint['path'])
    return {'verified': True, 'http_status': 404, 'endpoints': list(REMOVED_POSTING_ENDPOINTS)}


def probe_core(command: list[str], log_path: Path, *, timeout=90.0,
               shutdown_timeout=20.0, expected_revision='stability-r94', pure_ig_smoke=False,
               snapshot_scale_smoke=False, snapshot_result_count=441552,
               snapshot_identity_count=602831) -> dict:
    if any(not math.isfinite(value) or value <= 0 for value in (timeout, shutdown_timeout)):
        raise ValueError('Core probe deadlines must be finite and positive')
    log_path = log_path.absolute()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(('IGAC_', 'COLLECTOR_CORE_', 'PLAYWRIGHT_', 'PYTHON'))
                   and key.upper() not in {'OPENVINO_LIB_PATHS', '__PYVENV_LAUNCHER__'}}
    environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    if os.name == 'nt':
        windows = Path(os.environ['SystemRoot'])
        environment['PATH'] = os.pathsep.join(map(str, (windows / 'System32', windows, windows / 'System32/Wbem')))
    token = secrets.token_hex(32)
    opener = build_opener(ProxyHandler({}))
    scale_proof = None
    with probe_directory() as temporary:
        data = Path(temporary)
        database = data / 'collector.sqlite3'
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        environment.update(IGAC_STARTUP_TOKEN=token, IGAC_DATA_DIR=str(data),
                           IGAC_DB_PATH=str(database), IGAC_PORT=str(port),
                           IGAC_BITBROWSER_URL='http://127.0.0.1:1')
        base_url = f'http://127.0.0.1:{port}'
        def request(path, *, authorized=True, method='GET', budget=1.0, payload=None, session=None):
            headers = {'X-Startup-Token': token} if authorized else {}
            if session:
                headers['Authorization'] = 'Bearer ' + session
            if payload is not None:
                headers['Content-Type'] = 'application/json'
            req = Request(base_url + path, headers=headers, method=method,
                          data=json.dumps(payload).encode() if payload is not None else None)
            return read_json_response(opener, req, timeout=max(.01, budget))
        with log_path.open('wb') as log:
            child_command = direct_child_command(command, environment)
            process = subprocess.Popen([*child_command, '--host', '127.0.0.1', '--port', str(port)],
                                       cwd=data, env=environment, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + timeout
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f'Core exited before readiness (exit {process.returncode}); see {log_path}')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError(f'Core startup timed out; see {log_path}')
                    try:
                        status, health = request('/api/health', budget=min(1., remaining))
                    except HTTPError as error:
                        raise RuntimeError(f'Core health rejected fresh startup token (HTTP {error.code})') from error
                    except (URLError, TimeoutError, ConnectionError):
                        time.sleep(min(.1, max(0, deadline - time.monotonic())))
                        continue
                    if (status != 200 or health.get('status') != 'ready'
                            or health.get('source_revision') != expected_revision
                            or health.get('database') != 'ok'):
                        raise RuntimeError(f'Core health payload is invalid: {health!r}')
                    break
                try:
                    request('/api/health', authorized=False)
                except HTTPError as error:
                    if error.code != 401:
                        raise RuntimeError('Core authentication returned an unexpected status') from error
                else:
                    raise RuntimeError('Core accepted a request without its startup token')
                if pure_ig_smoke or snapshot_scale_smoke:
                    status, registered = request('/api/session/register', method='POST', budget=10,
                        payload={'username': 'install-smoke', 'password': secrets.token_hex(24)})
                    session = registered.get('session_token')
                    if status != 201 or not session:
                        raise RuntimeError('Installed Core test session could not be created')
                removed_proof = probe_posting_removed(request, session=locals().get('session'))
                if snapshot_scale_smoke:
                    # Only the fresh probe's temporary DB is seeded. This tests
                    # the exact installed executable, never a user's live data.
                    helper_path = Path(__file__).with_name('snapshot_scale_fixture.py')
                    spec = importlib.util.spec_from_file_location('installed_snapshot_scale_fixture', helper_path)
                    helper = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(helper)
                    expected = helper.seed_snapshot_scale(database, registered['user']['id'],
                        result_count=snapshot_result_count, identity_count=snapshot_identity_count)
                    expected_ig_identities = snapshot_identity_count
                    timings = []
                    health_timings = []
                    last_snapshot = {}
                    def measured_snapshot(platform=None, *, compact=False):
                        nonlocal last_snapshot
                        started = time.monotonic()
                        status, snapshot = request('/api/workbench/snapshot?limit=2000&history_limit=2000'
                            + ('&platform=' + platform if platform else '') + ('&compact=1' if compact else ''),
                            session=session, budget=25)
                        elapsed = time.monotonic() - started
                        if status != 200 or elapsed >= 25:
                            raise RuntimeError('Installed scale snapshot exceeded its 25 second release budget')
                        for key in ('total_collected', 'total_public', 'total_private', 'total_split'):
                            if snapshot.get('counts', {}).get(key) != expected[key]:
                                raise RuntimeError('Installed scale snapshot lost or inflated ' + key)
                        expected_dedupe = expected_ig_identities
                        if snapshot.get('dedupe', {}).get('total') != expected_dedupe:
                            raise RuntimeError(f"Installed scale snapshot lost or inflated identity totals: platform={platform} actual={snapshot.get('dedupe', {}).get('total')} expected={expected_dedupe}")
                        if platform and snapshot.get('platform') != platform:
                            raise RuntimeError('Installed snapshot platform scope was not applied')
                        last_snapshot = snapshot
                        return round(elapsed, 3)
                    timings.append(measured_snapshot())
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pending = pool.submit(measured_snapshot)
                        for _ in range(3):
                            started = time.monotonic()
                            status, heartbeat = request('/api/workbench/live-status', session=session, budget=5)
                            health_timings.append(round(time.monotonic() - started, 3))
                            if status != 200 or not isinstance(heartbeat.get('tasks'), list):
                                raise RuntimeError('Installed task heartbeat failed during the heavy snapshot')
                        timings.append(pending.result(timeout=30))
                    platform_timings = {platform: measured_snapshot(platform)
                        for platform in ('instagram',)}
                    # Verify the real modern desktop transport against the same
                    # installed service and unchanged large synthetic database.
                    legacy_seconds = measured_snapshot('instagram')
                    legacy_snapshot = last_snapshot
                    compact_seconds = measured_snapshot('instagram', compact=True)
                    compact_snapshot = last_snapshot
                    aliases = {'pending_public_accounts', 'pending_private_accounts',
                        'approved_public_accounts', 'approved_private_accounts',
                        'approval_history', 'manual_rejection_history', 'collection_exclusion_history'}
                    if not aliases <= set(legacy_snapshot) or aliases & set(compact_snapshot):
                        raise RuntimeError('Installed compact snapshot wire contract is invalid')
                    for field in ('pending', 'approved', 'history', 'counts', 'has_more', 'split_candidates'):
                        if legacy_snapshot[field] != compact_snapshot[field]:
                            raise RuntimeError('Installed compact snapshot changed business rows: ' + field)
                    encoded_size = lambda value: len(json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))
                    full_bytes, compact_bytes = encoded_size(legacy_snapshot), encoded_size(compact_snapshot)
                    if compact_bytes >= full_bytes or (snapshot_result_count >= 100_000 and compact_bytes >= full_bytes * .70):
                        raise RuntimeError('Installed compact snapshot did not remove repeated row payloads')
                    wire_proof = {'verified': True, 'canonical_rows_equal': True,
                        'legacy_bytes': full_bytes, 'compact_bytes': compact_bytes,
                        'legacy_seconds': legacy_seconds, 'compact_seconds': compact_seconds,
                        'ratio': round(compact_bytes / full_bytes, 4)}
                    scale_proof = {'verified': True, 'compact_wire': wire_proof, 'collected': snapshot_result_count,
                        'identities': snapshot_identity_count, 'snapshot_seconds': timings,
                        'platform_snapshot_seconds': platform_timings,
                        'heartbeat_seconds': health_timings, 'seed_seconds': expected['seed_seconds']}
                    scale_proof['work_report_summary'] = probe_work_report_summary(
                        request, session=session, result_count=snapshot_result_count)
                if pure_ig_smoke:
                    if health.get('collection_platforms') != ['instagram'] or 'facebook_worker' in health:
                        raise RuntimeError('Installed Core is not pure Instagram')
                    for payload in (
                        {'platform': 'facebook', 'targets': ['https://www.facebook.com/profile.php?id=123456789'], 'window_ids': [], 'modes': ['followers']},
                        {'platform': 'instagram', 'targets': ['https://www.facebook.com/example'], 'window_ids': [], 'modes': ['followers']},
                    ):
                        try:
                            request('/api/tasks', method='POST', session=session, budget=10, payload=payload)
                        except HTTPError as error:
                            if error.code not in {400, 409, 422}:
                                raise RuntimeError('Installed pure IG foreign-input refusal failed') from error
                        else:
                            raise RuntimeError('Installed pure IG accepted removed platform input')

                if snapshot_scale_smoke:
                    card_spec = importlib.util.spec_from_file_location('installed_completed_card_smoke_fixture',
                        Path(__file__).with_name('completed_card_smoke_fixture.py'))
                    card_helper = importlib.util.module_from_spec(card_spec)
                    card_spec.loader.exec_module(card_helper)
                    status, created_card = request('/api/tasks', method='POST', session=session, budget=10,
                        payload={'platform': 'instagram', 'targets': [card_helper.SOURCE],
                                 'window_ids': [card_helper.WINDOW], 'modes': ['followers']})
                    if status != 201 or not created_card.get('task_id'):
                        raise RuntimeError('Installed completed-card fixture task could not be created')
                    status, card_task = request('/api/tasks/' + created_card['task_id'], session=session, budget=10)
                    if status != 200 or len(card_task.get('targets', [])) != 1:
                        raise RuntimeError('Installed completed-card fixture task could not be read')
                    card_fixture = card_helper.seed_completed_card_smoke(database, registered['user']['id'],
                        created_card['task_id'], card_task['targets'][0]['id'], isolated_directory=data)
                    # HTTP task creation reserves its source identity; the helper
                    # adds one saved profile and one immutable completion record.
                    expected_ig_identities += 1 + card_fixture['identities_added']
                    expected['total_collected'] += card_fixture['results_added']
                    expected['total_public'] += card_fixture['public_added']
                    expected['total_split'] += card_fixture['split_added']

                    def check_completed_card_views(dismissed):
                        status, current = request('/api/tasks/' + card_fixture['task_id'], session=session, budget=10)
                        if status != 200:
                            raise RuntimeError('Installed completed-card task could not be read')
                        card_helper.assert_completed_card_dto(current, card_fixture, dismissed=dismissed)
                        for platform in ('instagram',):
                            status, page = request('/api/workbench/snapshot?platform=' + platform + '&limit=100&history_limit=100',
                                session=session, budget=25)
                            if status != 200 or page.get('platform') != platform:
                                raise RuntimeError('Installed completed-card snapshot platform was not applied')
                            matching = [task for task in page.get('tasks', []) if task.get('id') == card_fixture['task_id']]
                            if len(matching) != 1:
                                raise RuntimeError('Installed completed-card was removed from retained task history')
                            else:
                                card_helper.assert_completed_card_dto(matching[0], card_fixture, dismissed=dismissed)

                    check_completed_card_views(False)
                    card_before = card_helper.completed_card_state(database, card_fixture)
                    card_payload = {'task_id': card_fixture['task_id'], 'target_id': card_fixture['target_id'],
                                    'action': 'dismiss_completed'}
                    try:
                        request('/api/workbench/commands', method='POST', session=session, budget=10,
                            payload={'command': 'task_target_control', 'payload': {**card_payload, 'platform': 'facebook'}})
                    except HTTPError as error:
                        if error.code not in {400, 409, 422}:
                            raise RuntimeError('Installed completed-card wrong-platform refusal was not a conflict') from error
                    else:
                        raise RuntimeError('Installed completed-card accepted a crossed-platform dismissal')
                    if card_helper.completed_card_state(database, card_fixture) != card_before:
                        raise RuntimeError('Installed wrong-platform dismissal changed retained data')
                    card_after = None
                    for _ in range(2):
                        status, dismissed_reply = request('/api/workbench/commands', method='POST', session=session, budget=10,
                            payload={'command': 'task_target_control', 'payload': {**card_payload, 'platform': 'instagram'}})
                        expected_result = {'task_id': card_fixture['task_id'], 'target_id': card_fixture['target_id'],
                                           'collection_list_dismissed': True}
                        if status != 200 or dismissed_reply.get('result') != expected_result:
                            raise RuntimeError('Installed completed-card dismissal command returned invalid DTO flags')
                        current_state = card_helper.completed_card_state(database, card_fixture)
                        card_helper.assert_completed_card_retained(card_before, current_state, card_fixture)
                        if card_after is not None and current_state != card_after:
                            raise RuntimeError('Installed completed-card repeated dismissal was not idempotent')
                        card_after = current_state
                    check_completed_card_views(True)
                    measured_snapshot('instagram')
                    scale_proof['completed_card_dismissal'] = {
                        'verified': True, 'persistence_after_restart': False,
                        'retained_data': True,
                        'retained_data_details': {'exact_fixture_rows': True, 'ledger_counts': card_after['counts']},
                        'platform_isolation': True, 'idempotent': True,
                        'completed_recheck_positive_gap': True, 'isolated_temporary_database': True,
                        'user_data_touched': False}

                status, reply = request('/api/internal/shutdown', method='POST')
                if status != 200 or reply.get('accepted') is not True:
                    raise RuntimeError('Core did not acknowledge orderly shutdown')
                try:
                    code = process.wait(timeout=shutdown_timeout)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError('Core did not exit after acknowledging shutdown') from error
                if code != 0:
                    raise RuntimeError(f'Core shutdown failed (exit {code})')
                if snapshot_scale_smoke:
                    upgrade_spec = importlib.util.spec_from_file_location('installed_pure_ig_upgrade_fixture',
                        Path(__file__).with_name('pure_ig_upgrade_fixture.py'))
                    upgrade_helper = importlib.util.module_from_spec(upgrade_spec)
                    upgrade_spec.loader.exec_module(upgrade_helper)
                    upgrade_fixture = upgrade_helper.seed(database, registered['user']['id'], isolated_directory=data)
                    expected_ig_identities += upgrade_fixture['ig_added']
                    # Deliberately added IG sentinel rows become part of the
                    # expected retained ledger; every preexisting card row stays exact.
                    for table, delta in upgrade_fixture['ig_count_deltas'].items():
                        card_after['counts'][table] += delta
                    # Reopen the same isolated large DB in the installed EXE after
                    # removing only the additive platform counters. This models
                    # upgrading an earlier installation without touching records.
                    with closing(sqlite3.connect(database)) as connection:
                        for suffix in ('insert', 'delete', 'rename', 'account_delete'):
                            connection.execute('DROP TRIGGER IF EXISTS trg_global_seen_platform_' + suffix)
                        connection.execute('DROP TABLE global_seen_platform_stats')
                        # Model an older installation that predates both review
                        # columns, not just a missing materialized stats table.
                        for index in ('idx_candidates_platform_review_stage', 'idx_workbench_review_stage'):
                            connection.execute('DROP INDEX IF EXISTS ' + index)
                        connection.execute('ALTER TABLE workbench_candidates DROP COLUMN review_stage')
                        connection.execute('ALTER TABLE workbench_candidates DROP COLUMN review_transferred_at')
                        connection.commit()
                    migration_started = time.monotonic()
                    process = subprocess.Popen([*child_command, '--host', '127.0.0.1', '--port', str(port)],
                        cwd=data, env=environment, stdout=log, stderr=subprocess.STDOUT)
                    migration_deadline = migration_started + timeout
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError('Installed Core exited during legacy counter upgrade')
                        if time.monotonic() >= migration_deadline:
                            raise RuntimeError('Installed Core legacy counter upgrade timed out')
                        try:
                            status, migrated_health = request('/api/health')
                        except (URLError, TimeoutError, ConnectionError):
                            time.sleep(.1)
                            continue
                        if status != 200 or migrated_health.get('database') != 'ok':
                            raise RuntimeError('Installed Core legacy counter upgrade health failed')
                        break
                    migration_seconds = round(time.monotonic() - migration_started, 3)
                    scale_proof['pure_ig_upgrade'] = upgrade_helper.verify(database, upgrade_fixture)
                    upgraded = {platform: measured_snapshot(platform) for platform in ('instagram',)}
                    measured_snapshot()
                    check_completed_card_views(True)
                    if card_helper.completed_card_state(database, card_fixture) != card_after:
                        raise RuntimeError('Installed completed-card marker/evidence changed after restart')
                    scale_proof['completed_card_dismissal']['persistence_after_restart'] = True
                    scale_proof['legacy_platform_counter_upgrade'] = {
                        'verified': True, 'restart_seconds': migration_seconds,
                        'platform_snapshot_seconds': upgraded, 'records_preserved': True,
                        'legacy_review_columns_restored': True,
                        'expected_ig_identities': expected_ig_identities}
                    status, reply = request('/api/internal/shutdown', method='POST')
                    if status != 200 or reply.get('accepted') is not True:
                        raise RuntimeError('Migrated installed Core did not acknowledge shutdown')
                    if process.wait(timeout=shutdown_timeout) != 0:
                        raise RuntimeError('Migrated installed Core did not shut down cleanly')
                    # A normal current-schema restart is separate from first
                    # upgrade/migration; measure the installed executable without
                    # dropping its indexes or any retained business records.
                    restart_started = time.monotonic()
                    process = subprocess.Popen([*child_command, '--host', '127.0.0.1', '--port', str(port)],
                        cwd=data, env=environment, stdout=log, stderr=subprocess.STDOUT)
                    restart_deadline = restart_started + timeout
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError('Installed Core exited during normal restart')
                        if time.monotonic() >= restart_deadline:
                            raise RuntimeError('Installed Core normal restart timed out')
                        try:
                            status, restarted_health = request('/api/health')
                        except (URLError, TimeoutError, ConnectionError):
                            time.sleep(.1)
                            continue
                        if status != 200 or restarted_health.get('database') != 'ok':
                            raise RuntimeError('Installed Core normal restart health failed')
                        break
                    normal_restart_seconds = round(time.monotonic() - restart_started, 3)
                    upgrade_helper.verify(database, upgrade_fixture)
                    restarted_timings = {platform: measured_snapshot(platform, compact=True)
                        for platform in ('instagram',)}
                    if card_helper.completed_card_state(database, card_fixture) != card_after:
                        raise RuntimeError('Installed normal restart changed retained card data')
                    scale_proof['normal_restart'] = {'verified': True,
                        'startup_seconds': normal_restart_seconds,
                        'compact_platform_snapshot_seconds': restarted_timings,
                        'retained_data': True, 'schema_unchanged': True}
                    status, index_user = request('/api/session/register', method='POST', budget=10,
                        payload={'username': 'report-index-upgrade-smoke', 'password': secrets.token_hex(24)})
                    index_session = index_user.get('session_token')
                    if status != 201 or not index_session:
                        raise RuntimeError('Installed report-index fixture owner could not be created')
                    status, reply = request('/api/internal/shutdown', method='POST')
                    if status != 200 or reply.get('accepted') is not True:
                        raise RuntimeError('Restarted installed Core did not acknowledge shutdown')
                    if process.wait(timeout=shutdown_timeout) != 0:
                        raise RuntimeError('Restarted installed Core did not shut down cleanly')
                    # Model the actual historical collision while the installed
                    # Core is stopped. The fixture changes schema indexes only;
                    # every existing inventory/dedup payload is hashed intact.
                    index_spec = importlib.util.spec_from_file_location('installed_report_index_fixture',
                        Path(__file__).with_name('report_index_upgrade_fixture.py'))
                    index_helper = importlib.util.module_from_spec(index_spec)
                    index_spec.loader.exec_module(index_helper)
                    index_expected = index_helper.seed(database, index_user['user']['id'], isolated_directory=data)
                    index_before = index_helper.state(database)
                    legacy_index = index_helper.install_old_index(database, isolated_directory=data)
                    report_timings = []
                    for restart in range(2):
                        process = subprocess.Popen([*child_command, '--host', '127.0.0.1', '--port', str(port)],
                            cwd=data, env=environment, stdout=log, stderr=subprocess.STDOUT)
                        index_deadline = time.monotonic() + timeout
                        while True:
                            if process.poll() is not None:
                                raise RuntimeError('Installed Core exited during report-index upgrade')
                            if time.monotonic() >= index_deadline:
                                raise RuntimeError('Installed Core report-index upgrade timed out')
                            try:
                                status, index_health = request('/api/health')
                            except (URLError, TimeoutError, ConnectionError):
                                time.sleep(.1)
                                continue
                            if (status != 200 or index_health.get('database') != 'ok'
                                    or index_health.get('source_revision') != expected_revision):
                                raise RuntimeError('Installed Core report-index upgrade health failed')
                            break
                        report_timings.append(probe_report_index_totals(request,
                            session=index_session, expected=index_expected))
                        index_proof = index_helper.verify(database, index_before, legacy_index)
                        status, reply = request('/api/internal/shutdown', method='POST')
                        if status != 200 or reply.get('accepted') is not True:
                            raise RuntimeError('Report-index Core did not acknowledge shutdown')
                        if process.wait(timeout=shutdown_timeout) != 0:
                            raise RuntimeError('Report-index Core did not shut down cleanly')
                        index_helper.verify(database, index_before, legacy_index)
                    scale_proof['report_index_upgrade'] = {**index_proof,
                        'restart_count': 2, 'repeated_startup_idempotent': True,
                        'four_card_totals': index_expected['totals'], 'report_timings': report_timings}
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        if not database.is_file():
            raise RuntimeError('Core did not create its isolated database')
        # sqlite3.Connection.__exit__ only commits/rolls back; it does NOT close
        # the file. Windows refuses to delete this directory while it is open.
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
            if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise RuntimeError('Core database integrity check failed')
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'app_users', 'tasks', 'task_targets', 'schema_migrations'} <= tables:
                raise RuntimeError('Core did not initialize the current application schema')
            if snapshot_scale_smoke:
                required_indexes = {'idx_result_duplicate_archive_account',
                    'idx_workbench_cache_rebuild', 'idx_exclusions_legacy_hover'}
                actual_indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
                if not required_indexes <= actual_indexes:
                    raise RuntimeError('Installed performance indexes are missing')
                scale_proof['performance_indexes_verified'] = sorted(required_indexes)
                columns = {row[1] for row in connection.execute('PRAGMA table_info(workbench_candidates)')}
                index_columns = [row[2] for row in connection.execute('PRAGMA index_info(idx_candidates_platform_review_stage)')]
                if (not {'review_stage', 'review_transferred_at'} <= columns
                        or index_columns != ['owner_user_id', 'status', 'visibility', 'review_stage', 'created_at', 'id', 'account_id']):
                    raise RuntimeError('Installed old-schema upgrade did not restore review fields and covering index')

        return {'verified': True, 'source_revision': expected_revision, 'posting_removed': removed_proof,
                'authentication': True, 'database': 'ok', 'orderly_shutdown': True,
                **({'pure_instagram': True, 'removed_platform_inputs_rejected': True} if pure_ig_smoke else {}),
                **({'snapshot_scale': scale_proof, 'work_report_summary': scale_proof['work_report_summary'],
                    'report_index_upgrade': scale_proof['report_index_upgrade']}
                   if snapshot_scale_smoke else {})}


def probe_collection_completion(command: list[str], log_path: Path, *, timeout=120.0) -> dict:
    """Run the installed executable's offline production-runtime fixture.

    The executable creates its own disposable DB; no user DB or browser settings
    are inherited. Timeout/nonzero exit/malformed proof are all release failures.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Collection completion probe deadline must be finite and positive')
    environment = {key: value for key, value in os.environ.items()
        if not key.upper().startswith(('IGAC_', 'COLLECTOR_CORE_', 'PLAYWRIGHT_', 'PYTHON'))
        and key.upper() not in {'OPENVINO_LIB_PATHS', '__PYVENV_LAUNCHER__'}}
    environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    if os.name == 'nt':
        windows = Path(os.environ['SystemRoot'])
        environment['PATH'] = os.pathsep.join(map(str, (windows / 'System32', windows, windows / 'System32/Wbem')))
    log_path = Path(log_path).absolute()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with probe_directory(prefix='Juxin-CollectionCompletionCli-') as temporary:
        with log_path.open('wb') as log:
            process = subprocess.Popen([*direct_child_command(command, environment), '--verify-collection-completion'],
                cwd=temporary, env=environment, stdout=log, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait(timeout=10)
                raise RuntimeError(f'Installed collection completion self-test timed out; see {log_path}') from error
    if code != 0:
        raise RuntimeError(f'Installed collection completion self-test failed ({code}); see {log_path}')
    prefix = 'COLLECTION_COMPLETION_SELFTEST=PASS '
    proofs = [line[len(prefix):] for line in log_path.read_text(encoding='utf-8').splitlines() if line.startswith(prefix)]
    if len(proofs) != 1:
        raise RuntimeError('Installed collection completion self-test did not emit exactly one proof')
    proof = json.loads(proofs[0])
    return validate_collection_completion_proof(proof)


def validate_collection_completion_proof(proof: dict) -> dict:
    """Reject missing, stale or internally inconsistent R6/R6.2 evidence."""
    if (not isinstance(proof, dict) or proof.get('verified') is not True or proof.get('synthetic') is not True
            or proof.get('live_accounts_tested') is not False or proof.get('user_data_touched') is not False
            or proof.get('network_disabled') is not True or proof.get('production_manager_and_service') is not True):
        raise RuntimeError('Installed collection completion proof has invalid isolation flags')
    cases = proof.get('cases', {})
    if not isinstance(cases, dict):
        raise RuntimeError('Installed collection completion cases are missing')
    for name, calls in (('normal_with_truthful_gap', 2), ('normal_without_gap', 1),
                        ('abnormal_source_retained', 1), ('extra_pass_failure_retained', 2)):
        case = cases.get(name, {})
        if (not isinstance(case, dict) or case.get('verified') is not True
                or type(case.get('source_calls')) is not int or case['source_calls'] != calls):
            raise RuntimeError('Installed collection completion case missing or invalid: ' + name)
        failed = name in {'abnormal_source_retained', 'extra_pass_failure_retained'}
        flags = ('abnormal_card_retained', 'failure_remains_incomplete') if failed else (
            'bounded_whole_list_passes', 'same_lease', 'duplicates_never_requeued',
            'cleanup_before_dismissal', 'retained_data', 'database_reopen_persistence',
            'single_target_no_duplicate_queue', 'source_seed_dedupe',
            'cross_task_recognition_dedupe', 'completion_history_retained')
        if any(case.get(flag) is not True for flag in flags):
            raise RuntimeError('Installed collection completion evidence incomplete: ' + name)
    gap = cases['normal_with_truthful_gap']
    if (gap.get('remaining_gap') != 2 or gap.get('budget_persisted_before_extra') is not True
            or gap.get('extra_pass_restarts_at_top') is not True
            or cases['normal_without_gap'].get('remaining_gap') != 0):
        raise RuntimeError('Installed one-extra-pass counts, budget or truthful coverage invalid')
    extra_cases = {
        'pause_restart_extra_pass': {
            'numbers': {'source_invocations': 3, 'from_top_passes': 2, 'completed_passes': 2, 'remaining_gap': 2},
            'flags': ('pause_resume_same_pass', 'database_reopen_persistence', 'extra_pass_resumed_from_saved_tail',
                      'no_third_pass', 'same_lease_before_shutdown', 'completed_and_hidden', 'cleanup_after_restart')},
        'parent_reels_after_final_pass': {
            'numbers': {'source_calls': 2, 'duplicate_detail_reads': 0, 'remaining_gap': 2},
            'flags': ('same_lease', 'one_child_pool', 'sibling_lease_untouched', 'parent_reels_after_final_extraction',
                      'children_finish_and_cleanup', 'reels_joined_before_cleanup')},
        'historical_completed_gap': {
            'numbers': {'source_calls': 0},
            'flags': ('no_historical_rescheduling', 'completed_rows_unchanged', 'no_browser_or_lease')},
    }
    for name, contract in extra_cases.items():
        case = cases.get(name, {})
        if (not isinstance(case, dict) or case.get('verified') is not True
                or any(case.get(flag) is not True for flag in contract['flags'])
                or any(type(case.get(key)) is not int or case[key] != value
                       for key, value in contract['numbers'].items())):
            raise RuntimeError('Installed single-gap-recheck case missing or invalid: ' + name)
    recheck = proof.get('single_gap_recheck', {})
    required = ('verified', 'restart_from_top', 'durable_per_target_mode_budget',
                'pause_resume_and_restart', 'same_collection_lease', 'sibling_lease_untouched', 'persistent_gap_completes_truthfully',
                'failures_remain_incomplete', 'no_historical_rescheduling',
                'parent_reels_after_final_extraction', 'children_finish_and_cleanup', 'synthetic')
    if (not isinstance(recheck, dict) or any(recheck.get(flag) is not True for flag in required)
            or type(recheck.get('no_gap_passes')) is not int or recheck['no_gap_passes'] != 1
            or type(recheck.get('gap_passes')) is not int or recheck['gap_passes'] != 2
            or recheck.get('live_accounts_tested') is not False):
        raise RuntimeError('Installed single_gap_recheck proof missing or invalid')
    pending_flags = ('pending_request_durable', 'duplicate_clicks_coalesced',
                     'cursor_unchanged_until_safe_point', 'children_continue_while_pending',
                     'exact_lease_fenced', 'post_pass_reels_resumed', 'no_automatic_extra_manual_loop',
                     'duplicates_never_requeued', 'cleanup_joined', 'completed_task_not_resurrected')
    pending_cases = {
        'manual_pending_before_producer_return': (1, ('producer_not_returned_at_admission',)),
        'manual_pending_parent_join': (2, ('parent_cancel_join_before_navigation', 'same_child_pool_and_lease')),
        'manual_pending_stop_reopen_retry': (1, ('producer_not_returned_at_admission',
            'stop_preserves_prepared_request', 'database_reopen_persistence', 'explicit_retry_same_generation',
            'new_lease_after_previous_released', 'no_automatic_restart')),
    }
    for name, (reels_runs, flags) in pending_cases.items():
        case = cases.get(name, {})
        numbers = {'source_calls': 3, 'manual_passes': 1, 'child_commits_while_pending': 1,
                   'saved_results': 3, 'remaining_gap': 2, 'reels_runs': reels_runs}
        if (not isinstance(case, dict) or case.get('verified') is not True
                or any(case.get(flag) is not True for flag in (*pending_flags, *flags))
                or any(type(case.get(key)) is not int or case[key] != value for key, value in numbers.items())):
            raise RuntimeError('Installed manual-parent-recheck case missing or invalid: ' + name)
    manual = proof.get('manual_parent_recheck', {})
    manual_flags = ('verified', 'durable_pending_admission', 'producer_return_and_parent_join_fenced',
                    'children_continue_while_pending', 'single_manual_pass', 'post_pass_reels_resumed',
                    'stop_reopen_explicit_retry_same_generation', 'new_lease_only_after_old_released',
                    'duplicates_never_requeued', 'completed_task_not_resurrected', 'synthetic')
    if (not isinstance(manual, dict) or any(manual.get(flag) is not True for flag in manual_flags)
            or manual.get('live_accounts_tested') is not False):
        raise RuntimeError('Installed manual_parent_recheck proof missing or invalid')
    final_flags = ('production_playwright_pool', 'late_metadata_requires_owned_close',
                   'selected_pool_bound', 'all_sources_complete', 'durable_candidates_drained',
                   'duplicate_details_prevented', 'children_joined_before_provider_close',
                   'cards_hidden_after_close_ack', 'results_dedupe_history_preserved',
                   'database_reopen_persistence', 'no_remaining_seed_or_lease')
    for name, count, children in (('final_seed_single_source', 1, 1), ('final_seed_two_sources', 2, 3),
                                   ('final_seed_multiple_sources', 3, 3)):
        case = cases.get(name, {})
        numbers = {'source_count': count, 'source_calls': count, 'saved_results': count,
                   'selected_children': children, 'provider_closes': 1, 'lease_generations': 1}
        if (not isinstance(case, dict) or case.get('verified') is not True
                or any(case.get(flag) is not True for flag in final_flags)
                or any(type(case.get(key)) is not int or case[key] != value for key, value in numbers.items())):
            raise RuntimeError('Installed final-seed case missing or invalid: ' + name)
    factory_flags = ('failure_after_first_source_completed', 'waiting_queue_empty_at_failure',
                     'parent_health_probe_true', 'authoritative_cause_preserved',
                     'final_source_read_after_reconnect', 'both_sources_completed',
                     'children_joined_before_close', 'cards_hidden_after_close_ack',
                     'no_remaining_seed_lease_or_waiter', 'results_dedupe_history_preserved',
                     'database_reopen_persistence')
    factory_numbers = {'source_count': 2, 'source_calls': 2, 'saved_results': 2,
                       'factory_failures': 1, 'connections': 2, 'reconnects': 1,
                       'provider_closes': 1, 'lease_generations': 1}
    for reason in ('worker_not_connected', 'browser_context_missing'):
        name = 'final_seed_factory_' + reason
        case = cases.get(name, {})
        if (not isinstance(case, dict) or case.get('verified') is not True
                or case.get('injected_reason') != reason
                or any(case.get(flag) is not True for flag in factory_flags)
                or any(type(case.get(key)) is not int or case[key] != value
                       for key, value in factory_numbers.items())):
            raise RuntimeError('Installed final-seed factory-reconnect case missing or invalid: ' + name)
    final = proof.get('final_seed_completion', {})
    final_summary_flags = ('verified', 'production_playwright_pool', 'late_idle_children_retired',
                          'empty_queue_after_last_source', 'authoritative_factory_reconnect', 'exact_lease_until_confirmed_close',
                          'results_dedupe_history_preserved', 'synthetic')
    if (not isinstance(final, dict) or any(final.get(flag) is not True for flag in final_summary_flags)
            or final.get('live_accounts_tested') is not False):
        raise RuntimeError('Installed final_seed_completion proof missing or invalid')
    return proof



def probe_standalone_nurture(command: list[str], log_path: Path, *, timeout=120.0) -> dict:
    """Run the exact installed executable's separate, isolated nurture fixture."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Standalone nurture probe deadline must be finite and positive')
    environment = {key: value for key, value in os.environ.items()
        if not key.upper().startswith(('IGAC_', 'COLLECTOR_CORE_', 'PLAYWRIGHT_', 'PYTHON'))
        and key.upper() not in {'OPENVINO_LIB_PATHS', '__PYVENV_LAUNCHER__'}}
    environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    if os.name == 'nt':
        windows = Path(os.environ['SystemRoot'])
        environment['PATH'] = os.pathsep.join(map(str, (windows / 'System32', windows, windows / 'System32/Wbem')))
    log_path = Path(log_path).absolute()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with probe_directory(prefix='Juxin-StandaloneNurtureCli-') as temporary:
        with log_path.open('wb') as log:
            process = subprocess.Popen([*direct_child_command(command, environment), '--verify-standalone-nurture'],
                cwd=temporary, env=environment, stdout=log, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait(timeout=10)
                raise RuntimeError(f'Installed standalone nurture self-test timed out; see {log_path}') from error
    if code != 0:
        raise RuntimeError(f'Installed standalone nurture self-test failed ({code}); see {log_path}')
    prefix = 'STANDALONE_NURTURE_SELFTEST=PASS '
    proofs = [line[len(prefix):] for line in log_path.read_text(encoding='utf-8').splitlines() if line.startswith(prefix)]
    if len(proofs) != 1:
        raise RuntimeError('Installed standalone nurture self-test did not emit exactly one proof')
    return validate_standalone_nurture_proof(json.loads(proofs[0]))


def validate_standalone_nurture_proof(proof: dict) -> dict:
    """Fail closed on stale, partial, forged or summary-only nurture receipts."""
    flags = ('verified', 'synthetic', 'network_disabled', 'production_manager_and_service',
             'production_nurture_engine', 'synthetic_dom_not_live_site_verification')
    if (not isinstance(proof, dict) or any(proof.get(flag) is not True for flag in flags)
            or proof.get('policy') != 'standalone-reels-8-20-70-v1'
            or proof.get('user_data_touched') is not False or proof.get('live_accounts_tested') is not False):
        raise RuntimeError('Installed standalone nurture proof has invalid policy or isolation flags')
    cases = proof.get('cases', {})
    if not isinstance(cases, dict):
        raise RuntimeError('Installed standalone nurture cases are missing')
    contracts = {
        'selection_and_fixed_policy': {
            'numbers': {'default_minutes': 5, 'default_concurrency': 0, 'selected_windows': 2, 'concurrent_windows': 2},
            'flags': ('blocked_batch_atomic', 'foreign_lease_untouched', 'fixed_policy_normalized',
                      'editable_minutes_and_concurrency', 'request_idempotent', 'only_selected_windows_executed')},
        'completed_history': {
            'numbers': {'planned_seconds': 60, 'followers': 1234, 'following': 27, 'posts': 0,
                        'probability_draws': 2, 'synthetic_like_clicks': 1, 'per_window_runs': 2, 'resume_observations': 2},
            'flags': ('actual_duration_recorded', 'active_budget_enforced', 'first_verified_snapshot_before_reels',
                      'first_verified_snapshot_immutable', 'unknown_counts_remain_unknown',
                      'one_decision_per_stable_video', 'exact_70_percent_boundary', 'unknown_and_liked_never_clicked',
                      'confirmed_history_atomic', 'database_reopen_persistence', 'history_owner_isolation',
                      'close_after_disconnect', 'same_lease_through_cleanup', 'completed_lease_released',
                      'safe_resume_preserves_first_verified_snapshot', 'resume_no_confirmed_effect_replay',
                      'resume_accumulates_active_duration', 'resume_counts_as_one_run',
                      'canonical_singular_and_plural_routes')},
        'unknown_own_profile': {
            'numbers': {}, 'flags': ('unknown_owner', 'identity_changed', 'no_reels_or_effect',
                'failed_window_retained', 'failed_lease_released', 'failed_actual_duration_and_outcome')},
        'interrupted_pending_effect': {
            'numbers': {'synthetic_click_attempts': 1},
            'flags': ('pending_committed_before_click', 'needs_review_after_interruption',
                      'resume_without_review_rejected', 'restart_does_not_open_browser_or_replay',
                      'decisions_and_snapshot_persist', 'pending_window_and_lease_retained',
                      'actual_duration_and_outcome_retained')},
        'legacy_plan_fence': {
            'numbers': {}, 'flags': ('unmarked_legacy_jobs_fail_closed', 'resume_and_retry_rejected',
                                     'legacy_history_preserved', 'no_browser_or_lease','legacy_wall_time_not_reinterpreted')},
        'verified_playback': {
            'numbers': {'effective_seconds':60},
            'flags': ('frozen_media_zero_seconds','stalls_excluded_from_duration',
                      'startup_navigation_confirmation_excluded','stable_advance_required',
                      'one_action_without_blind_retry','icon_only_key_advance')},
        'completed_cleanup_fence': {
            'numbers': {},
            'flags': ('close_failure_retains_exact_lease','restart_preserves_cleanup_token',
                      'new_task_blocked_until_confirmed_close','cleanup_retry_without_activity_replay',
                      'confirmed_close_releases_once','completed_history_unchanged',
                      'lost_generation_never_closes_or_releases_replacement')},
    }
    for name, contract in contracts.items():
        case = cases.get(name, {})
        if (not isinstance(case, dict) or case.get('verified') is not True
                or any(case.get(flag) is not True for flag in contract['flags'])
                or any(type(case.get(key)) is not int or case[key] != value for key, value in contract['numbers'].items())):
            raise RuntimeError('Installed standalone nurture case missing or invalid: ' + name)
    actual = cases['completed_history'].get('actual_seconds')
    if type(actual) not in (float, int) or not math.isfinite(actual) or not 52 <= actual <= 60.001:
        raise RuntimeError('Installed standalone nurture actual duration is missing or invalid')
    return proof



def _nurture_upgrade_fixture():
    spec = importlib.util.spec_from_file_location('nurture_cleanup_upgrade_fixture',
        Path(__file__).with_name('nurture_cleanup_upgrade_fixture.py'))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return fixture


def validate_nurture_cleanup_upgrade_proof(proof, *, manifest, manifest_sha256,
        expected_executable, process_pid, launch_ns, launch_perf_ns, require_installed=True):
    """Bind every case to the pre-start input and exact launched executable.

    Summary flags alone are insufficient. The caller must also run the independent
    post-exit SQLite/file oracle before this receipt can enter a release report.
    Source-development tests explicitly disable only the Windows/frozen check.
    """
    fixture = _nurture_upgrade_fixture()
    true_flags = ('verified', 'synthetic', 'network_disabled', 'production_manager_and_service',
        'production_closed_profile_guard', 'synthetic_desktop_rpc', 'persisted_before_process',
        'owner_isolation', 'archived_history_visible', 'second_startup_idempotent',
        'no_historical_activity_replay', 'normal_startup_and_shutdown',
        'archived_hold_visible_before_startup', 'fresh_account_successor_after_startup')
    false_flags = ('live_accounts_tested', 'user_data_touched')
    expected_keys = set(true_flags + false_flags) | {'contract', 'baseline_commit', 'network_attempts',
        'activity_attempts', 'synthetic_open_admissions', 'manifest_sha256', 'input_database_sha256',
        'nonce', 'seed_pid', 'seed_completed_ns', 'opened_ns', 'runtime', 'cases', 'admission',
        'startup_sequence', 'startup_action_transition', 'queued_blockers_before_startup',
        'legacy_schema_sha256', 'baseline_database_code_sha256', 'baseline_posting_code_sha256',
        'chronology_clock', 'seed_completed_perf_ns', 'opened_perf_ns'}
    if (not isinstance(proof, dict) or set(proof) != expected_keys
            or any(proof.get(key) is not True for key in true_flags)
            or any(proof.get(key) is not False for key in false_flags)):
        raise RuntimeError('Installed nurture upgrade proof has incomplete isolation or execution evidence')
    expected_values = {'contract': fixture.CONTRACT, 'baseline_commit': fixture.BASELINE_COMMIT,
        'legacy_schema_sha256': fixture.SCHEMA_SHA256,
        'baseline_database_code_sha256': fixture.BASELINE_DATABASE_SHA256,
        'baseline_posting_code_sha256': fixture.BASELINE_POSTING_SCHEMA_SHA256,
        'manifest_sha256': manifest_sha256, 'input_database_sha256': manifest['database_sha256'],
        'nonce': manifest['nonce'], 'seed_pid': manifest['seed_pid'],
        'seed_completed_ns': manifest['seed_completed_ns'],
        'chronology_clock': fixture.CHRONOLOGY_CLOCK, 'seed_completed_perf_ns': manifest['seed_completed_perf_ns']}
    if any(type(proof.get(key)) is not type(value) or proof[key] != value
           for key, value in expected_values.items()):
        raise RuntimeError('Installed nurture upgrade proof is stale or not bound to the persisted input')
    # These processes share one system-wide high-resolution counter. Strict
    # chronology must not depend on a coarse or adjustable UTC wall clock.
    if (type(proof['opened_ns']) is not int or type(launch_ns) is not int
            or type(proof['opened_perf_ns']) is not int or type(launch_perf_ns) is not int
            or type(manifest['seed_completed_perf_ns']) is not int
            or not 0 < manifest['seed_completed_perf_ns'] < launch_perf_ns < proof['opened_perf_ns']
            or proof['seed_pid'] == process_pid):
        raise RuntimeError('Installed nurture upgrade did not open a database persisted before process launch: '
            + json.dumps({'chronology_clock': proof['chronology_clock'],
                'seed_completed_perf_ns': manifest['seed_completed_perf_ns'], 'launch_perf_ns': launch_perf_ns,
                'opened_perf_ns': proof['opened_perf_ns'], 'seed_completed_ns': manifest['seed_completed_ns'],
                'launch_ns': launch_ns, 'opened_ns': proof['opened_ns'], 'seed_pid': proof['seed_pid'],
                'process_pid': process_pid, 'runtime': proof.get('runtime'),
                'performance_clock': vars(time.get_clock_info('perf_counter'))}, sort_keys=True))
    for key, value in (('network_attempts', 0), ('activity_attempts', 0), ('synthetic_open_admissions', len(fixture.RECOVERED))):
        if type(proof.get(key)) is not int or proof[key] != value:
            raise RuntimeError('Installed nurture upgrade performed unexpected effects or admissions')
    runtime = proof.get('runtime')
    runtime_keys = {'pid', 'frozen', 'windows', 'executable', 'executable_sha256', 'module_file', 'bundle_root'}
    same_path = lambda a, b: os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    if (not isinstance(runtime, dict) or set(runtime) != runtime_keys
            or type(runtime.get('pid')) is not int or runtime['pid'] != process_pid
            or type(runtime.get('frozen')) is not bool or type(runtime.get('windows')) is not bool
            or not isinstance(runtime.get('executable'), str)
            or not same_path(runtime['executable'], str(expected_executable))
            or runtime.get('executable_sha256') != fixture.file_sha256(expected_executable)
            or not isinstance(runtime.get('module_file'), str) or not isinstance(runtime.get('bundle_root'), str)):
        raise RuntimeError('Installed nurture upgrade was not executed by the exact selected Core process')
    if require_installed:
        module, bundle = Path(runtime['module_file']), Path(runtime['bundle_root'])
        if (runtime['frozen'] is not True or runtime['windows'] is not True
                or not module.is_absolute() or not bundle.is_absolute()
                or not module.is_relative_to(bundle)
                or module.name not in {'nurture_cleanup_upgrade_selftest.py', 'nurture_cleanup_upgrade_selftest.pyc'}
                or module.parent.name != 'app'):
            raise RuntimeError('Installed nurture upgrade requires bundled code in the frozen Windows Core')
    if proof.get('startup_sequence') != ['database.initialize', 'posting.retire_legacy', 'service.recover_interrupted_operations',
            'monitor.recover_interrupted', 'studio.recover', 'accounts.recover',
            'studio.start_scheduler']:
        raise RuntimeError('Installed nurture upgrade bypassed the normal production startup sequence')
    if proof.get('queued_blockers_before_startup') != {'queued_collection': 'collection',
            'queued_studio': 'studio', 'queued_monitor': 'monitor', 'queued_action': 'action'}:
        raise RuntimeError('Installed nurture upgrade did not verify queued/prepared blockers before startup')
    transition = proof.get('startup_action_transition')
    if (not isinstance(transition, dict) or set(transition) != {'before', 'after', 'version_before', 'version_after', 'after_sha256'}
            or transition.get('before') != 'queued' or transition.get('after') != 'paused'
            or type(transition.get('version_before')) is not int or transition['version_before'] != 1
            or type(transition.get('version_after')) is not int or transition['version_after'] != 2
            or not isinstance(transition.get('after_sha256'), str) or len(transition['after_sha256']) != 64
            or any(char not in '0123456789abcdef' for char in transition['after_sha256'])):
        raise RuntimeError('Installed nurture upgrade did not disclose the normal startup action pause')
    cases = proof.get('cases')
    if not isinstance(cases, dict) or set(cases) != set(fixture.CASES):
        raise RuntimeError('Installed nurture upgrade cases are missing or unexpected')
    case_keys = {'verified', 'job_id', 'profile_id', 'hold_before', 'hold_after', 'startup_guard_calls',
                 'recovery_state', 'history_sha256', 'explicit_retry_error', 'historical_lease_token'}
    for name in fixture.CASES:
        case = cases.get(name)
        ident = manifest['cases'][name]['job_id']
        recovered = name in fixture.RECOVERED
        calls = 1 if name in fixture.RECOVERED + ('open', 'unknown_state') else 0
        error = '' if recovered else ('not_found' if name in {'unknown_profile', 'foreign_profile_owner'}
                  else 'upstream_unavailable' if name == 'unknown_state' else 'conflict')
        before = manifest['before_jobs'][ident]
        token = json.loads(before['result_json']).get('window_cleanup', {}).get('lease_token', '')
        if (not isinstance(case, dict) or set(case) != case_keys or case.get('verified') is not True
                or case.get('job_id') != ident or case.get('profile_id') != manifest['cases'][name]['profile_id']
                or case.get('hold_before') is not True or case.get('hold_after') is not (not recovered)
                or type(case.get('startup_guard_calls')) is not int or case['startup_guard_calls'] != calls
                or case.get('history_sha256') != fixture.digest(fixture.history(before))
                or case.get('historical_lease_token') != token or case.get('explicit_retry_error') != error
                or case.get('recovery_state') != ('reconciled_closed' if recovered else 'lease_lost')):
            raise RuntimeError('Installed nurture upgrade case missing or inconsistent: ' + name)
    admission = proof.get('admission')
    if not isinstance(admission, dict) or set(admission) != set(fixture.RECOVERED):
        raise RuntimeError('Installed nurture upgrade new-task/open admission evidence is missing')
    flags = ('blocked_task_before', 'blocked_open_before', 'open_after', 'new_task_after', 'account_lease_released')
    job_ids = []
    for name in fixture.RECOVERED:
        case = admission.get(name)
        if (not isinstance(case, dict) or set(case) != set(flags) | {'new_job_id'}
                or any(case.get(flag) is not True for flag in flags)
                or not isinstance(case.get('new_job_id'), str) or not case['new_job_id']
                or case['new_job_id'] in manifest['before_jobs']):
            raise RuntimeError('Installed nurture upgrade admission case missing or invalid: ' + name)
        job_ids.append(case['new_job_id'])
    if len(set(job_ids)) != len(fixture.RECOVERED):
        raise RuntimeError('Installed nurture upgrade did not create one fresh job per recovered profile')
    return proof


def probe_nurture_cleanup_upgrade(command: list[str], log_path: Path, *, timeout=120.0,
                                  require_installed=True) -> dict:
    """Seed old state externally, launch the selected EXE, then verify actual DB bytes/state."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Nurture upgrade probe deadline must be finite and positive')
    if not command:
        raise ValueError('Nurture upgrade needs an executable command')
    environment = {key: value for key, value in os.environ.items()
        if not key.upper().startswith(('IGAC_', 'COLLECTOR_CORE_', 'PLAYWRIGHT_', 'PYTHON'))
        and key.upper() not in {'OPENVINO_LIB_PATHS', '__PYVENV_LAUNCHER__'}}
    environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    if os.name == 'nt':
        windows = Path(os.environ['SystemRoot'])
        environment['PATH'] = os.pathsep.join(map(str, (windows / 'System32', windows, windows / 'System32/Wbem')))
    log_path = Path(log_path).absolute()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fixture = _nurture_upgrade_fixture()
    with probe_directory(prefix='Juxin-NurtureUpgradeCli-') as temporary:
        data = Path(temporary)
        manifest_path, manifest = fixture.seed(data)
        manifest_sha256 = fixture.file_sha256(manifest_path)
        child_command = direct_child_command(command, environment)
        executable = Path(command[0]).resolve(strict=True)
        with log_path.open('wb') as log:
            launch_ns = time.time_ns()
            launch_perf_ns = time.perf_counter_ns()
            process = subprocess.Popen([*child_command, '--verify-nurture-cleanup-upgrade', str(manifest_path)],
                cwd=data, env=environment, stdout=log, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait(timeout=10)
                raise RuntimeError(f'Installed nurture upgrade timed out; see {log_path}') from error
        if code != 0:
            raise RuntimeError(f'Installed nurture upgrade failed ({code}); see {log_path}')
        prefix = 'NURTURE_CLEANUP_UPGRADE_SELFTEST=PASS '
        emitted = [line[len(prefix):] for line in log_path.read_text(encoding='utf-8').splitlines()
                   if line.startswith(prefix)]
        if len(emitted) != 1:
            raise RuntimeError('Installed nurture upgrade did not emit exactly one proof')
        proof = validate_nurture_cleanup_upgrade_proof(json.loads(emitted[0]), manifest=manifest,
            manifest_sha256=manifest_sha256, expected_executable=executable, process_pid=process.pid,
            launch_ns=launch_ns, launch_perf_ns=launch_perf_ns, require_installed=require_installed)
        if fixture.file_sha256(manifest_path) != manifest_sha256:
            raise RuntimeError('Installed nurture upgrade modified its externally captured input manifest')
        proof['persisted_state'] = fixture.inspect_after(data, manifest, proof)
        return proof


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--pure-ig', action='store_true', help='Require pure Instagram capability and removed-platform rejection')
    parser.add_argument('--snapshot-scale', action='store_true', help='Verify the installed Core with an isolated 441552/602831-row synthetic database')
    parser.add_argument('--collection-completion', action='store_true', help='Run offline installed R6/R6.2 single-gap-pass, durable manual parent recheck, completion and cleanup verification')
    parser.add_argument('--standalone-nurture', action='store_true', help='Run isolated installed standalone Reels, identity, history and pending-effect verification')
    parser.add_argument('--nurture-cleanup-upgrade', action='store_true', help='Require installed Windows Core recovery of externally persisted R6.2 orphan holds')
    parser.add_argument('--report', type=Path, help='Write the successful verification record')
    args = parser.parse_args()
    try:
        result = probe_core([str(args.executable.resolve(strict=True))], args.log, timeout=args.timeout,
                            pure_ig_smoke=args.pure_ig, snapshot_scale_smoke=args.snapshot_scale)
        if args.collection_completion:
            result['collection_completion'] = probe_collection_completion(
                [str(args.executable.resolve(strict=True))], args.log.with_name(args.log.stem + '-collection-completion.log'))
            result['single_gap_recheck'] = result['collection_completion']['single_gap_recheck']
            result['manual_parent_recheck'] = result['collection_completion']['manual_parent_recheck']
            result['final_seed_completion'] = result['collection_completion']['final_seed_completion']
        if args.standalone_nurture:
            result['standalone_nurture'] = probe_standalone_nurture(
                [str(args.executable.resolve(strict=True))], args.log.with_name(args.log.stem + '-standalone-nurture.log'))
        if args.nurture_cleanup_upgrade:
            result['nurture_cleanup_upgrade'] = probe_nurture_cleanup_upgrade(
                [str(args.executable.resolve(strict=True))], args.log.with_name(args.log.stem + '-nurture-cleanup-upgrade.log'))
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    except Exception as error:
        print(f'FROZEN_CORE_SERVICE_CHECK=FAIL: {error}')
        return 1
    print('FROZEN_CORE_SERVICE_CHECK=PASS ' + json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
