"""Measure real SQLite VM work and preserve identity semantics during repair."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.database import Database
from app.errors import NotFoundError, ValidationError
from app.service import CoreService


STAMP = "2026-09-16T00:00:00.000+00:00"


class CountingDatabase(Database):
    def __init__(self, path):
        super().__init__(path)
        self.measuring = False
        self.vm_steps = 0

    def _connect(self):
        connection = super()._connect()
        # Exercise the historical host-parameter ceiling supported by this app.
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)

        def count():
            if self.measuring:
                self.vm_steps += 100
            return 0

        connection.set_progress_handler(count, 100)
        return connection

    def measure(self, action):
        self.vm_steps = 0
        self.measuring = True
        try:
            result = action()
        finally:
            self.measuring = False
        return result, self.vm_steps


def seed_task(database, *, task="task", target="target", owner="owner", recognition=True):
    with database.write() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO app_users(id,username_norm,username_display,password_hash,created_at) "
            "VALUES(?,?,?,?,?)", (owner, owner, owner, "unused-test-hash", STAMP),
        )
        connection.execute(
            "INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) "
            "VALUES(?,?,?,'running','[\"followers\"]',?,?,?)",
            (task, owner, task, json.dumps({"local_person_recognition": recognition}), STAMP, STAMP),
        )
        connection.execute(
            "INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,created_at,updated_at) "
            "VALUES(?,?,?,?,1,'running',?,?)", (target, task, target, target, STAMP, STAMP),
        )


def seed_pending(database, start, count):
    with database.write() as connection:
        connection.executemany(
            "INSERT INTO task_mode_candidates(target_id,mode,username_norm,username_display,"
            "discovery_order,discovered_at,updated_at) VALUES('target','followers',?,?,?,?,?)",
            ((f"pending{index:06d}", f"pending{index:06d}", index+1, STAMP, STAMP)
             for index in range(start, start+count)),
        )


class SpoolPerformanceR30Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = CountingDatabase(Path(self.temp.name) / "spool.sqlite3")
        self.database.initialize()
        seed_task(self.database)
        self.service = CoreService(self.database)
        self.args = ("owner", "task", "target", "followers")

    def tearDown(self):
        self.temp.cleanup()

    def append(self, *names):
        return self.service.append_task_mode_candidates(*self.args, names)

    def repair(self, *names):
        return self.service.reconcile_task_mode_candidates(*self.args, usernames=names)

    def claim(self, username):
        return self.service.claim_workbench_identity(
            "owner", username=username, source="followers", source_target="target",
            allow_owned_resume=True,
        )

    def record(self, username, *, claim=None, task="task", target="target", owner="owner"):
        return self.service.record_result(
            owner, task, target, username=username, instagram_user_id=None,
            source_mode="followers", visibility="private", profile={"marker": username},
            screening={}, qualified=None, dedupe_claim_id=claim["claim_id"] if claim else None,
        )

    def rows(self, table):
        with self.database.read() as connection:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def test_append_vm_work_depends_on_batch_not_accumulated_pending_queue(self):
        for recognition in (False, True):
            with self.subTest(recognition=recognition):
                database = CountingDatabase(Path(self.temp.name) / f"scale-{recognition}.sqlite3")
                database.initialize()
                seed_task(database, recognition=recognition)
                service = CoreService(database)
                seed_pending(database, 0, 1000)
                small, small_steps = database.measure(lambda: service.append_task_mode_candidates(
                    *self.args, [f"first{index}" for index in range(10)]))
                seed_pending(database, 1010, 20_000)
                large, large_steps = database.measure(lambda: service.append_task_mode_candidates(
                    *self.args, [f"second{index}" for index in range(10)]))
                self.assertEqual(1010, small["total"])
                self.assertEqual(21_020, large["total"])
                # VM instruction counts expose scans even on fast disks. The old
                # IN clause alone passed textual tests but grew roughly 20-fold.
                self.assertLessEqual(large_steps, small_steps * 3 + 2000,
                                     (small_steps, large_steps, recognition))

    def test_pending_page_and_terminal_write_keep_bounded_work(self):
        seed_pending(self.database, 0, 30_000)
        page, read_steps = self.database.measure(lambda: self.service.list_pending_task_mode_candidates(
            *self.args, after_discovery_order=29_980, limit=10))
        self.assertEqual(list(range(29_981, 29_991)), [row["discovery_order"] for row in page])
        finished, finish_steps = self.database.measure(lambda: self.service.finish_task_mode_candidate(
            *self.args, page[0]["username"], state="deduped"))
        self.assertEqual("deduped", finished["state"])
        self.assertLess(read_steps, 2000)
        self.assertLess(finish_steps, 2000)
        repeated = self.service.finish_task_mode_candidate(*self.args, page[0]["username"], state="recorded")
        self.assertEqual(finished, repeated)

    def test_failure_repair_does_not_scan_or_finish_unrelated_candidates(self):
        self.append("unread.person", "unrelated.saved")
        unread = self.claim("unread.person")
        unrelated = self.claim("unrelated.saved")
        self.record("unrelated.saved", claim=unrelated)
        _, small_steps = self.database.measure(lambda: self.repair("@UNREAD.PERSON"))
        seed_pending(self.database, 2, 30_000)
        stats, large_steps = self.database.measure(lambda: self.repair("unread.person"))
        self.assertEqual(30_002, stats["pending"])
        self.assertLessEqual(large_steps, small_steps * 3 + 2000, (small_steps, large_steps))
        self.assertEqual([], self.rows("workbench_candidates"))
        resumed = self.claim("unread.person")
        self.assertTrue(resumed["resumed"])
        self.assertEqual(unread["claim_id"], resumed["claim_id"])
        # Full resume still reconciles every crash gap; the optional scope did not
        # replace or weaken that recovery operation.
        full = self.service.reconcile_task_mode_candidates(*self.args)
        self.assertEqual(1, full["recorded"])
        self.assertEqual(30_001, full["pending"])

    def test_alias_expansion_repairs_first_saved_alias_across_parameter_batches(self):
        aliases = [f"alias{index:04d}" for index in range(1100)]
        with self.database.write() as connection:
            connection.executemany(
                "INSERT INTO task_mode_candidates(target_id,mode,username_norm,username_display,"
                "discovery_order,discovered_at,updated_at) VALUES('target','followers',?,?,?,?,?)",
                ((name, name, index+1, STAMP, STAMP) for index, name in enumerate(aliases)),
            )
        claim = self.claim(aliases[-1])
        self.record(aliases[-1], claim=claim)
        with self.database.write() as connection:
            connection.executemany(
                "INSERT INTO instagram_username_aliases(account_id,username_norm,first_seen_at,last_seen_at) "
                "VALUES(?,?,?,?)", ((claim["claim_id"], name, STAMP, STAMP) for name in aliases[:-1]),
            )
        saved_before = self.rows("task_results")
        stats = self.repair(aliases[-1])
        self.assertEqual({"total": 1100, "pending": 0, "recorded": 1, "deduped": 1099}, stats)
        recorded = [row for row in self.rows("task_mode_candidates") if row["state"] == "recorded"]
        self.assertEqual([aliases[0]], [row["username_norm"] for row in recorded])
        self.assertEqual(saved_before, self.rows("task_results"))
        self.assertEqual(1, len(self.rows("workbench_candidates")))
        self.assertEqual(stats, self.repair(aliases[-1]))

    def test_review_decisions_and_saved_evidence_are_immutable_during_scoped_repair(self):
        for decision in ("approved", "rejected"):
            name = f"reviewed.{decision}"
            self.append(name)
            claim = self.claim(name)
            self.record(name, claim=claim)
            candidate = self.service.create_workbench_candidate(
                "owner", claim_id=claim["claim_id"], username=name,
                visibility="private", profile={"marker": "review evidence"}, screening={},
                review_cache={}, source_mode="followers", source_target="target",
            )
            self.service.decide_workbench_candidate("owner", candidate_id=candidate["id"], decision=decision)
        tables = ("task_results", "workbench_candidates", "workbench_review_decisions", "global_seen")
        before = {table: self.rows(table) for table in tables}
        stats = self.repair("reviewed.approved", "reviewed.rejected")
        self.assertEqual(2, stats["recorded"])
        self.assertEqual(0, stats["pending"])
        self.assertEqual(before, {table: self.rows(table) for table in tables})

    def test_saved_identities_skip_for_all_owners_while_unread_owned_claim_can_resume(self):
        seed_task(self.database, task="canonical", target="canonical-source")
        seed_task(self.database, task="foreign", target="foreign-source", owner="foreign-owner")
        self.append("same.legacy", "foreign.legacy", "unread.owned")
        self.record("same.legacy", task="canonical", target="canonical-source")
        self.record("foreign.legacy", task="foreign", target="foreign-source", owner="foreign-owner")
        self.claim("unread.owned")
        evidence = self.rows("task_results")
        stats = self.repair("same.legacy", "foreign.legacy", "unread.owned")
        self.assertEqual({"total": 3, "pending": 1, "recorded": 0, "deduped": 2}, stats)
        states = {row["username_norm"]: row["state"] for row in self.rows("task_mode_candidates")}
        self.assertEqual({"same.legacy": "deduped", "foreign.legacy": "deduped", "unread.owned": "pending"}, states)
        self.assertEqual(evidence, self.rows("task_results"))

    def test_scoped_repair_keeps_owner_validation_batch_limit_and_empty_semantics(self):
        self.append("saved.gap")
        claim = self.claim("saved.gap")
        self.record("saved.gap", claim=claim)
        self.assertEqual(1, self.repair()["pending"])
        with self.assertRaises(NotFoundError):
            self.service.reconcile_task_mode_candidates("wrong-owner", "task", "target", "followers", usernames=[])
        with self.assertRaises(ValidationError):
            self.repair(*(f"candidate{index}" for index in range(101)))
        self.assertEqual(1, self.repair("saved.gap")["recorded"])


if __name__ == "__main__":
    unittest.main()
