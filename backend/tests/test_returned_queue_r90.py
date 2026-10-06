"""Unclaimed returned sources survive old target and parent terminal callbacks."""
import unittest

import test_split_recovery_fence_r72 as fixtures


class ReturnedQueueR90Tests(unittest.TestCase):
    setUp = fixtures.SplitRecoveryFenceTests.setUp
    tearDown = fixtures.SplitRecoveryFenceTests.tearDown
    returned = fixtures.SplitRecoveryFenceTests._returned

    def assert_waiting(self, task, target, waiting):
        row = self.service.list_split_candidates(self.owner, candidate_ids=[waiting["id"]])[0]
        self.assertEqual("queued", row["queue_state"])
        self.assertEqual(["window-b"], row["allowed_window_ids"])
        self.assertIsNone(row["queued_target_id"])
        self.assertEqual(target["id"], row["source_target_id"])
        self.assertEqual(2, self.service.get_checkpoint(
            self.owner, task["id"], target["id"], "followers")["counters"]["saved"])
        self.assertFalse(any(row["kind"] == "failure" for row in
                             self.service.list_split_candidates(self.owner)))

    def test_parent_terminal_replays_preserve_waiting_identity_affinity_and_checkpoint(self):
        task, target, waiting = self.returned(live=True, allowed=["window-b"])
        for status in ("recoverable", "stopped", "failed", "recoverable"):
            with self.subTest(status=status):
                self.service.finalize_task_runtime_status(self.owner, task["id"], status)
                self.assert_waiting(task, target, waiting)

    def test_late_old_target_failures_do_not_revoke_a_committed_return(self):
        task, target, waiting = self.returned(live=True, allowed=["window-b"])
        for status in ("recoverable", "failed", "stopped"):
            with self.subTest(status=status):
                self.service.set_target_runtime_status(
                    self.owner, task["id"], target["id"], status, window_id="window-a")
                self.assert_waiting(task, target, waiting)

    def test_b_claim_and_new_failure_remain_visible_as_a_new_failure_generation(self):
        task, target, waiting = self.returned(live=True, allowed=["window-b"])
        with self.db.read() as connection:
            first_id = connection.execute(
                "SELECT candidate_id FROM task_target_recovery_controls WHERE target_id=?",
                (target["id"],)).fetchone()[0]
        self.service.finalize_task_runtime_status(self.owner, task["id"], "recoverable")
        self.assert_waiting(task, target, waiting)
        self.service.set_task_runtime_status(self.owner, task["id"], "running")
        claimed = self.service.claim_next_split_candidate(self.owner, task["id"], "window-b")
        self.assertEqual(target["id"], claimed["id"])
        self.service.set_target_runtime_status(
            self.owner, task["id"], target["id"], "recoverable", window_id="window-b")
        self.service.finalize_task_runtime_status(self.owner, task["id"], "recoverable")
        failures = [row for row in self.service.list_split_candidates(self.owner) if row["kind"] == "failure"]
        self.assertEqual(1, len(failures))
        self.assertNotEqual(first_id, failures[0]["id"])
        self.assertEqual("window-b", failures[0]["source_window_id"])

    def test_missing_waiting_row_does_not_hide_an_unfinished_failure(self):
        task, target, waiting = self.returned(live=True, allowed=["window-b"])
        with self.db.write() as connection:
            connection.execute("DELETE FROM split_candidates WHERE id=?", (waiting["id"],))
        self.service.finalize_task_runtime_status(self.owner, task["id"], "failed")
        failures = [row for row in self.service.list_split_candidates(self.owner) if row["kind"] == "failure"]
        self.assertEqual(1, len(failures))
        self.assertEqual(target["id"], failures[0]["source_target_id"])

    def test_reinitialize_replaces_old_trigger_without_changing_existing_queue(self):
        task, target, waiting = self.returned(live=True, allowed=["window-b"])
        # Recreate the r89 parent guard: only an explicitly removed/stopped
        # window was protected. Normal automatic handoff cleanup did not match.
        with self.db.write() as connection:
            sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='trg_split_candidate_task_terminal'").fetchone()[0]
            old = sql.replace("target.status IN ('pending', 'failed', 'recoverable', 'stopped')",
                              "target.status='stopped' AND target.current_stage='interrupted_recoverable' AND target.preferred_window_id IS NULL")
            self.assertNotEqual(sql, old)
            connection.execute("DROP TRIGGER trg_split_candidate_task_terminal")
            connection.execute(old)
        for _ in range(2):
            self.db.initialize()
            self.assert_waiting(task, target, waiting)
            self.service.finalize_task_runtime_status(self.owner, task["id"], "recoverable")
            self.assert_waiting(task, target, waiting)
        with self.db.read() as connection:
            self.assertEqual(set(range(1, 42)), {row[0] for row in connection.execute("SELECT version FROM schema_migrations")})


if __name__ == "__main__":
    unittest.main()
