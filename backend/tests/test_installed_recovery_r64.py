"""Actual source Core HTTP recovery and strict installed-proof rejection gates."""
from __future__ import annotations
import copy
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('installed_recovery_r64', ROOT / 'scripts/verify_installed_recovery_r64.py')
probe = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(probe)
f = probe.fixture


class IpcFacade:
    """Mirror the actual preload allowlist and status-stripping error boundary."""
    def __init__(self, api):
        self.api = api
        source = (ROOT / 'desktop/src/core-request-policy.ts').read_text()
        import re
        self.posts = set(re.findall(r'"(/api/[^" ]+)"', source.split('const allowedPostPaths = new Set([')[1].split(']);')[0]))
    def __call__(self, path, body=None):
        allowed = path in self.posts if body is not None else path in {'/api/posting/snapshot', '/api/workbench/snapshot?limit=1&history_limit=1&platform=instagram'}
        if not allowed: return {'ok': False, 'transport': 'desktop-ipc', 'status': None, 'error': '不允许访问该本机接口'}
        value = self.api(path, body)
        if value['ok']: return {**value, 'transport': 'desktop-ipc'}
        return {'ok': False, 'transport': 'desktop-ipc', 'status': None, 'error': str(value['error'].get('detail') or value['error'].get('message') or '请求失败：' + str(value['status']))}


class InstalledRecoveryR64Tests(unittest.TestCase):
    IPC_MODE = False
    @classmethod
    def setUpClass(cls):
        # Verify the actual source representation once. Rejection cases below
        # vary synthetic artifact fields, not the source identity authority.
        cls.source_identity = probe.source_binding()
        cls.temporary = tempfile.TemporaryDirectory(prefix='r64-api-source-')
        cls.root = Path(cls.temporary.name)
        cls.manifest_path, cls.manifest = f.seed(cls.root)
        cls.artifacts = cls.root / 'synthetic-install'; cls.artifacts.mkdir()
        cls.executable = cls.artifacts / 'desktop.exe'; cls.executable.write_bytes(b'Synthetic desktop identity for validator rejection tests')
        cls.core_executable = cls.artifacts / 'resources/backend/collector_core/collector_core.exe'; cls.core_executable.parent.mkdir(parents=True); cls.core_executable.write_bytes(b'Synthetic core identity for validator rejection tests')
        (cls.artifacts / 'resources/app.asar').write_bytes(b'Synthetic packaged app identity for validator rejection tests')
        cls.manifest_sha256 = f.legacy.file_sha256(cls.manifest_path)
        cls.calls = []; cls.forbidden = []
        bridge_token = 'offline-desktop-bridge-' + cls.manifest['nonce']
        class Bridge(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.headers.get('Authorization') != 'Bearer ' + bridge_token:
                    self.send_error(403); return
                cls.calls.append(body)
                method = body['method']
                if method == 'confirm-closed':
                    if body.get('profile') not in f.PROFILES.values() or body.get('owner') != f.OWNER:
                        cls.forbidden.append(body); self.send_error(409); return
                    result = {'closed': True, 'profile_id': body['profile'], 'owner_user_id': body['owner'], 'verification': 'desktop-absence-v1'}
                elif method == 'inventory': result = {'profiles': []}
                elif method == 'hide': result = {'visible': False}
                else:
                    cls.forbidden.append(body); self.send_error(409); return
                encoded = json.dumps(result).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
            def log_message(self, *args): pass
        cls.bridge = ThreadingHTTPServer(('127.0.0.1', 0), Bridge)
        cls.thread = threading.Thread(target=cls.bridge.serve_forever, daemon=True); cls.thread.start()
        port = probe.reserve_port(); token = cls.manifest['nonce']
        env = probe.environment()
        env.update(IGAC_STARTUP_TOKEN=token, IGAC_PORT=str(port), IGAC_DATA_DIR=str(cls.root),
            IGAC_DB_PATH=str(cls.root / 'collector.sqlite3'), IGAC_BITBROWSER_URL='http://127.0.0.1:1',
            IGAC_EMBEDDED_BROWSER_URL='http://127.0.0.1:' + str(cls.bridge.server_port), IGAC_EMBEDDED_BROWSER_TOKEN=bridge_token)
        body = f'import sys;sys.path.insert(0,{str(ROOT / "backend")!r});from app.__main__ import main;main()'
        if os.environ.get('IGAC_LOCAL_TEST_ISOLATION') == '1':
            guard = ROOT.parent / 'local-test-isolation'
            if not (guard / 'guard.py').is_file(): raise RuntimeError('Local isolation guard is missing')
            body = f'import os,sys;os.environ["IGAC_LOCAL_TEST_ISOLATION"]="1";sys.path.insert(0,{str(guard)!r});import guard;guard.install();' + body
        cmd = probe.core_probe.direct_child_command([sys.executable, '-c', body], env)
        cls.log_path = cls.root / 'core.log'; cls.log = cls.log_path.open('wb')
        cls.launch_perf_ns = time.perf_counter_ns()
        cls.process = subprocess.Popen(cmd, cwd=cls.root, env=env, stdout=cls.log, stderr=subprocess.STDOUT)
        cls.http_api = probe.HttpApi('http://127.0.0.1:' + str(port), token)
        cls.api = IpcFacade(cls.http_api) if cls.IPC_MODE else cls.http_api
        try:
            deadline = time.monotonic() + 30
            while True:
                if cls.process.poll() is not None or time.monotonic() >= deadline: raise RuntimeError('Source Core did not become ready')
                try:
                    if cls.http_api('/api/health')['ok']: break
                except OSError: pass
                time.sleep(.05)
            cls.ready_perf_ns = time.perf_counter_ns()
            cls.result = probe.exercise(cls.api, cls.root, cls.manifest)
            response = cls.http_api('/api/internal/shutdown', {})
            if not response['ok']: raise RuntimeError('Normal Core shutdown rejected: ' + str(response))
            if cls.process.wait(timeout=20): raise RuntimeError('Normal Core shutdown failed')
            cls.log.close()
            cls.result['persisted_state'] = f.inspect(cls.root, cls.manifest, phase='complete')
        except BaseException:
            if cls.process.poll() is None: cls.process.kill(); cls.process.wait(timeout=10)
            cls.log.close()
            print(cls.log_path.read_text(encoding='utf-8'))
            print('Failed source fixture posts:', f.read_state(cls.root)['posting_jobs'])
            print('Failed source bridge calls:', cls.calls)
            cls.bridge.shutdown(); cls.bridge.server_close(); cls.thread.join(timeout=2)
            cls.temporary.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.bridge.shutdown(); cls.bridge.server_close(); cls.thread.join(timeout=2)
        cls.temporary.cleanup()

    def test_real_separate_normal_lifespan_core_authenticated_api_recovers_both_flows(self):
        self.assertTrue(all(self.result['checks'].values()))
        self.assertEqual([], self.forbidden)
        self.assertEqual(2, sum(call['method'] == 'confirm-closed' for call in self.calls))
        self.assertLess(self.manifest['seed_completed_perf_ns'], self.launch_perf_ns)
        self.assertLess(self.launch_perf_ns, self.ready_perf_ns)
        self.assertNotEqual(self.manifest['seed_pid'], self.process.pid)
        self.assertEqual(self.manifest_sha256, f.legacy.file_sha256(self.manifest_path))
        print('SOURCE_INSTALLED_RECOVERY_R64=PASS ' + json.dumps(self.result, sort_keys=True))

    def proof(self):
        binding = copy.deepcopy(self.source_identity)
        executable = self.executable
        result = copy.deepcopy(self.result)
        for call in result['api_calls']:
            call['transport'] = 'desktop-ipc'; call['observed_status'] = None
        return {**result, **binding, 'desktop_app_asar_sha256': f.legacy.file_sha256(self.artifacts / 'resources/app.asar'), 'verified': True, 'contract': f.CONTRACT, 'platform': 'win32',
            'synthetic': True, 'live_accounts_tested': False, 'user_data_touched': False,
            'desktop': {'pid': self.process.pid + 1, 'executable': str(executable), 'sha256': f.legacy.file_sha256(executable)},
            'core': {'pid': self.process.pid, 'parent_pid': self.process.pid + 1, 'executable': str(self.core_executable), 'sha256': f.legacy.file_sha256(self.core_executable)},
            'core_frozen': True, 'core_upgrade_manifest_sha256': 'a' * 64,
            'seed_pid': self.manifest['seed_pid'], 'seed_completed_perf_ns': self.manifest['seed_completed_perf_ns'],
            'launch_perf_ns': self.launch_perf_ns, 'ready_perf_ns': self.ready_perf_ns, 'chronology_clock': f.legacy.CHRONOLOGY_CLOCK,
            'manifest_sha256': self.manifest_sha256, 'input_database_sha256': self.manifest['database_sha256'], 'nonce': self.manifest['nonce'],
            'normal_shutdown': True, 'manifest_unchanged': True}

    def validate(self, proof):
        with patch.object(probe, 'source_binding', return_value=copy.deepcopy(self.source_identity)):
            return probe.validate_proof(proof, executable=self.executable, core_executable=self.core_executable)

    def test_installed_receipt_requires_every_api_check_and_process_binding(self):
        proof = self.proof(); self.validate(proof)
        for key in proof:
            damaged = copy.deepcopy(proof); damaged.pop(key)
            with self.subTest(missing=key), self.assertRaises((RuntimeError, KeyError)): self.validate(damaged)
        for flag in probe.FLAGS:
            for change in ('missing', False, 1):
                damaged = copy.deepcopy(proof)
                if change == 'missing': damaged['checks'].pop(flag)
                else: damaged['checks'][flag] = change
                with self.subTest(flag=flag, change=change), self.assertRaises(RuntimeError): self.validate(damaged)
        for path, value in ((('source_commit',), 'b' * 40), (('source_manifest_sha256',), 'b' * 64), (('desktop_app_asar_sha256',), 'b' * 64), (('core', 'parent_pid'), -1), (('core', 'sha256'), 'b' * 64),
                (('desktop', 'pid'), True), (('core_frozen',), False), (('normal_shutdown',), False),
                (('manifest_unchanged',), False), (('platform',), 'linux'),
                (('launch_perf_ns',), self.manifest['seed_completed_perf_ns']),
                (('ready_perf_ns',), self.launch_perf_ns), (('safety_fence',), 'no-op'),
                (('persisted_state', 'withdrawal_audits'), 0), (('startup', 'posting_queue_revision'), True),
                (('persisted_state', 'no_submission'), False)):
            damaged = copy.deepcopy(proof); target = damaged
            for key in path[:-1]: target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(RuntimeError): self.validate(damaged)

    def test_independent_post_exit_oracle_rejects_corruption_and_closes_windows_handles(self):
        mutations = (
            "UPDATE posting_jobs SET caption='lost' WHERE id='offline-r64-queued-post'",
            "UPDATE posting_jobs SET queue_revision=0 WHERE id='offline-r64-queued-post'",
            "UPDATE posting_jobs SET attempt_id='shared' WHERE id='offline-r64-queued-post'",
            'DELETE FROM posting_withdraw_history', 'DELETE FROM posting_receipts',
            'DELETE FROM task_results', 'DELETE FROM task_checkpoints', 'DELETE FROM global_seen',
            'DELETE FROM task_list_dismissals', "UPDATE tasks SET status='paused'",
            "UPDATE studio_jobs SET result_json=json_set(result_json,'$.counts.like',99)",
            "UPDATE studio_jobs SET result_json=json_set(result_json,'$.window_cleanup.lease_token','changed')",
            "UPDATE posting_assets SET render_sha256='changed'", 'UPDATE auth_sessions SET auto_login=0')
        for sql in mutations:
            with self.subTest(sql=sql), tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary) / 'copy'; shutil.copytree(self.root, target)
                with closing(sqlite3.connect(target / 'collector.sqlite3')) as c, c: c.execute(sql)
                with self.assertRaises(sqlite3.ProgrammingError): c.execute('SELECT 1')
                with self.assertRaises(RuntimeError): f.inspect(target, self.manifest, phase='complete')

    def test_startup_oracle_allows_only_strict_zero_queue_revision_and_empty_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); _, manifest = f.seed(root)
            with closing(sqlite3.connect(root / 'collector.sqlite3')) as c, c:
                c.execute('ALTER TABLE posting_jobs ADD COLUMN queue_revision INTEGER NOT NULL DEFAULT 0')
                c.execute('CREATE TABLE posting_withdraw_history(id TEXT,job_id TEXT,owner_user_id TEXT,profile_id TEXT,expected_username TEXT,expected_actor_id TEXT,withdrawn_at TEXT)')
            f.inspect(root, manifest, phase='startup')
            with closing(sqlite3.connect(root / 'collector.sqlite3')) as c, c: c.execute('UPDATE posting_jobs SET queue_revision=1')
            with self.assertRaisesRegex(RuntimeError, 'Startup'): f.inspect(root, manifest, phase='startup')

    def test_renderer_selection_is_exact_and_never_uses_first_account_target(self):
        root = Path('/installed')
        foreign = Mock(url='https://www.instagram.com/')
        selected = Mock(url='file:///installed/resources/app.asar/renderer/dist/index.html')
        selected.evaluate.return_value = 'function'
        browser = Mock(contexts=[Mock(pages=[foreign, selected])])
        self.assertIs(selected, probe.exact_renderer(browser, root)); foreign.evaluate.assert_not_called()
        for pages in ([foreign], [selected, selected], [Mock(url='file:///other/resources/app.asar/renderer/dist/index.html')]):
            with self.assertRaises(RuntimeError): probe.exact_renderer(Mock(contexts=[Mock(pages=pages)]), root)


class InstalledRecoveryR64IpcTests(InstalledRecoveryR64Tests):
    IPC_MODE = True


class InstalledRecoveryProcessTests(unittest.TestCase):
    def test_failed_tree_kill_falls_back_and_checks_exact_core_identity(self):
        process = Mock(pid=65432); process.poll.return_value = None
        core = {'pid':65433, 'parent_pid':65432, 'executable':r'C:\Synthetic Install\collector_core.exe'}
        with patch.object(probe.subprocess, 'run', side_effect=[subprocess.CalledProcessError(1, ['taskkill']), Mock(returncode=0)]) as run:
            probe.stop_owned_runtime(process, core)
        process.kill.assert_called_once(); process.wait.assert_called_once_with(timeout=10)
        self.assertEqual(['taskkill.exe','/PID','65432','/T','/F'], run.call_args_list[0].args[0])
        self.assertTrue(run.call_args_list[0].kwargs['check'])
        command = run.call_args_list[1].args[0][-1]
        for value in ('ProcessId = 65433', 'ParentProcessId -ne 65432', 'ExecutablePath -ine', core['executable']): self.assertIn(value, command)
        self.assertTrue(run.call_args_list[1].kwargs['check'])

    def test_exited_desktop_still_checks_and_reaps_only_its_observed_core(self):
        process = Mock(pid=65432); process.poll.return_value = 0
        with patch.object(probe.subprocess, 'run') as run:
            probe.stop_owned_runtime(process, {'pid':65433,'parent_pid':65432,'executable':'C:/synthetic/core.exe'})
        self.assertEqual(1, run.call_count); process.kill.assert_not_called()
        with patch.object(probe.subprocess, 'run') as run, self.assertRaises(RuntimeError):
            probe.stop_owned_runtime(process, {'pid':65433,'parent_pid':999,'executable':'C:/synthetic/core.exe'})
        run.assert_not_called()

    def test_poisoned_environment_cannot_redirect_or_authenticate_proof(self):
        with patch.dict(os.environ, {'IGAC_DB_PATH':'real-user-db','IGAC_STARTUP_TOKEN':'secret','COLLECTOR_CORE_PORT':'1234','ELECTRON_RUN_AS_NODE':'1','OPENAI_API_KEY':'secret','PYTHONPATH':'elsewhere'}):
            env = probe.environment()
        for key in ('IGAC_DB_PATH','IGAC_STARTUP_TOKEN','COLLECTOR_CORE_PORT','ELECTRON_RUN_AS_NODE','OPENAI_API_KEY','PYTHONPATH'): self.assertNotIn(key, env)


if __name__ == '__main__': unittest.main()
