"""Shared posting fences survive refresh/restart and cannot steal other work."""
from pathlib import Path
import tempfile
import unittest
from app.database import Database
from app.service import CoreService
from app.errors import ConflictError

class PostingLeaseIntegrationR6Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'test.sqlite3'); self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user('posting_owner', 'fixture password only')['id']
        self.other = self.service.register_user('other_owner', 'fixture password only')['id']
        with self.db.write() as c:
            c.execute('CREATE TABLE posting_jobs(id TEXT PRIMARY KEY,owner_user_id TEXT,profile_id TEXT,lease_token TEXT,status TEXT)')
            c.execute("INSERT INTO posting_jobs VALUES('job',?,'window-a',NULL,'queued')", (self.owner,))

    def lease(self):
        token = self.service._acquire_browser_lease_record(self.owner, 'window-a', operation_type='posting', entity_id='job')
        with self.db.write() as c:
            c.execute('UPDATE posting_jobs SET lease_token=? WHERE id=?', (token,'job'))
            c.execute("UPDATE browser_operation_leases SET expires_at='2000-01-01T00:00:00+00:00',heartbeat_at='2000-01-01T00:00:00+00:00'")
        self.db.live_browser_lease_tokens.clear()  # Simulated Core restart.
        return token

    def states(self):
        return self.service.list_browser_lease_states(self.owner, active_posting_entity_ids=set(), inactive_grace_seconds=0)

    def test_every_durable_pending_or_cleanup_state_ignores_ttl(self):
        self.lease()
        for status in ('running','submitting','needs_review','cleanup_pending','failed','completed','cancelled'):
            with self.subTest(status=status):
                with self.db.write() as c:c.execute('UPDATE posting_jobs SET status=?', (status,))
                self.assertEqual('posting', self.states()[0]['operation_type'])
                with self.assertRaises(ConflictError):
                    self.service._acquire_browser_lease_record(self.other, 'window-a', operation_type='studio', entity_id='sibling')

    def test_unknown_or_mismatched_owner_generation_never_looks_free(self):
        self.lease()
        for update in ("lease_token='wrong-generation'", "owner_user_id='unknown-owner'", "profile_id='wrong-window'"):
            with self.db.write() as c:c.execute('UPDATE posting_jobs SET '+update)
            self.assertEqual(1, len(self.states()))
        with self.db.write() as c:c.execute('DELETE FROM posting_jobs')
        self.assertEqual(1, len(self.states()))
        with self.db.write() as c:c.execute('DROP TABLE posting_jobs')
        self.assertEqual(1, len(self.states()))

    def test_only_terminal_token_free_expired_remnant_can_be_reaped(self):
        self.lease()
        with self.db.write() as c:c.execute("UPDATE posting_jobs SET lease_token=NULL,status='needs_review'")
        self.assertEqual(1,len(self.states()))
        with self.db.write() as c:c.execute("UPDATE posting_jobs SET status='completed'")
        self.assertEqual([],self.states())

    def test_admission_requires_owned_job_and_exact_window(self):
        for owner, profile, job in ((self.other,'window-a','job'), (self.owner,'window-b','job'), (self.owner,'window-a','absent')):
            with self.assertRaises(ConflictError):
                self.service._acquire_browser_lease_record(owner, profile, operation_type='posting', entity_id=job)
        with self.db.read() as c:self.assertEqual(0,c.execute('SELECT COUNT(*) FROM browser_operation_leases').fetchone()[0])

    def test_release_exact_generation_does_not_release_sibling(self):
        token=self.lease()
        sibling=self.service._acquire_browser_lease_record(self.owner,'window-b',operation_type='studio',entity_id='sibling')
        self.service.release_browser_lease('window-a','wrong-generation')
        self.assertEqual(2,len(self.states()))
        self.service.release_browser_lease('window-a',token)
        with self.db.read() as c:
            rows=c.execute('SELECT profile_id,lease_token FROM browser_operation_leases').fetchall()
        self.assertEqual([('window-b',sibling)],[tuple(row) for row in rows])

    def test_missing_browser_row_does_not_erase_durable_posting_hold(self):
        token=self.lease()
        with self.db.write() as c:c.execute('DELETE FROM browser_operation_leases')
        for operation in ('collection','studio','monitor','action','account','posting'):
            with self.subTest(operation=operation), self.assertRaises(ConflictError):
                self.service._acquire_browser_lease_record(self.owner,'window-a',operation_type=operation,entity_id='job')
        with self.db.read() as c:self.assertEqual(token,c.execute('SELECT lease_token FROM posting_jobs').fetchone()[0])

    def test_generic_startup_keeps_posting_fence(self):
        token=self.lease()
        self.service.recover_interrupted_operations()
        with self.db.read() as c:
            row=c.execute("SELECT lease_token FROM browser_operation_leases WHERE operation_type='posting'").fetchone()
        self.assertEqual(token,row[0])
