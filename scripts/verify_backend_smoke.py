"""Deterministic backend release smoke check used by the Windows builder.

The complete asynchronous/endurance suite remains a CI and release-gate job.
End-user packaging only needs a fast, timing-independent check that imports the
packaged backend, initializes the current SQLite schema, exercises both cheap
liveness and explicit integrity diagnostics, and verifies version consistency.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _project_versions() -> tuple[str, str]:
    package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    pyproject = tomllib.loads(
        (BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return str(package["version"]), str(pyproject["project"]["version"])


def _verify_person_model_assets() -> int:
    asset_dir = BACKEND_ROOT / "app" / "assets" / "person_classifier"
    manifest = json.loads(
        (asset_dir / "MODEL_SOURCES.json").read_text(encoding="utf-8")
    )
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != 8:
        raise RuntimeError("Local person-model manifest must pin exactly eight files")
    for item in files:
        filename = str(item["filename"])
        path = asset_dir / filename
        payload = path.read_bytes()
        if len(payload) != int(item["size"]):
            raise RuntimeError(f"Local person-model size mismatch: {filename}")
        if hashlib.sha384(payload).hexdigest() != str(item["sha384"]):
            raise RuntimeError(f"Local person-model SHA-384 mismatch: {filename}")
    return len(files)


def _verify_sqlite_schema(connection: sqlite3.Connection) -> set[int]:
    """Check the release schema without importing optional HTTP/native packages.

    This is also called by the complete release smoke, whose runtime imports and
    asset checks remain mandatory.  Requiring the exact migration set prevents
    an old, internally consistent schema from passing the current release gate.
    """
    migration_versions = {
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    }
    if migration_versions != set(range(1, 42)):
        raise RuntimeError(
            f"Unexpected SQLite migration set: {sorted(migration_versions)}"
        )
    required_tables = {
        "retired_account_profiles", "workbench_aggregate_state", "workbench_aggregate_members",
        "workbench_snapshot_counts", "workbench_actionable_candidates",
        "workbench_progress_contributions", "workbench_progress_totals",
        "native_browser_profiles", "cloud_workspace_links",
        "account_window_plans", "account_window_events", "studio_jobs",
        "studio_assets", "studio_templates", "studio_daily_actions", "tasks",
        "task_targets", "task_target_recovery_controls", "task_checkpoints",
        "task_mode_candidates", "task_mode_candidate_counters", "task_results",
        "split_candidates", "split_candidate_window_affinity",
        "split_candidate_history", "split_admission_totals", "split_completed_targets",
        "private_follow_completions", "action_campaigns", "action_targets",
        "action_attempts", "action_success_ledger", "action_dispatch_claims",
        "workbench_identity_claims", "workbench_candidates",
        "workbench_cache_usage", "workbench_candidate_dismissals",
        "workbench_review_decisions", "workbench_collection_exclusions",
        "hover_r64_repair_archive",
        "workbench_state_revision", "global_seen_stats", "event_log_usage",
    }
    actual_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing_tables = sorted(required_tables - actual_tables)
    if missing_tables:
        raise RuntimeError(f"SQLite schema is missing tables: {missing_tables}")
    required_triggers = {
        "trg_workbench_aggregate_member_insert", "trg_workbench_aggregate_member_delete",
        "trg_workbench_aggregate_actionable_insert", "trg_workbench_aggregate_actionable_delete",
        "trg_progress_contribution_insert", "trg_progress_contribution_delete", "trg_progress_refresh_identity",
        "trg_private_follow_completion_no_update", "trg_private_follow_completion_no_delete",
        "trg_event_log_usage_insert", "trg_event_log_usage_update",
        "trg_event_log_usage_delete",
        "trg_workbench_exclusion_no_delete",
        "trg_split_admission_executed_insert", "trg_split_admission_executed_update",
        "trg_split_completed_target_insert", "trg_split_completed_target_update",
    }
    actual_triggers = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    missing_triggers = sorted(required_triggers - actual_triggers)
    if missing_triggers:
        raise RuntimeError(f"SQLite schema is missing triggers: {missing_triggers}")
    task_target_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(task_targets)")
    }
    if "allowed_window_ids_json" not in task_target_columns:
        raise RuntimeError("SQLite task_targets is missing allowed_window_ids_json")
    required_columns = {
        "posting_account_snapshots": {"followers_count", "following_count", "posts_count", "owner_user_id", "profile_id"},
        "private_follow_completions": {"owner_user_id", "username_norm", "username_display", "campaign_id", "target_id", "attempt_id", "completed_at", "source_window_id", "profile_json", "executor_json", "confirmation"},
        "task_targets": {"source_profile_json"},
        "workbench_candidates": {"review_stage", "review_transferred_at"},
        "hover_r64_repair_archive": {"account_id", "username_display", "exclusion_id", "repaired_at"},
        "split_admission_totals": {
            "owner_user_id", "username_norm", "successful_adds", "history_complete",
            "has_executed", "updated_at",
        },
        "split_completed_targets": {
            "target_id", "owner_user_id", "username_norm", "username_display",
            "source_task_id", "source_window_id", "completed_at", "completion_details_json",
        },
    }
    for table, expected_columns in required_columns.items():
        actual_columns = {
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        missing_columns = sorted(expected_columns - actual_columns)
        if missing_columns:
            raise RuntimeError(f"SQLite {table} is missing columns: {missing_columns}")

    _verify_event_log_projection(connection)
    _verify_split_registry(connection)
    return migration_versions


def _verify_event_log_projection(connection: sqlite3.Connection) -> None:
    """Exercise all three triggers; names alone must not satisfy this gate."""
    owner = f"backend-smoke-{uuid.uuid4().hex}"
    created_at = "2026-09-17T00:00:00+00:00"
    connection.execute("SAVEPOINT backend_smoke_event_projection")
    try:
        connection.execute(
            "INSERT INTO app_users(id, username_norm, username_display, password_hash, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (owner, owner, "release smoke", "not-a-login-credential", created_at),
        )
        event_id = connection.execute(
            "INSERT INTO event_log(owner_user_id, entity_type, entity_id, event_type, payload_json, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (owner, "task", "smoke", "created", '{"内容":"中文😀"}', created_at),
        ).lastrowid

        def verify_totals(stage: str) -> None:
            expected = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(CAST(payload_json AS BLOB)) "
                "+ length(entity_type) + length(entity_id) + length(event_type) "
                "+ length(created_at)), 0) FROM event_log WHERE owner_user_id=?",
                (owner,),
            ).fetchone()
            actual = connection.execute(
                "SELECT entries, bytes FROM event_log_usage WHERE owner_user_id=?",
                (owner,),
            ).fetchone()
            if actual is None or tuple(actual) != tuple(expected):
                raise RuntimeError(
                    f"SQLite event_log_usage {stage} projection mismatch: "
                    f"expected={tuple(expected)}, actual={tuple(actual) if actual is not None else None}"
                )

        verify_totals("insert")
        connection.execute(
            "UPDATE event_log SET payload_json=?, event_type=? WHERE seq=?",
            ('{"内容":"更新后的记录😀"}', "updated", event_id),
        )
        verify_totals("update")
        connection.execute("DELETE FROM event_log WHERE seq=?", (event_id,))
        verify_totals("delete")
    finally:
        # Nothing from this behavioral probe belongs in even the temporary DB.
        connection.execute("ROLLBACK TO SAVEPOINT backend_smoke_event_projection")
        connection.execute("RELEASE SAVEPOINT backend_smoke_event_projection")


def _verify_split_registry(connection: sqlite3.Connection) -> None:
    """Exercise r55 execution/completion triggers without leaving business data.

    Successful admission counts are written by the admission transaction; state
    triggers must preserve them and retain the FIRST completion date and window.
    Both INSERT and UPDATE paths matter when old workspaces are restored.
    """
    owner = f"backend-smoke-{uuid.uuid4().hex}"
    task_id = f"smoke-task-{uuid.uuid4().hex}"
    first = "2026-09-17T00:00:00+00:00"
    later = "2026-09-24T00:00:00+00:00"
    connection.execute("SAVEPOINT backend_smoke_split_registry")
    try:
        connection.execute(
            "INSERT INTO app_users(id,username_norm,username_display,password_hash,created_at) "
            "VALUES(?,?,?,?,?)", (owner, owner, "release smoke", "not-a-login-credential", first),
        )
        connection.execute(
            "INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) "
            "VALUES(?,?,?,'running','[\"followers\"]','{}',?,?)",
            (task_id, owner, "release smoke", first, first),
        )
        for queue_order, mode in enumerate(("update", "insert"), start=1):
            username = f"smoke.{mode}"
            target_id = f"smoke-target-{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO split_admission_totals(owner_user_id,username_norm,successful_adds,"
                "history_complete,has_executed,updated_at) VALUES(?,?,1,1,0,?)",
                (owner, username, first),
            )
            connection.execute(
                "INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,"
                "status,current_window_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (target_id, task_id, username, username, queue_order,
                 "pending" if mode == "update" else "completed", "original-window", first, first),
            )
            if mode == "update":
                pending = connection.execute(
                    "SELECT has_executed FROM split_admission_totals WHERE owner_user_id=? AND username_norm=?",
                    (owner, username),
                ).fetchone()
                if pending is None or pending[0] != 0:
                    raise RuntimeError("SQLite split registry pending target was falsely marked executed")
                connection.execute("UPDATE task_targets SET status='running' WHERE id=?", (target_id,))
            admission = connection.execute(
                "SELECT successful_adds,history_complete,has_executed FROM split_admission_totals "
                "WHERE owner_user_id=? AND username_norm=?", (owner, username),
            ).fetchone()
            if admission is None or tuple(admission) != (1, 1, 1):
                raise RuntimeError(f"SQLite split registry {mode} execution/count mismatch")
            if mode == "update":
                connection.execute("UPDATE task_targets SET status='completed' WHERE id=?", (target_id,))

            def verify_completion(stage: str) -> None:
                row = connection.execute(
                    "SELECT owner_user_id,username_norm,username_display,source_task_id,"
                    "source_window_id,completed_at FROM split_completed_targets WHERE target_id=?",
                    (target_id,),
                ).fetchone()
                if row is None or tuple(row) != (owner, username, username, task_id, "original-window", first):
                    raise RuntimeError(f"SQLite split registry {mode} {stage} completion mismatch")

            verify_completion("first")
            connection.execute(
                "UPDATE task_targets SET status='completed',current_window_id='late-window',updated_at=? WHERE id=?",
                (later, target_id),
            )
            connection.execute(
                "UPDATE task_targets SET current_window_id=NULL,preferred_window_id=NULL,"
                "current_stage='completed_archived',updated_at=? WHERE id=?", (later, target_id),
            )
            verify_completion("retained")
            if connection.execute(
                "SELECT successful_adds FROM split_admission_totals WHERE owner_user_id=? AND username_norm=?",
                (owner, username),
            ).fetchone()[0] != 1:
                raise RuntimeError(f"SQLite split registry {mode} replay incremented admissions")
    finally:
        connection.execute("ROLLBACK TO SAVEPOINT backend_smoke_split_registry")
        connection.execute("RELEASE SAVEPOINT backend_smoke_split_registry")


def main() -> None:
    # Import every Core layer PyInstaller must discover.  These imports are kept
    # explicit so a missing runtime dependency fails here with a useful traceback.
    from app import __version__ as backend_version
    from app.bitbrowser_api import BitBrowserClient
    from app.config import Settings
    from app.database import Database
    from app.execution_manager import ExecutionManager  # noqa: F401
    from app.main import create_app
    from app.parent_watchdog import ParentProcessWatchdog  # noqa: F401
    from app.person_recognition import LocalOpenVinoPersonClassifier  # noqa: F401
    from app.playwright_worker import PlaywrightWorker  # noqa: F401

    # The release runtime has one stable BitBrowser import boundary.  Loading the
    # retired connection module here would both make packaging depend on dead code
    # and let a smoke check accidentally validate a client production never uses.
    if BitBrowserClient.__module__ != "app.bitbrowser_v2":
        raise RuntimeError(
            "BitBrowser production entry point does not resolve to the V2 client: "
            f"{BitBrowserClient.__module__}.{BitBrowserClient.__name__}"
        )
    if "app.bitbrowser" in sys.modules:
        raise RuntimeError("Legacy app.bitbrowser was imported by the release smoke check")

    package_version, pyproject_version = _project_versions()
    versions = {package_version, pyproject_version, backend_version}
    if len(versions) != 1:
        raise RuntimeError(
            "Application version mismatch: "
            f"package={package_version}, pyproject={pyproject_version}, backend={backend_version}"
        )

    verified_model_count = _verify_person_model_assets()

    with tempfile.TemporaryDirectory(prefix="igac-backend-smoke-") as temporary:
        data_dir = Path(temporary)
        database = Database(data_dir / "collector.sqlite3")
        database.acquire_instance_lock()
        try:
            database.initialize()
            if database.liveness_check() != "ok":
                raise RuntimeError("SQLite liveness check failed")
            if database.integrity_check() != "ok":
                raise RuntimeError("SQLite integrity check failed")
            with database.write() as connection:
                migration_versions = _verify_sqlite_schema(connection)

            settings = Settings(
                startup_token="backend-smoke-startup-token-00000001",
                database_path=database.path,
                data_dir=data_dir,
                bitbrowser_url="http://127.0.0.1:54345",
                bind_host="127.0.0.1",
                bind_port=17831,
            )
            application = create_app(settings, database=database)
            if application.version != package_version:
                raise RuntimeError(
                    f"FastAPI metadata version mismatch: {application.version} != {package_version}"
                )
        finally:
            database.release_instance_lock()

    print(
        f"Backend smoke verified: v{package_version}, migrations {min(migration_versions)}-{max(migration_versions)}, "
        f"SQLite liveness/integrity, event-log and split-registry triggers, Core imports, {verified_model_count} "
        "pinned local person-model assets, and the V2-only BitBrowser import boundary."
    )


if __name__ == "__main__":
    main()
