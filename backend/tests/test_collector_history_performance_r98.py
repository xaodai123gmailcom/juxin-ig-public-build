"""Recognition keeps durable history without scanning unrelated archived people."""
from pathlib import Path
import tempfile
import unittest

from app.service import CoreService
from test_spool_performance_r30 import CountingDatabase, seed_task, STAMP


class CollectorHistoryPerformanceR98Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = CountingDatabase(Path(self.temp.name) / "history.sqlite3")
        self.database.initialize()
        seed_task(self.database)
        self.service = CoreService(self.database)
        self.claim_args = dict(username="still.pending", source="followers",
                               source_target="target", allow_owned_resume=True)
        self.claim = self.service.claim_workbench_identity("owner", **self.claim_args)

    def tearDown(self):
        self.temp.cleanup()

    def seed_archive(self, count):
        with self.database.write() as connection:
            connection.executemany(
                "INSERT INTO instagram_accounts(id,current_username_norm,current_username_display,"
                "first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                ((f"archive-{index}", f"archived{index}", f"archived{index}", STAMP, STAMP)
                 for index in range(count)),
            )
            connection.executemany(
                "INSERT INTO task_result_duplicate_archive(original_result_id,owner_user_id,"
                "task_id,target_id,account_id,sources_json,visibility,profile_json,screening_json,"
                "created_at,updated_at,archived_at) "
                "VALUES(?,'owner','task','target',?,'[\"followers\"]','private','{}','{}',?,?,?)",
                ((f"saved-{index}", f"archive-{index}", STAMP, STAMP, STAMP)
                 for index in range(count)),
            )

    def test_pending_claim_and_hover_probe_ignore_unrelated_archive_size(self):
        operations = (
            lambda: self.service.claim_workbench_identity("owner", **self.claim_args),
            lambda: self.service.should_skip_relationship_hover(
                "owner", "still.pending", source="followers", source_target="target"),
        )
        before = [self.database.measure(operation)[1] for operation in operations]
        self.seed_archive(20_000)
        for operation, small_steps in zip(operations, before):
            result, large_steps = self.database.measure(operation)
            self.assertLessEqual(large_steps, small_steps * 3 + 2000,
                                 (small_steps, large_steps))
            if isinstance(result, dict):
                self.assertTrue(result["resumed"])
                self.assertEqual(self.claim["claim_id"], result["claim_id"])
            else:
                self.assertFalse(result)
        with self.database.read() as connection:
            self.assertEqual(20_000, connection.execute(
                "SELECT COUNT(*) FROM task_result_duplicate_archive").fetchone()[0])

    def test_archive_only_identity_still_dedupes_without_registry_membership(self):
        self.seed_archive(20_000)
        result, steps = self.database.measure(
            lambda: self.service.check_global_dedupe("archived19999"))
        self.assertTrue(result["seen"])
        self.assertLess(steps, 2000)
        claimed, steps = self.database.measure(lambda: self.service.claim_workbench_identity(
            "owner", username="archived19999", source="followers", source_target="target",
            allow_owned_resume=True))
        self.assertTrue(claimed["duplicate"])
        self.assertLess(steps, 3000)


if __name__ == "__main__":
    unittest.main()
