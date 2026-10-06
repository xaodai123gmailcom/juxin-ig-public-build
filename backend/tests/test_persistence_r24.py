from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.cloud_workspace import CloudWorkspace, decode_workspace, export_workspace, import_workspace
from app.database import Database
from app.errors import ValidationError
from app.service import CoreService


class PersistenceR24Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db, self.service, self.owner = self.make_database('source')

    def make_database(self, name):
        db = Database(self.root / f'{name}.sqlite3')
        db.initialize()
        service = CoreService(db)
        owner = service.register_user(name, 'correct horse battery staple')['id']
        return db, service, owner

    def task(self, service, owner, name):
        return service.create_task(owner, name=name, modes=['followers'], targets=[name], settings={})

    def payload(self, edit):
        data = decode_workspace(export_workspace(self.db, self.owner, self.root)[0])
        edit(data['tables'])
        raw = json.dumps(data).encode()
        return {'format': 1, 'encoding': 'zlib+base64', 'sha256': hashlib.sha256(raw).hexdigest(),
                'data': base64.b64encode(zlib.compress(raw)).decode()}

    def test_cloud_login_does_not_hold_session_lock_while_waiting_for_database(self):
        # Recovery calls check_active under a write transaction. Login must let
        # it acquire the session lock even while login waits for that transaction.
        class API:
            def request(self, *args, **kwargs):
                return {'access_token': 'test-access', 'refresh_token': 'test-refresh',
                        'user': {'id': 'test-cloud-user', 'email': 'test@example.test'}}

        cloud = CloudWorkspace(self.service, self.root, API())
        entered = threading.Event()
        original_write = self.db.write
        errors = []

        @contextmanager
        def marked_write():
            entered.set()
            with original_write() as connection:
                yield connection

        def login():
            try:
                cloud.command(self.owner, {'action': 'login', 'email': 'test@example.test', 'password': 'test'})
            except BaseException as exc:
                errors.append(exc)

        with patch.object(self.db, 'write', marked_write):
            self.db._write_lock.acquire()
            thread = threading.Thread(target=login)
            try:
                thread.start()
                self.assertTrue(entered.wait(3), 'login did not reach the write transaction')
                acquired = cloud.lock.acquire(timeout=0.3)
                if acquired:
                    cloud.lock.release()
                self.assertTrue(acquired, 'login holds session lock while waiting for DB: recovery deadlock')
            finally:
                self.db._write_lock.release()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(cloud.status(self.owner)['signed_in'])

    def test_restore_cannot_attach_target_to_existing_foreign_task(self):
        self.task(self.service, self.owner, 'source_account')
        destination, service, owner = self.make_database('destination')
        foreign = service.register_user('foreign', 'correct horse battery staple')['id']
        foreign_task = self.task(service, foreign, 'foreign_account')
        payload = self.payload(lambda tables: tables['task_targets'][0].update(
            task_id=foreign_task['id'], queue_order=9))
        with self.assertRaises(ValidationError):
            import_workspace(destination, owner, self.root / 'restore', payload)
        with destination.read() as connection:
            self.assertEqual(1, connection.execute('SELECT count(*) FROM task_targets').fetchone()[0])
            self.assertEqual(0, connection.execute('SELECT count(*) FROM tasks WHERE owner_user_id=?', (owner,)).fetchone()[0])

    def test_restore_rejects_result_associated_with_a_different_task_target(self):
        first = self.task(self.service, self.owner, 'source_one')
        second = self.task(self.service, self.owner, 'source_two')
        self.service.record_result(self.owner, first['id'], first['targets'][0]['id'],
                                   username='observed_person', instagram_user_id='123456789', source_mode='followers', visibility='private',
                                   profile={'followers_count': 25}, screening={}, qualified=True)
        payload = self.payload(lambda tables: tables['task_results'][0].update(target_id=second['targets'][0]['id']))
        destination, _, owner = self.make_database('destination')
        with self.assertRaises(ValidationError):
            import_workspace(destination, owner, self.root / 'restore', payload)
        with destination.read() as connection:
            self.assertEqual(0, connection.execute('SELECT count(*) FROM tasks').fetchone()[0])

    def test_restore_cancelled_after_first_insert_rolls_back_all_business_records(self):
        self.task(self.service, self.owner, 'source_account')
        payload = self.payload(lambda tables: None)
        destination, _, owner = self.make_database('destination')
        checks = 0

        def check_active():
            nonlocal checks
            checks += 1
            if checks == 3:
                # A task row has been inserted, but the transaction is not yet
                # committed. Simulate logout or shutdown during restoration.
                raise InterruptedError('cloud session ended')

        with self.assertRaises(InterruptedError):
            import_workspace(destination, owner, self.root / 'restore', payload, check_active=check_active)
        with destination.read() as connection:
            for table in ('tasks', 'task_targets', 'instagram_accounts', 'global_seen', 'global_identity_owners'):
                self.assertEqual(0, connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0], table)
            self.assertEqual([], connection.execute('PRAGMA foreign_key_check').fetchall())


if __name__ == '__main__':
    unittest.main()
