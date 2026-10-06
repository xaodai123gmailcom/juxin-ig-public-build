r"""Read-only diagnosis of legacy Instagram profiles saved before they were read.

Run from a Windows release directory with Python 3.10+:
    python scripts\diagnose_unread_profiles.py
    python scripts\diagnose_unread_profiles.py --database "%APPDATA%\聚鑫国际\data\collector.sqlite3" --owner LOGIN

This script never initializes the application, edits SQLite, or prints profile,
screening, review, credential, or message payloads. The 'eligible_unfinished'
classification is a review of prerequisites, not an automatic database repair.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys


REQUIRED = {
    "app_users": {"id", "username_norm", "username_display"},
    "tasks": {"id", "owner_user_id", "status"},
    "task_targets": {"id", "task_id", "status", "current_stage"},
    "task_checkpoints": {"target_id", "mode", "stage"},
    "task_mode_candidates": {"target_id", "mode", "username_norm", "state"},
    "instagram_accounts": {"id", "current_username_norm"},
    "instagram_username_aliases": {"account_id", "username_norm"},
    "task_results": {"id", "task_id", "target_id", "account_id", "profile_json", "screening_json", "sources_json", "created_at"},
    "global_seen": {"account_id"},
    "global_identity_owners": {"account_id", "owner_user_id"},
    "workbench_identity_claims": {"account_id", "claimed_by_user_id", "source", "source_target"},
    "workbench_candidates": {"id", "account_id", "owner_user_id", "status", "review_stage", "review_transferred_at", "profile_json"},
    "workbench_review_decisions": {"candidate_id"},
    "workbench_candidate_dismissals": {"candidate_id"},
    "workbench_collection_exclusions": {"account_id"},
    "task_result_duplicate_archive": {"account_id"},
    "browser_operation_leases": {"entity_id", "operation_type"},
}
LEGACY_STATUS = "skipped_after_final_confirmation"
LEGACY_REASON = "instagram_content_not_visible"
RESTARTABLE_TASK_STATES = {"draft", "queued", "paused", "stopped", "failed", "recoverable"}
RESTARTABLE_TARGET_STATES = {"pending", "recoverable", "failed", "stopped"}
SAFETY = {
    "eligible_unfinished": ("A", "A controlled repair may retry this unfinished source after a fresh transaction check."),
    "active_execution_deferred": ("B", "Wait for the current execution to stop before reviewing this row."),
    "manual_decision_preserved": ("C", "A reviewed decision or terminal review card is preserved."),
    "review_pending_preserved": ("C", "An existing pending review card is preserved."),
    "completed_or_pruned": ("C", "A closed source or missing spool needs a separate recheck path."),
    "other_terminal_history": ("C", "Other durable history needs individual review."),
    "claim_or_spool_mismatch": ("C", "Claim or spool evidence does not support a safe retry."),
    "ambiguous_evidence": ("C", "The legacy markers do not consistently identify this old failure."),
}


def database_candidates(environ: dict[str, str] | None = None) -> list[Path]:
    """Find known installations without creating a database or directory."""
    env = os.environ if environ is None else environ
    paths = []
    if env.get("IGAC_DB_PATH"):
        paths.append(Path(env["IGAC_DB_PATH"]).expanduser())
    if env.get("IGAC_DATA_DIR"):
        paths.append(Path(env["IGAC_DATA_DIR"]).expanduser() / "collector.sqlite3")
    if env.get("APPDATA"):
        base = Path(env["APPDATA"])
        paths.extend(base / name / "data" / "collector.sqlite3" for name in (
            "聚鑫国际", "聚鑫国际 新一代 IG 采集器", "juxin-ig-audience-collector-newgen",
        ))
    if env.get("LOCALAPPDATA"):
        paths.append(Path(env["LOCALAPPDATA"]) / "JuxinIGCollector" / "collector.sqlite3")
    if env.get("XDG_DATA_HOME"):
        paths.append(Path(env["XDG_DATA_HOME"]) / "juxin-ig-collector" / "collector.sqlite3")
    return list({os.path.normcase(str(path.resolve())): path.resolve() for path in paths}.values())


def _has_schema(connection: sqlite3.Connection) -> list[str]:
    missing = []
    for table, expected in REQUIRED.items():
        actual = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        if not expected <= actual:
            missing.append(table)
    return missing


def _count(connection: sqlite3.Connection, sql: str, *params: object) -> int:
    return int(connection.execute(sql, params).fetchone()[0])


def _classify(connection: sqlite3.Connection, result: sqlite3.Row) -> tuple[str, str | None]:
    """Prefer holding an ambiguous row over revoking a real decision or exclusion."""
    account = result["account_id"]
    candidate = connection.execute(
        "SELECT * FROM workbench_candidates WHERE account_id=?", (account,)
    ).fetchone()
    if candidate and (candidate["status"] != "pending" or _count(
        connection, "SELECT COUNT(*) FROM workbench_review_decisions WHERE candidate_id=?", candidate["id"]
    )):
        return "manual_decision_preserved", None
    if candidate:
        # A pending review card may already be open in another window. Preserve
        # its identity and snapshot; an automatic repair must not withdraw it.
        return "review_pending_preserved", None
    if result["page_reason"] != LEGACY_REASON or result["screen_status"] != "partial" or result["screen_reason"] != LEGACY_REASON:
        return "ambiguous_evidence", None
    if result["task_status"] == "completed" or result["target_status"] == "completed" or result["current_stage"] in {"completed_archived", "deleted_archived"}:
        return "completed_or_pruned", None
    if result["task_status"] not in RESTARTABLE_TASK_STATES or result["target_status"] not in RESTARTABLE_TARGET_STATES or _count(
        connection,
        "SELECT COUNT(*) FROM browser_operation_leases WHERE operation_type='collection' AND entity_id=?",
        result["task_id"],
    ):
        return "active_execution_deferred", None
    if (_count(connection, "SELECT COUNT(*) FROM workbench_collection_exclusions WHERE account_id=?", account)
        or _count(connection, "SELECT COUNT(*) FROM task_result_duplicate_archive WHERE account_id=?", account)
        or _count(connection, "SELECT COUNT(*) FROM task_results WHERE account_id=? AND id<>?", account, result["id"])
        or _count(connection, "SELECT COUNT(*) FROM global_identity_owners WHERE account_id=? AND owner_user_id<>?", account, result["owner_user_id"])):
        return "other_terminal_history", None
    claim = connection.execute("SELECT * FROM workbench_identity_claims WHERE account_id=?", (account,)).fetchone()
    if (claim is None or claim["claimed_by_user_id"] != result["owner_user_id"]
        or claim["source_target"] != result["target_id"]
        or claim["source"] not in {"followers", "following", "post_likers"}
        or not _count(connection, "SELECT COUNT(*) FROM global_seen WHERE account_id=?", account)):
        return "claim_or_spool_mismatch", None
    try:
        sources = json.loads(result["sources_json"])
    except (TypeError, ValueError):
        return "ambiguous_evidence", None
    if not isinstance(sources, list) or claim["source"] not in sources:
        return "claim_or_spool_mismatch", None
    checkpoint = connection.execute(
        "SELECT stage FROM task_checkpoints WHERE target_id=? AND mode=?",
        (result["target_id"], claim["source"]),
    ).fetchone()
    if checkpoint and checkpoint["stage"] == "mode_completed":
        return "completed_or_pruned", None
    spool = connection.execute(
        """SELECT candidate.username_norm, candidate.state
           FROM task_mode_candidates candidate
           JOIN instagram_username_aliases alias ON alias.username_norm=candidate.username_norm
           WHERE candidate.target_id=? AND candidate.mode=? AND alias.account_id=?""",
        (result["target_id"], claim["source"], account),
    ).fetchall()
    if not spool:
        return "completed_or_pruned", None
    if len(spool) != 1 or spool[0]["state"] != "recorded":
        return "claim_or_spool_mismatch", None
    return "eligible_unfinished", spool[0]["username_norm"]


def inspect_database(path: Path, owner: str | None = None, *, limit: int = 50) -> dict:
    """Use a consistent read transaction, including committed WAL content."""
    report: dict = {"database": str(path.resolve()), "read_only": True}
    if not path.is_file():
        return {**report, "status": "not_found"}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN")
            missing = _has_schema(connection)
            if missing:
                return {**report, "status": "unsupported_schema", "missing_tables_or_columns": missing}
            users = [dict(row) for row in connection.execute(
                "SELECT id,username_norm,username_display FROM app_users ORDER BY username_norm"
            )]
            if owner is None and len(users) != 1:
                return {**report, "status": "owner_selection_required", "owners": users}
            if owner is None:
                matches = users
            else:
                matches = [user for user in users if user["id"] == owner]
                if not matches:
                    matches = [user for user in users if user["username_norm"] == owner]
                if not matches:
                    matches = [user for user in users if user["username_display"] == owner]
            if len(matches) != 1:
                return {**report, "status": "owner_not_found" if not matches else "owner_selection_required", "owners": users}
            selected = matches[0]
            # Both JSON markers came from the same old worker branch. Query the
            # profile marker first to report inconsistent/edited evidence too.
            rows = connection.execute(
                """SELECT result.id,result.task_id,result.target_id,result.account_id,
                          result.sources_json,task.owner_user_id,task.status AS task_status,
                          target.status AS target_status,target.current_stage,
                          account.current_username_norm AS username,
                          CASE WHEN json_valid(result.profile_json) THEN json_extract(result.profile_json,'$.page_read_reason') END AS page_reason,
                          CASE WHEN json_valid(result.screening_json) THEN json_extract(result.screening_json,'$.page_read.status') END AS screen_status,
                          CASE WHEN json_valid(result.screening_json) THEN json_extract(result.screening_json,'$.page_read.reason') END AS screen_reason
                   FROM task_results result
                   JOIN tasks task ON task.id=result.task_id
                   JOIN task_targets target ON target.id=result.target_id
                   JOIN instagram_accounts account ON account.id=result.account_id
                   WHERE task.owner_user_id=?
                     AND CASE WHEN json_valid(result.profile_json)
                       THEN json_extract(result.profile_json,'$.page_read_status') END=?
                   ORDER BY result.created_at,result.id""",
                (selected["id"], LEGACY_STATUS),
            )
            counts: Counter[str] = Counter()
            details = []
            for row in rows:
                classification, spool_username = _classify(connection, row)
                counts[classification] += 1
                if len(details) < limit:
                    details.append({
                        "result_id": row["id"], "task_id": row["task_id"],
                        "target_id": row["target_id"], "account_id": row["account_id"],
                        "username": row["username"], "task_status": row["task_status"],
                        "target_status": row["target_status"], "classification": classification,
                        "safety_level": SAFETY[classification][0],
                        "spool_username": spool_username,
                    })
            return {**report, "status": "inspected", "owner": selected,
                    "total": sum(counts.values()), "counts": dict(sorted(counts.items())),
                    "safety_levels": {key: {"level": SAFETY[key][0], "meaning": SAFETY[key][1]}
                                      for key in sorted(counts)},
                    "shown": len(details), "truncated": sum(counts.values()) > len(details),
                    "rows": details}
    except (OSError, sqlite3.Error):
        return {**report, "status": "read_failed", "message": "Could not read this database; no changes were made."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="Exact SQLite database file")
    parser.add_argument("--owner", help="Login name or owner ID; the sole login is selected automatically")
    parser.add_argument("--limit", type=int, default=50, help="Maximum identity keys shown (default: 50)")
    parser.add_argument("--json", action="store_true", help="Print a JSON report to stdout")
    args = parser.parse_args(argv)
    if not 0 <= args.limit <= 5000:
        parser.error("--limit must be between 0 and 5000")
    candidates = [args.database] if args.database else [p for p in database_candidates() if p.is_file()]
    if len(candidates) != 1:
        if not candidates:
            print("No database found. Pass --database with the exact collector.sqlite3 path.", file=sys.stderr)
        else:
            print("Multiple databases found. Choose one with --database:", file=sys.stderr)
            for candidate in candidates:
                print(candidate, file=sys.stderr)
        return 2
    report = inspect_database(candidates[0], args.owner, limit=args.limit)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Database: {report['database']}")
        print(f"Status: {report['status']} (read only)")
        if "owners" in report:
            print("Select a login with --owner:")
            for user in report["owners"]:
                print(f"  {user['username_display']} [{user['id']}]")
        if report["status"] == "inspected":
            print(f"Login: {report['owner']['username_display']}")
            print(f"Legacy unread results: {report['total']}")
            for classification, count in report["counts"].items():
                safety = report["safety_levels"][classification]
                print(f"  Level {safety['level']} — {classification}: {count}. {safety['meaning']}")
            for row in report["rows"]:
                print(f"  @{row['username']} — {row['classification']} ({row['result_id']})")
            if report["truncated"]:
                print(f"Showing {report['shown']} of {report['total']}; use --limit for more.")
            print("eligible_unfinished needs an application repair transaction; this tool does not recheck or alter records.")
        elif report["status"] not in {"owner_selection_required", "owner_not_found"}:
            print(report.get("message", "Use a current app database to run this diagnosis."))
    return 0 if report["status"] == "inspected" else 2


if __name__ == "__main__":
    raise SystemExit(main())
