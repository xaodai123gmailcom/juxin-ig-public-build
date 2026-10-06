"""Bound action snapshot payload reads without changing priority or history."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Database
from app.service import CoreService


class SnapshotCampaignScaleR39Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tmp.name) / "campaign-snapshot.sqlite3")
        self.database.initialize()
        self.service = CoreService(self.database)
        self.owner = self.service.register_user(
            "campaign-r39", "campaign-snapshot-test-password")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self, name, *, size=8, updated_at="2026-09-01T00:00:00Z",
             campaign_status="running"):
        campaign = self.service.create_action_campaign(
            self.owner, operation="follow", execution_type="campaign",
            profile_id=f"window-{name}",
            targets=[f"{name}.account.{index}" for index in range(size)],
            message=None, interval_min_seconds=8, interval_max_seconds=15,
            limit_count=size)
        payload = json.dumps({"audit": "x" * 32768, "campaign": name})
        with self.database.write() as connection:
            connection.execute(
                "UPDATE action_campaigns SET status=?, updated_at=? WHERE id=?",
                (campaign_status, updated_at, campaign["id"]))
            for index, target in enumerate(campaign["targets"]):
                status = {size - 1: "running", size - 2: "unknown",
                          size - 3: "failed"}.get(index, "confirmed")
                connection.execute(
                    "UPDATE action_targets SET status=?, last_error=? WHERE id=?",
                    (status, "historic-diagnostic-" + "z" * 1024, target["id"]))
                connection.execute(
                    """INSERT INTO action_attempts(
                        id,campaign_id,target_id,attempt_number,status,
                        started_at,finished_at,details_json
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (f"attempt-{name}-{index:05d}", campaign["id"], target["id"],
                     1, status, f"2026-09-01T00:{index // 60:02d}:{index % 60:02d}Z",
                     None if status == "running" else "2026-09-01T01:00:00Z", payload))
        return campaign, payload

    def test_bounded_snapshot_loads_payload_only_for_selected_detail_rows(self):
        campaign, payload = self.seed("large", size=300)
        expected = self.service.list_action_campaigns(self.owner, limit=1, detail_limit=3)
        read_counts = {"attempts": 0, "targets": 0}
        read_bytes = {"attempts": 0, "targets": 0}
        original_connect = self.database._connect

        def instrumented_connect():
            connection = original_connect()

            def probe(bucket, value):
                read_counts[bucket] += 1
                read_bytes[bucket] += len((value or "").encode("utf-8"))
                return value

            connection.create_function("r39_attempt_payload", 1,
                                       lambda value: probe("attempts", value))
            connection.create_function("r39_target_payload", 1,
                                       lambda value: probe("targets", value))
            # These connection-local views preserve the real tables and values.
            # The callbacks measure when SQLite evaluates large payload columns;
            # selecting full rows inside the old ranking CTE invokes all 300.
            connection.executescript("""
                CREATE TEMP VIEW action_attempts AS
                SELECT id,campaign_id,target_id,attempt_number,status,started_at,
                       finished_at,r39_attempt_payload(details_json) AS details_json
                FROM main.action_attempts;
                CREATE TEMP VIEW action_targets AS
                SELECT id,campaign_id,username_norm,username_display,source_target,
                       queue_order,status,control_after_attempt,
                       r39_target_payload(last_error) AS last_error,updated_at
                FROM main.action_targets;
            """)
            return connection

        with patch.object(self.database, "_connect", side_effect=instrumented_connect):
            actual = self.service.list_action_campaigns(self.owner, limit=1, detail_limit=3)
        self.assertEqual(expected, actual)
        self.assertEqual({"attempts": 3, "targets": 3}, read_counts)
        self.assertEqual(3 * len(payload.encode("utf-8")), read_bytes["attempts"])
        row = actual[0]
        self.assertTrue(row["targets_truncated"])
        self.assertTrue(row["attempts_truncated"])
        self.assertEqual({"running", "unknown", "failed"},
                         {item["status"] for item in row["targets"]})
        self.assertEqual({"running", "unknown", "failed"},
                         {item["status"] for item in row["attempts"]})
        with self.database.read() as connection:
            self.assertEqual(300, connection.execute(
                "SELECT COUNT(*) FROM action_attempts WHERE campaign_id=?",
                (campaign["id"],)).fetchone()[0])

    def test_shared_budget_keeps_each_campaign_and_active_rows_ahead_of_history(self):
        first, _ = self.seed("first", updated_at="2026-09-01T00:00:00Z")
        second, _ = self.seed("second", updated_at="2026-09-02T00:00:00Z")
        terminal, _ = self.seed("terminal", campaign_status="completed",
                                updated_at="2026-09-03T00:00:00Z")
        rows = self.service.list_action_campaigns(self.owner, limit=3, detail_limit=4)
        self.assertEqual([second["id"], first["id"], terminal["id"]],
                         [row["id"] for row in rows])
        for row in rows:
            self.assertEqual(["running"], [item["status"] for item in row["targets"]])
            self.assertEqual(["running"], [item["status"] for item in row["attempts"]])
            self.assertTrue(row["targets_truncated"])
            self.assertTrue(row["attempts_truncated"])
        page = self.service.list_action_campaigns(self.owner, limit=1, offset=2, detail_limit=1)
        self.assertEqual([terminal["id"]], [row["id"] for row in page])

    def test_unbounded_call_preserves_all_historical_details(self):
        campaign, payload = self.seed("history")
        rows = self.service.list_action_campaigns(self.owner)
        self.assertEqual(1, len(rows))
        self.assertEqual(campaign["id"], rows[0]["id"])
        self.assertEqual(8, len(rows[0]["targets"]))
        self.assertEqual(8, len(rows[0]["attempts"]))
        self.assertFalse(rows[0]["targets_truncated"])
        self.assertFalse(rows[0]["attempts_truncated"])
        self.assertEqual(json.loads(payload), rows[0]["attempts"][0]["details"])


if __name__ == "__main__":
    unittest.main()
