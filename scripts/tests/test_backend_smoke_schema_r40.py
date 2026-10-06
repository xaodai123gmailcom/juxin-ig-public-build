"""Release-schema checks against the real database, independent of HTTP imports."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "backend_smoke_schema_r40", PROJECT_ROOT / "scripts" / "verify_backend_smoke.py"
)
assert SPEC is not None and SPEC.loader is not None
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)
from app.database import Database  # noqa: E402


class BackendSmokeSchemaR40Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="聚鑫 schema (40) ")
        self.addCleanup(temporary.cleanup)
        self.database = Database(Path(temporary.name) / "collector.sqlite3")
        self.database.initialize()

    def verify(self):
        with self.database.write() as connection:
            return SMOKE._verify_sqlite_schema(connection)

    def test_real_current_schema_passes_and_probe_leaves_no_rows(self):
        self.assertEqual(set(range(1, 42)), self.verify())
        with self.database.read() as connection:
            for table in ("app_users", "event_log", "event_log_usage", "tasks", "task_targets",
                          "split_admission_totals", "split_completed_targets"):
                self.assertEqual(0, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_missing_migration_31_fails(self):
        with self.database.write() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version=31")
        with self.assertRaisesRegex(RuntimeError, "Unexpected SQLite migration set"):
            self.verify()

    def test_missing_migration_32_fails(self):
        with self.database.write() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version=32")
        with self.assertRaisesRegex(RuntimeError, "Unexpected SQLite migration set"):
            self.verify()

    def test_missing_either_r55_migration_fails(self):
        for version in (33, 34, 35, 36):
            with self.subTest(version=version), self.database.write() as connection:
                connection.execute("SAVEPOINT absent_r55_migration")
                try:
                    connection.execute("DELETE FROM schema_migrations WHERE version=?", (version,))
                    with self.assertRaisesRegex(RuntimeError, "Unexpected SQLite migration set"):
                        SMOKE._verify_sqlite_schema(connection)
                finally:
                    connection.execute("ROLLBACK TO absent_r55_migration")
                    connection.execute("RELEASE absent_r55_migration")

    def test_old_1_through_30_schema_never_passes(self):
        with self.database.write() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version>30")
            versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        self.assertEqual(set(range(1, 31)), versions)
        with self.assertRaisesRegex(RuntimeError, "Unexpected SQLite migration set"):
            self.verify()

    def test_old_1_through_32_schema_must_upgrade_before_passing(self):
        old_date = "2026-09-17T00:00:00+00:00"
        with self.database.write() as connection:
            connection.execute(
                "INSERT INTO app_users(id,username_norm,username_display,password_hash,created_at) "
                "VALUES('old-owner','old-owner','old owner','test-only',?)", (old_date,),
            )
            connection.execute(
                "INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) "
                "VALUES('old-task','old-owner','old task','completed','[\"followers\"]','{}',?,?)",
                (old_date, old_date),
            )
            connection.execute(
                "INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,"
                "current_window_id,created_at,updated_at) "
                "VALUES('old-target','old-task','old.source','old.source',1,'completed','old-window',?,?)",
                (old_date, old_date),
            )
            for trigger in self.split_triggers():
                connection.execute(f"DROP TRIGGER {trigger}")
            connection.execute("DROP TABLE split_admission_totals")
            connection.execute("DROP TABLE split_completed_targets")
            connection.execute("DELETE FROM schema_migrations WHERE version>32")
        with self.assertRaisesRegex(RuntimeError, "Unexpected SQLite migration set"):
            self.verify()
        self.database.initialize()
        self.assertEqual(set(range(1, 42)), self.verify())
        with self.database.read() as connection:
            admission = connection.execute(
                "SELECT successful_adds,history_complete,has_executed FROM split_admission_totals "
                "WHERE owner_user_id='old-owner' AND username_norm='old.source'",
            ).fetchone()
            self.assertEqual((1, 0, 1), tuple(admission))
            completion = connection.execute(
                "SELECT completed_at,source_window_id FROM split_completed_targets WHERE target_id='old-target'",
            ).fetchone()
            self.assertEqual((old_date, "old-window"), tuple(completion))
            for table in ("app_users", "tasks", "task_targets", "split_admission_totals", "split_completed_targets"):
                self.assertEqual(1, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_real_r55_columns_upgrade_to_completion_details(self):
        with self.database.write() as connection:
            connection.execute("ALTER TABLE task_targets DROP COLUMN source_profile_json")
            connection.execute("ALTER TABLE split_completed_targets DROP COLUMN completion_details_json")
            connection.execute("DELETE FROM schema_migrations WHERE version=35")
        with self.assertRaisesRegex(RuntimeError, "Unexpected SQLite migration set"):
            self.verify()
        self.database.initialize()
        self.assertEqual(set(range(1, 42)), self.verify())

    def test_real_schema35_upgrades_private_follow_records(self):
        with self.database.write() as connection:
            connection.execute("DROP TABLE private_follow_completions")
            connection.execute("DELETE FROM schema_migrations WHERE version=36")
        with self.assertRaisesRegex(RuntimeError, "Unexpected SQLite migration set"):
            self.verify()
        self.database.initialize()
        self.assertEqual(set(range(1, 42)), self.verify())

    def test_missing_hover_repair_migration_fails(self):
        with self.database.write() as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version=37")
        with self.assertRaisesRegex(RuntimeError, "Unexpected SQLite migration set"):
            self.verify()

    def test_missing_private_follow_immutability_triggers_fail(self):
        for trigger in ("trg_private_follow_completion_no_update", "trg_private_follow_completion_no_delete"):
            with self.subTest(trigger=trigger), self.database.write() as connection:
                connection.execute("SAVEPOINT missing_private_trigger")
                try:
                    connection.execute(f"DROP TRIGGER {trigger}")
                    with self.assertRaisesRegex(RuntimeError, "missing triggers"):
                        SMOKE._verify_sqlite_schema(connection)
                finally:
                    connection.execute("ROLLBACK TO missing_private_trigger")
                    connection.execute("RELEASE missing_private_trigger")

    @staticmethod
    def split_triggers():
        return (
            "trg_split_admission_executed_insert", "trg_split_admission_executed_update",
            "trg_split_completed_target_insert", "trg_split_completed_target_update",
        )

    def test_missing_r55_tables_fail_despite_current_migration_markers(self):
        for table in ("split_admission_totals", "split_completed_targets", "private_follow_completions", "hover_r64_repair_archive"):
            with self.subTest(table=table), self.database.write() as connection:
                connection.execute("SAVEPOINT absent_r55_table")
                try:
                    connection.execute(f"DROP TABLE {table}")
                    with self.assertRaisesRegex(RuntimeError, f"missing tables.*{table}"):
                        SMOKE._verify_sqlite_schema(connection)
                finally:
                    connection.execute("ROLLBACK TO absent_r55_table")
                    connection.execute("RELEASE absent_r55_table")

    def test_missing_required_r55_columns_fail(self):
        cases = {
            "private_follow_completions": ("owner_user_id", "username_norm", "username_display", "campaign_id", "target_id", "attempt_id", "completed_at", "source_window_id", "profile_json", "executor_json", "confirmation"),
            "split_admission_totals": ("owner_user_id", "username_norm", "successful_adds", "history_complete", "has_executed", "updated_at"),
            "split_completed_targets": ("target_id", "owner_user_id", "username_norm", "username_display", "source_task_id", "source_window_id", "completed_at", "completion_details_json"),
            "task_targets": ("source_profile_json",),
            "workbench_candidates": ("review_stage", "review_transferred_at"),
        }
        for table, columns in cases.items():
            for column in columns:
                with self.subTest(table=table, column=column), self.database.write() as connection:
                    connection.execute("SAVEPOINT absent_r55_column")
                    try:
                        connection.execute(f"ALTER TABLE {table} RENAME COLUMN {column} TO unavailable_column")
                        with self.assertRaisesRegex(RuntimeError, f"{table} is missing columns.*{column}"):
                            SMOKE._verify_sqlite_schema(connection)
                    finally:
                        connection.execute("ROLLBACK TO absent_r55_column")
                        connection.execute("RELEASE absent_r55_column")

    def test_each_missing_split_registry_trigger_fails(self):
        for trigger in self.split_triggers():
            with self.subTest(trigger=trigger), self.database.write() as connection:
                connection.execute("SAVEPOINT absent_r55_trigger")
                try:
                    connection.execute(f"DROP TRIGGER {trigger}")
                    with self.assertRaisesRegex(RuntimeError, f"missing triggers.*{trigger}"):
                        SMOKE._verify_sqlite_schema(connection)
                finally:
                    connection.execute("ROLLBACK TO absent_r55_trigger")
                    connection.execute("RELEASE absent_r55_trigger")

    def test_named_but_nonfunctional_split_registry_triggers_cannot_pass(self):
        for trigger in self.split_triggers():
            operation = "INSERT" if trigger.endswith("insert") else "UPDATE"
            with self.subTest(trigger=trigger), self.database.write() as connection:
                connection.execute("SAVEPOINT broken_r55_trigger")
                try:
                    connection.execute(f"DROP TRIGGER {trigger}")
                    connection.execute(
                        f"CREATE TRIGGER {trigger} AFTER {operation} ON task_targets BEGIN SELECT 1; END"
                    )
                    with self.assertRaisesRegex(RuntimeError, "SQLite split registry.*mismatch"):
                        SMOKE._verify_sqlite_schema(connection)
                    for table in ("app_users", "tasks", "task_targets", "split_admission_totals", "split_completed_targets"):
                        self.assertEqual(0, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                finally:
                    connection.execute("ROLLBACK TO broken_r55_trigger")
                    connection.execute("RELEASE broken_r55_trigger")

    def test_missing_event_projection_table_fails_despite_migration_marker(self):
        with self.database.write() as connection:
            connection.execute("DROP TABLE event_log_usage")
        with self.assertRaisesRegex(RuntimeError, "missing tables.*event_log_usage"):
            self.verify()

    def test_each_missing_event_projection_trigger_fails(self):
        for operation in ("insert", "update", "delete"):
            with self.subTest(operation=operation):
                with self.database.write() as connection:
                    trigger = f"trg_event_log_usage_{operation}"
                    original = connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
                    ).fetchone()[0]
                    connection.execute(f"DROP TRIGGER {trigger}")
                try:
                    with self.assertRaisesRegex(RuntimeError, f"missing triggers.*{trigger}"):
                        self.verify()
                finally:
                    with self.database.write() as connection:
                        connection.execute(original)

    def test_named_but_nonfunctional_triggers_cannot_pass(self):
        for operation in ("insert", "update", "delete"):
            with self.subTest(operation=operation):
                with self.database.write() as connection:
                    trigger = f"trg_event_log_usage_{operation}"
                    original = connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
                    ).fetchone()[0]
                    connection.execute(f"DROP TRIGGER {trigger}")
                    connection.execute(
                        f"CREATE TRIGGER {trigger} AFTER {operation.upper()} ON event_log "
                        "BEGIN SELECT 1; END"
                    )
                try:
                    with self.assertRaisesRegex(RuntimeError, f"event_log_usage {operation} projection mismatch"):
                        self.verify()
                    with self.database.read() as connection:
                        self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM app_users").fetchone()[0])
                finally:
                    with self.database.write() as connection:
                        connection.execute(f"DROP TRIGGER {trigger}")
                        connection.execute(original)

    def test_existing_required_schema_checks_remain_enforced(self):
        with self.database.write() as connection:
            connection.execute("DROP TABLE global_seen_stats")
        with self.assertRaisesRegex(RuntimeError, "missing tables.*global_seen_stats"):
            self.verify()

    def test_future_unexpected_migration_is_rejected(self):
        with self.database.write() as connection:
            connection.execute("INSERT INTO schema_migrations VALUES(42, datetime('now'))")
        with self.assertRaisesRegex(RuntimeError, "Unexpected SQLite migration set"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
