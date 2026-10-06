"""Real archived-window projections and concurrent durable counter invariants."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from app.errors import NotFoundError, ValidationError
from app.service import CoreService
from test_spool_performance_r30 import CountingDatabase, seed_task, STAMP


OLD = "2025-01-01T00:00:00.000+00:00"


class StorageR31Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = CountingDatabase(Path(self.temp.name) / "storage.sqlite3")
        self.database.initialize()
        seed_task(self.database)
        self.service = CoreService(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def seed_archives(self, start, count):
        # Normal task completion retains this window id as historical evidence.
        with self.database.write() as connection:
            connection.executemany(
                "INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,"
                "current_window_id,current_stage,created_at,updated_at) "
                "VALUES(?,'task',?,?,?,'completed','reused-window','completed_archived',?,?)",
                ((f"archive{index:05d}", f"archive{index:05d}", f"archive{index:05d}", index+2, STAMP, STAMP)
                 for index in range(start, start+count)),
            )

    def live(self, ids=()):
        return self.service.get_task_live_status("owner", "task", current_target_ids=ids)

    def test_archived_window_bindings_do_not_turn_heartbeat_into_lifetime_history(self):
        self.seed_archives(0, 1000)
        with self.database.write() as connection:
            connection.execute("UPDATE task_targets SET current_window_id='reused-window',updated_at=? WHERE id='target'", (OLD,))
        small, small_steps = self.database.measure(lambda: self.live(("target",)))
        self.seed_archives(1000, 19_000)
        large, large_steps = self.database.measure(lambda: self.live(("target",)))
        self.assertTrue(large["targets_partial"])
        self.assertEqual(201, len(small["targets"]))
        self.assertEqual(201, len(large["targets"]))
        self.assertIn("target", {row["id"] for row in large["targets"]})
        self.assertLess(len(json.dumps(large).encode()), 150_000)
        self.assertLessEqual(large_steps, small_steps * 3 + 2000, (small_steps, large_steps))
        # Source/audit rows and the old full-read API remain intact.
        self.assertEqual(20_001, len(self.service.get_task("owner", "task")["targets"]))
        self.assertEqual(20_001, len(self.service.get_task_live_status("owner", "task")["targets"]))

    def test_runtime_terminal_cleanup_targets_are_retained_regardless_of_age(self):
        self.seed_archives(0, 500)
        with self.database.write() as connection:
            for index, status in enumerate(("completed", "failed", "stopped")):
                connection.execute(
                    "INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,"
                    "current_window_id,current_stage,created_at,updated_at) "
                    "VALUES(?,'task',?,?,?,?,'reused-window','cleanup',?,?)",
                    (f"cleanup{index}", f"cleanup{index}", f"cleanup{index}", 1000+index, status, OLD, OLD),
                )
        protected = ("cleanup0", "cleanup1", "cleanup2")
        live = self.live(protected)
        selected = {row["id"]: row for row in live["targets"]}
        self.assertTrue(set(protected) <= selected.keys())
        self.assertEqual(["completed", "failed", "stopped"], [selected[key]["status"] for key in protected])
        self.assertTrue(all(selected[key]["current_window_id"] == "reused-window" for key in protected))

    def test_new_nonterminal_claim_after_runtime_sampling_is_not_lost(self):
        self.seed_archives(0, 500)
        runtime_ids = ("target",)
        with self.database.write() as connection:
            for index, status in enumerate(("running", "waiting_network", "paused", "recoverable")):
                connection.execute(
                    "INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,"
                    "current_window_id,current_stage,created_at,updated_at) "
                    "VALUES(?,'task',?,?,?,?,'newly-claimed-window','screening_accounts',?,?)",
                    (f"claimed{index}", f"claimed{index}", f"claimed{index}", 1000+index, status, OLD, OLD),
                )
        live = self.live(runtime_ids)
        self.assertTrue({"claimed0", "claimed1", "claimed2", "claimed3"}
                        <= {row["id"] for row in live["targets"]})

    def test_runtime_ids_above_999_sql_parameter_limit_remain_task_scoped(self):
        self.seed_archives(0, 3000)
        seed_task(self.database, task="same-owner-task", target="same-owner-other-target")
        seed_task(self.database, task="foreign-task", target="foreign-target", owner="foreign-owner")
        ids = [f"archive{index:05d}" for index in range(1200)] + ["same-owner-other-target", "foreign-target", "missing"]
        live = self.live(ids)
        selected = {row["id"] for row in live["targets"]}
        self.assertEqual(1400, len(selected))  # runtime 1200 + recent 200
        self.assertTrue(set(ids[:1200]) <= selected)
        self.assertFalse({"same-owner-other-target", "foreign-target", "missing"} & selected)
        with self.assertRaises(NotFoundError):
            self.service.get_task_live_status("foreign-owner", "task", current_target_ids=ids)

    def test_partial_and_full_fields_and_progress_remain_equal_for_small_task(self):
        self.service.append_task_mode_candidates("owner", "task", "target", "followers", ["one", "two"])
        self.service.finish_task_mode_candidate("owner", "task", "target", "followers", "one", state="recorded")
        self.service.upsert_checkpoint("owner", "task", "target", mode="followers", stage="screening_accounts",
                                       cursor={}, counters={"source_total": 500, "discovered": 2, "processed": 1, "saved": 1})
        full = self.service.get_task("owner", "task")
        partial = self.live(("target",))
        self.assertEqual(full, {key: value for key, value in partial.items() if key != "targets_partial"})
        # A recorded spool row alone is not evidence of manual-review admission.
        # Keep the whole progress contract: every old counter and the new one.
        self.assertEqual({"source_total": 500, "discovered": 2, "processed": 1, "saved": 1,
                          "skipped_global_duplicates": 0, "qualified_for_review": 0,
                          "discarded": 0, "hover_discarded": 0},
                         partial["targets"][0]["mode_progress"]["followers"])

    def test_partial_and_full_progress_preserve_actual_review_admission_after_reopen(self):
        self.service.append_task_mode_candidates("owner", "task", "target", "followers", ["admitted", "pending"])
        claim = self.service.claim_workbench_identity(
            "owner", username="admitted", source="followers", source_target="target")
        profile = {"username": "admitted", "visibility": "private", "posts": 5}
        screening = {"review_tier": "primary", "routing_result": "private_review"}
        self.service.record_result("owner", "task", "target", username="admitted",
            instagram_user_id=None, source_mode="followers", visibility="private",
            profile=profile, screening=screening, qualified=True, dedupe_claim_id=claim["claim_id"])
        self.service.create_workbench_candidate("owner", claim_id=claim["claim_id"], username="admitted",
            visibility="private", profile=profile, screening=screening, review_cache={},
            source_mode="followers", source_target="target")
        self.service.reconcile_task_mode_candidates("owner", "task", "target", "followers")
        self.service.upsert_checkpoint("owner", "task", "target", mode="followers", stage="screening_accounts",
            cursor={}, counters={"source_total": 500, "discovered": 2, "processed": 1, "saved": 1})
        expected = {"source_total": 500, "discovered": 2, "processed": 1, "saved": 1,
                    "skipped_global_duplicates": 0, "qualified_for_review": 1,
                    "discarded": 0, "hover_discarded": 0}
        for restarted in (False, True):
            with self.subTest(restarted=restarted):
                if restarted:
                    self.database.initialize()
                    self.service = CoreService(self.database)
                full = self.service.get_task("owner", "task")
                partial = self.live(("target",))
                self.assertEqual(full, {key: value for key, value in partial.items() if key != "targets_partial"})
                self.assertEqual(expected, partial["targets"][0]["mode_progress"]["followers"])

    def test_status_read_and_live_id_validation_keep_owner_boundary(self):
        self.seed_archives(0, 1000)
        self.assertEqual("running", self.service.get_task_status("owner", "task"))
        with self.assertRaises(NotFoundError):
            self.service.get_task_status("foreign-owner", "task")
        for invalid in ("target", [None], [""], ["x" * 129]):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.live(invalid)

    def test_concurrent_duplicate_discovery_and_finish_keep_exact_counters(self):
        def writer(index):
            names = ["shared", f"window{index}.one", f"window{index}.two"]
            self.service.append_task_mode_candidates("owner", "task", "target", "followers", names)
            for name in names:
                self.service.finish_task_mode_candidate("owner", "task", "target", "followers", name,
                                                       state="recorded" if index % 2 else "deduped")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(writer, range(8)))
        stats = self.service.task_mode_candidate_stats("owner", "task", "target", "followers")
        with self.database.read() as connection:
            actual = dict(connection.execute(
                "SELECT COUNT(*) total,SUM(state='pending') pending,SUM(state='recorded') recorded,"
                "SUM(state='deduped') deduped FROM task_mode_candidates WHERE target_id='target' AND mode='followers'"
            ).fetchone())
        self.assertEqual(17, stats["total"])
        self.assertEqual(0, stats["pending"])
        self.assertEqual(stats, actual)
        self.assertEqual(stats["total"], stats["recorded"] + stats["deduped"])

    def test_rollback_and_reopen_preserve_candidate_counter_projection(self):
        self.service.append_task_mode_candidates("owner", "task", "target", "followers", ["one", "two"])
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with self.database.write() as connection:
                connection.execute("UPDATE task_mode_candidates SET state='recorded' WHERE username_norm='one'")
                raise RuntimeError("interrupted before commit")
        before = self.service.task_mode_candidate_stats("owner", "task", "target", "followers")
        self.assertEqual({"total": 2, "pending": 2, "recorded": 0, "deduped": 0}, before)
        self.database.initialize()
        after = CoreService(self.database).task_mode_candidate_stats("owner", "task", "target", "followers")
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
