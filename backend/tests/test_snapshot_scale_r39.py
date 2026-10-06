"""Real SQLite regressions for large-ledger workbench snapshots (no HTTP mocks)."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.database import Database
from app.service import CoreService

NOW = "2026-09-18T12:00:00+00:00"


class SnapshotScaleR39Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "snapshot.sqlite3")
        self.db.initialize()
        self.service = CoreService(self.db)
        self.owner = self.service.register_user("snapshot_owner", "snapshot-regression-password")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def assert_event_totals(self):
        with self.db.read() as c:
            owners = [r[0] for r in c.execute("SELECT id FROM app_users")]
            for owner in owners:
                actual = c.execute("""SELECT COUNT(*), COALESCE(SUM(
                    length(CAST(payload_json AS BLOB)) + length(entity_type)
                    + length(entity_id) + length(event_type) + length(created_at)),0)
                    FROM event_log WHERE owner_user_id=?""", (owner,)).fetchone()
                projection = c.execute("SELECT entries,bytes FROM event_log_usage WHERE owner_user_id=?", (owner,)).fetchone()
                self.assertEqual(tuple(actual), tuple(projection) if projection else (0, 0))

    def insert_event(self, owner, payload):
        with self.db.write() as c:
            return c.execute("""INSERT INTO event_log(owner_user_id,entity_type,
                entity_id,event_type,payload_json,created_at) VALUES(?,?,?,?,?,?)""",
                (owner, "任务", "用户😀", "progress", payload, NOW)).lastrowid

    def test_event_projection_tracks_unicode_update_owner_delete_and_rollback(self):
        other = self.service.register_user("second_owner", "snapshot-regression-password")["id"]
        event = self.insert_event(self.owner, json.dumps({"中文": "😀"}, ensure_ascii=False))
        self.assert_event_totals()
        with self.db.write() as c:
            c.execute("UPDATE event_log SET payload_json=?, entity_type=? WHERE seq=?", ('{"内容":"新数据"}', "任务更新", event))
        self.assert_event_totals()
        with self.db.write() as c:
            c.execute("UPDATE event_log SET owner_user_id=? WHERE seq=?", (other, event))
        self.assert_event_totals()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.db.write() as c:
                c.execute("UPDATE event_log SET payload_json='{}' WHERE seq=?", (event,))
                raise RuntimeError("rollback")
        self.assert_event_totals()
        with self.db.write() as c:
            c.execute("DELETE FROM event_log WHERE seq=?", (event,))
        self.assert_event_totals()
        self.insert_event(other, '{"event":"owner cascade"}')
        with self.db.write() as c:
            c.execute("DELETE FROM app_users WHERE id=?", (other,))
        self.assert_event_totals()
        with self.db.read() as c:
            self.assertEqual(0, c.execute("SELECT COUNT(*) FROM event_log_usage WHERE owner_user_id=?", (other,)).fetchone()[0])

    def test_legacy_upgrade_backfills_once_and_reopening_preserves_exact_totals(self):
        self.insert_event(self.owner, '{"legacy":"永久业务记录之外的日志"}')
        with self.db.write() as c:
            for name in ("insert", "update", "delete"):
                c.execute(f"DROP TRIGGER trg_event_log_usage_{name}")
            c.execute("DROP TABLE event_log_usage")
            c.execute("DELETE FROM schema_migrations WHERE version=32")
        self.db.initialize()
        self.assert_event_totals()
        self.insert_event(self.owner, '{"new":"after upgrade"}')
        self.db.initialize()
        self.assert_event_totals()
        with self.db.read() as c:
            self.assertEqual(1, c.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=32").fetchone()[0])
            self.assertEqual(2, c.execute("SELECT COUNT(*) FROM event_log WHERE owner_user_id=?", (self.owner,)).fetchone()[0])

    def test_snapshot_never_reads_retained_event_payloads(self):
        with self.db.write() as c:
            c.executemany("""INSERT INTO event_log(owner_user_id,entity_type,entity_id,
                event_type,payload_json,created_at) VALUES(?,?,?,?,?,?)""",
                ((self.owner, "task", str(i), "progress", json.dumps({"trace": "x" * 4096}), NOW) for i in range(4000)))
        connect = self.db._connect
        reads = []
        def guarded_connect():
            c = connect()
            def authorizer(action, table, column, *_):
                if action == sqlite3.SQLITE_READ:
                    reads.append((table, column))
                    if table == "event_log" and column == "payload_json":
                        return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            c.set_authorizer(authorizer)
            return c
        with patch.object(self.db, "_connect", guarded_connect):
            snapshot = self.service.get_workbench_snapshot(self.owner, limit=10, history_limit=10)
        self.assertEqual(4000, snapshot["storage"]["event_log_entries"])
        self.assertGreater(snapshot["storage"]["event_log_bytes"], 16_000_000)
        self.assertIn(("event_log_usage", "bytes"), reads)

    def test_approved_snapshot_uses_identity_point_lookups_and_exact_counts(self):
        count, succeeded = 6000, 1000
        with self.db.write() as c:
            c.executemany("INSERT INTO instagram_accounts VALUES(?,NULL,?,?,?,?)",
                ((f"a{i:05}", f"u{i:05}", f"u{i:05}", NOW, NOW) for i in range(count)))
            c.executemany("INSERT INTO instagram_username_aliases VALUES(?,?,?,?)",
                ((f"a{i:05}", f"u{i:05}", NOW, NOW) for i in range(count)))
            c.executemany("""INSERT INTO workbench_candidates(id,owner_user_id,account_id,
                visibility,status,created_at,updated_at,reviewed_at) VALUES(?,?,?,'private','approved',?,?,?)""",
                ((f"c{i:05}", self.owner, f"a{i:05}", NOW, NOW, NOW) for i in range(count)))
            # Successes are immutable and may outlive bounded campaign details.
            c.executemany("INSERT INTO action_success_ledger VALUES(?,'follow',?,?,?,?,?,?)",
                ((self.owner, f"u{i:05}", f"u{i:05}", "old_campaign", f"target{i}", f"attempt{i}", NOW) for i in range(succeeded)))
            # A renamed historical success must still hide the current identity.
            c.execute("INSERT INTO instagram_username_aliases VALUES('a01000','legacy_name',?,?)", (NOW, NOW))
            c.execute("INSERT INTO action_success_ledger VALUES(?,'follow','legacy_name','legacy_name','old_campaign','t','a',?)", (self.owner, NOW))
            c.execute("INSERT INTO workbench_candidate_dismissals VALUES('dismiss','c01001',?,?)", (self.owner, NOW))
        connect = self.db._connect
        steps = [0]
        statements = []
        def counted_connect():
            c = connect()
            def progress():
                steps[0] += 1000
                return 1 if steps[0] > 1_000_000 else 0
            c.set_progress_handler(progress, 1000)
            c.set_trace_callback(statements.append)
            return c
        with patch.object(self.db, "_connect", counted_connect):
            snapshot = self.service.get_workbench_snapshot(self.owner, limit=20, history_limit=10)
        self.assertEqual(count - succeeded - 2, snapshot["counts"]["approved_private"])
        self.assertEqual(20, len(snapshot["approved_private_accounts"]))
        self.assertTrue(snapshot["has_more"]["approved_private_accounts"])
        self.assertTrue(all(row["id"] >= "c01002" for row in snapshot["approved_private_accounts"]))
        # Eligibility is maintained transactionally. Polling seeks the bounded
        # actionable page and small exact counters without re-reading aliases or
        # the lifetime success ledger to establish queue membership.
        approved_queries = [sql for sql in statements if "FROM workbench_actionable_candidates eligible" in sql]
        self.assertEqual(2, len(approved_queries))
        self.assertFalse(any("FROM instagram_username_aliases alias" in sql for sql in statements))
        with self.db.read() as c:
            for sql in approved_queries:
                plans = [row[3] for row in c.execute("EXPLAIN QUERY PLAN " + sql)]
                self.assertTrue(any("idx_workbench_actionable_page" in p and "SEARCH" in p for p in plans), plans)
                self.assertFalse(any("TEMP B-TREE" in p or "SCAN" in p for p in plans), plans)
        self.assertLess(steps[0], 100_000)

    def test_classification_count_is_covering_and_review_page_has_no_sort(self):
        with self.db.read() as c:
            count_plan = [r[3] for r in c.execute("""EXPLAIN QUERY PLAN
                SELECT COUNT(*), SUM(result.visibility='public'), SUM(result.visibility='private')
                FROM task_results result JOIN tasks task ON task.id=result.task_id
                WHERE task.owner_user_id=?""", (self.owner,))]
            self.assertTrue(any("COVERING INDEX idx_results_task_visibility" in line for line in count_plan), count_plan)
            page_plan = [r[3] for r in c.execute("""EXPLAIN QUERY PLAN
                SELECT * FROM workbench_candidates WHERE owner_user_id=? AND status='approved'
                AND visibility='private' ORDER BY reviewed_at DESC,id DESC LIMIT 20""", (self.owner,))]
            self.assertFalse(any("TEMP B-TREE" in line for line in page_plan), page_plan)


if __name__ == "__main__":
    unittest.main()
