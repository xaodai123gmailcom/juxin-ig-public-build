"""Exercise the real cloud class with isolated SQLite and blocked fake HTTP.

Only the class AST is executed. No application module, credentials, network
client, OpenVINO package, or background backup worker is imported or started.
"""
import ast
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[2] / 'backend/app/cloud_workspace.py'
parsed = ast.parse(SOURCE.read_text(encoding='utf-8'))
class_node = next(node for node in parsed.body
                  if isinstance(node, ast.ClassDef) and node.name == 'CloudWorkspace')
namespace = dict(threading=threading, time=time, ValidationError=ValueError,
                 ConflictError=RuntimeError)
exec(compile(ast.Module(body=[class_node], type_ignores=[]), str(SOURCE), 'exec'), namespace)
CloudWorkspace = namespace['CloudWorkspace']


class Database:
    def __init__(self, path):
        self.path = path
        self.writer_entered = threading.Event()
        self.writer_release = threading.Event(); self.writer_release.set()
        self.commit_entered = threading.Event()
        self.commit_release = threading.Event(); self.commit_release.set()
        self.fail_entry = False
        self.fail_commit = False
        self.before_commit = None
        with self.read() as connection:
            connection.execute('''CREATE TABLE cloud_workspace_links(
                owner_user_id TEXT PRIMARY KEY, cloud_user_id TEXT UNIQUE,
                email TEXT, project_url TEXT, revision INTEGER DEFAULT 0,
                digest TEXT DEFAULT '', last_sync_at TEXT)''')
            connection.commit()

    @contextmanager
    def read(self):
        connection = sqlite3.connect(self.path, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write(self):
        self.writer_entered.set()
        if not self.writer_release.wait(2):
            raise TimeoutError('test writer release missing')
        if self.fail_entry:
            raise RuntimeError('test writer entry failed')
        with self.read() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                yield connection
                self.commit_entered.set()
                if not self.commit_release.wait(2):
                    raise TimeoutError('test commit release missing')
                if self.before_commit:
                    self.before_commit()
                if self.fail_commit:
                    raise RuntimeError('test commit failed')
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def rows(self):
        with self.read() as connection:
            return connection.execute('SELECT count(*) FROM cloud_workspace_links').fetchone()[0]


class API:
    configured = enabled = True
    project_url = 'https://example.invalid'

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def request(self, path, **kwargs):
        if path != '/auth/v1/token?grant_type=password':
            raise AssertionError('unexpected fake endpoint')
        self.calls += 1
        self.entered.set()
        if not self.release.wait(2):
            raise TimeoutError('test HTTP release missing')
        return {'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh',
                'user': {'id': 'synthetic-cloud-owner', 'email': 'owner@example.invalid'},
                'expires_in': 3600}


class CloudSessionFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cloud-fence-')
        self.db = Database(str(Path(self.temp.name) / 'fixture.sqlite'))
        self.api = API()
        self.cloud = CloudWorkspace(SimpleNamespace(database=self.db), Path(self.temp.name), self.api)
        self.threads = []

    def tearDown(self):
        self.api.release.set(); self.db.writer_release.set(); self.db.commit_release.set()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'owned test thread did not stop')
        self.temp.cleanup()

    def begin_login(self):
        result = {}
        def login():
            try:
                result['value'] = self.cloud.command('owner', {'action': 'login',
                    'email': 'owner@example.invalid', 'password': 'synthetic-password'})
            except BaseException as error:
                result['error'] = error
        thread = threading.Thread(target=login)
        self.threads.append(thread); thread.start()
        self.assertTrue(self.api.entered.wait(1))
        return thread, result

    def assert_cancelled(self, thread, result):
        thread.join(2); self.assertFalse(thread.is_alive())
        self.assertIsInstance(result.get('error'), InterruptedError)
        self.assertNotIn('owner', self.cloud.sessions)
        self.assertFalse(self.cloud.wake.is_set())
        self.assertEqual(0, self.db.rows())

    def test_logout_during_http_prevents_mapping_session_and_backup(self):
        thread, result = self.begin_login()
        self.assertFalse(self.cloud.command('owner', {'action': 'logout'})['signed_in'])
        self.api.release.set()
        self.assert_cancelled(thread, result)

    def test_logout_also_invalidates_pending_replacement_login(self):
        self.cloud.sessions['owner'] = {'access_token': 'synthetic-old'}
        thread, result = self.begin_login()
        self.assertFalse(self.cloud.command('owner', {'action': 'logout'})['signed_in'])
        self.api.release.set()
        self.assert_cancelled(thread, result)

    def test_logout_during_db_writer_wait_does_not_wait_for_writer(self):
        self.db.writer_release.clear()
        thread, result = self.begin_login(); self.api.release.set()
        self.assertTrue(self.db.writer_entered.wait(1))
        self.assertFalse(self.cloud.command('owner', {'action': 'logout'})['signed_in'])
        self.db.writer_release.set()
        self.assert_cancelled(thread, result)

    def test_logout_cannot_acknowledge_between_commit_and_publication(self):
        self.db.commit_release.clear()
        thread, result = self.begin_login(); self.api.release.set()
        self.assertTrue(self.db.commit_entered.wait(1))
        acknowledged = threading.Event()
        def logout():
            self.cloud.command('owner', {'action': 'logout'})
            acknowledged.set()
        logout_thread = threading.Thread(target=logout)
        self.threads.append(logout_thread); logout_thread.start()
        self.assertFalse(acknowledged.wait(.05))
        self.db.commit_release.set()
        thread.join(2); logout_thread.join(2)
        self.assertTrue(acknowledged.is_set())
        self.assertNotIn('owner', self.cloud.sessions)
        self.assertFalse(self.cloud.status('owner')['signed_in'])
        self.assertEqual(1, self.db.rows())

    def test_db_failures_never_publish_and_release_lock(self):
        for flag in ('fail_entry', 'fail_commit'):
            with self.subTest(stage=flag):
                setattr(self.db, flag, True); self.api.release.set()
                thread, result = self.begin_login(); thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(result.get('error'), RuntimeError)
                self.assertEqual(0, self.db.rows())
                self.assertNotIn('owner', self.cloud.sessions)
                self.assertFalse(self.cloud.wake.is_set())
                acquired = []
                def probe():
                    ok = self.cloud.lock.acquire(timeout=1); acquired.append(ok)
                    if ok: self.cloud.lock.release()
                probe_thread = threading.Thread(target=probe)
                self.threads.append(probe_thread); probe_thread.start(); probe_thread.join(2)
                self.assertEqual([True], acquired)
                setattr(self.db, flag, False)

    def test_shutdown_before_commit_does_not_publish_credentials(self):
        self.db.before_commit = self.cloud.stop_event.set
        self.api.release.set(); thread, result = self.begin_login(); thread.join(2)
        self.assertIsInstance(result.get('error'), InterruptedError)
        self.assertNotIn('owner', self.cloud.sessions)
        self.assertFalse(self.cloud.wake.is_set())

    def test_intentional_new_login_after_logout_can_succeed(self):
        self.cloud.command('owner', {'action': 'logout'})
        self.api.release.set(); thread, result = self.begin_login(); thread.join(2)
        self.assertNotIn('error', result)
        self.assertTrue(result['value']['signed_in'])
        self.assertEqual(1, self.db.rows())
        self.assertTrue(self.cloud.wake.is_set())

    def test_new_login_after_shutdown_does_not_call_http(self):
        self.cloud.shutdown()
        with self.assertRaises(InterruptedError):
            self.cloud.command('owner', {'action': 'login'})
        self.assertEqual(0, self.api.calls)


if __name__ == '__main__':
    unittest.main()
