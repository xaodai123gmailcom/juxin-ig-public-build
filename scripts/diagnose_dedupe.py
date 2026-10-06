"""Offline, read-only identity diagnosis. Never imports or initializes the app.

Only explicit database files and known Juxin locations are inspected. The report
contains identity keys and record states/times, never profiles, credentials,
screenshots, message text, or raw event/configuration JSON.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urlparse


DEFAULT_USERNAMES = ("sample_dedupe01", "sample_dedupe02")
ROOT = Path(__file__).resolve().parents[1]
TABLES = (
    "instagram_accounts", "instagram_username_aliases", "global_seen",
    "global_seen_stats", "task_results", "workbench_candidates",
    "workbench_identity_claims", "workbench_collection_exclusions",
    "workbench_review_decisions", "workbench_candidate_dismissals",
    "task_result_duplicate_archive", "global_identity_owners",
    "schema_migrations",
)
DETAIL_FIELDS = {
    "task_result_duplicate_archive": ("original_result_id", "task_id", "target_id", "account_id", "visibility", "qualified", "created_at", "updated_at", "archived_at"),
    "global_identity_owners": ("account_id", "first_seen_at", "last_seen_at"),
    "task_results": ("id", "task_id", "target_id", "account_id", "visibility", "qualified", "created_at", "updated_at"),
    "workbench_candidates": ("id", "account_id", "visibility", "status", "source_mode", "source_target", "created_at", "updated_at", "reviewed_at"),
    "workbench_identity_claims": ("account_id", "source", "source_target", "claimed_at"),
    "workbench_collection_exclusions": ("id", "account_id", "reason_code", "excluded_at"),
    "workbench_review_decisions": ("id", "candidate_id", "decision", "visibility", "decided_at"),
    "workbench_candidate_dismissals": ("id", "candidate_id", "dismissed_at"),
}
DETAIL_LIMIT = 100


def normalize_username(value: str) -> str:
    value = value.strip()
    if value.lower().startswith(("https://", "http://")):
        parsed = urlparse(value)
        if parsed.hostname not in {"instagram.com", "www.instagram.com"}:
            raise ValueError("Only Instagram profile URLs are accepted")
        value = parsed.path.strip("/").split("/")[0]
    value = value.lstrip("@").strip().lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", value):
        raise ValueError("Invalid Instagram username")
    return value


def database_candidates(explicit: list[str], environ=None) -> list[Path]:
    env = os.environ if environ is None else environ
    paths = [Path(value).expanduser() for value in explicit]
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
    unique = {}
    for path in paths:
        resolved = path.resolve()
        unique.setdefault(os.path.normcase(str(resolved)), resolved)
    return list(unique.values())


class Inspector:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        present = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.columns = {
            table: {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            for table in TABLES if table in present
        }

    def has(self, table: str, *columns: str) -> bool:
        return table in self.columns and set(columns).issubset(self.columns[table])

    def scalar(self, sql: str, args=()):
        return self.connection.execute(sql, args).fetchone()[0]

    def details(self, table: str, key: str, values: list[str], fields=None) -> dict:
        if not self.has(table, key):
            return {"available": False, "count": None, "rows": []}
        if not values:
            return {"available": True, "count": 0, "rows": [], "truncated": False}
        fields = [name for name in (fields or DETAIL_FIELDS[table]) if self.has(table, name)]
        placeholders = ",".join("?" for _ in values)
        where = f'"{key}" IN ({placeholders})'
        count = self.scalar(f'SELECT COUNT(*) FROM "{table}" WHERE {where}', values)
        selected = ",".join(f'"{name}"' for name in fields)
        rows = self.connection.execute(
            f'SELECT {selected} FROM "{table}" WHERE {where} ORDER BY "{key}" LIMIT ?',
            [*values, DETAIL_LIMIT],
        )
        return {"available": True, "count": count, "rows": [dict(row) for row in rows], "truncated": count > DETAIL_LIMIT}

    def metrics(self) -> dict:
        result = {"global_ledger_count": None, "displayed_ledger_count": None,
                  "business_identities_missing_ledger": None,
                  "accounts_missing_current_alias": None,
                  "duplicate_result_account_ids": None,
                  "duplicate_candidate_account_ids": None,
                  "duplicate_normalized_account_names": None}
        if self.has("global_seen", "account_id"):
            result["global_ledger_count"] = self.scalar("SELECT COUNT(*) FROM global_seen")
            sources = [table for table in ("task_results", "workbench_candidates", "workbench_collection_exclusions", "workbench_identity_claims") if self.has(table, "account_id")]
            if sources:
                business = " UNION ".join(f'SELECT account_id FROM "{table}"' for table in sources)
                result["business_identities_missing_ledger"] = self.scalar(
                    f"SELECT COUNT(*) FROM ({business}) business WHERE NOT EXISTS "
                    "(SELECT 1 FROM global_seen seen WHERE seen.account_id=business.account_id)"
                )
        if self.has("global_seen_stats", "singleton_id", "total_count"):
            row = self.connection.execute("SELECT total_count FROM global_seen_stats WHERE singleton_id=1").fetchone()
            result["displayed_ledger_count"] = row[0] if row else None
        if self.has("instagram_accounts", "id", "current_username_norm") and self.has("instagram_username_aliases", "account_id", "username_norm"):
            result["accounts_missing_current_alias"] = self.scalar(
                "SELECT COUNT(*) FROM instagram_accounts account WHERE NOT EXISTS "
                "(SELECT 1 FROM instagram_username_aliases alias WHERE alias.account_id=account.id "
                "AND alias.username_norm=account.current_username_norm)"
            )
        for table, key in (("task_results", "duplicate_result_account_ids"), ("workbench_candidates", "duplicate_candidate_account_ids")):
            if self.has(table, "account_id"):
                result[key] = self.scalar(f'SELECT COUNT(*) FROM (SELECT account_id FROM "{table}" GROUP BY account_id HAVING COUNT(*)>1)')
        if self.has("instagram_accounts", "current_username_norm"):
            result["duplicate_normalized_account_names"] = self.scalar(
                "SELECT COUNT(*) FROM (SELECT lower(ltrim(trim(current_username_norm),'@')) "
                "FROM instagram_accounts GROUP BY lower(ltrim(trim(current_username_norm),'@')) HAVING COUNT(*)>1)"
            )
        return result

    def username(self, username: str) -> dict:
        ids = set()
        if self.has("instagram_accounts", "id"):
            for field in ("current_username_norm", "current_username_display"):
                if self.has("instagram_accounts", field):
                    ids.update(row[0] for row in self.connection.execute(
                        f"SELECT id FROM instagram_accounts WHERE lower(ltrim(trim(\"{field}\"),'@'))=?", (username,),
                    ))
        if self.has("instagram_username_aliases", "account_id", "username_norm"):
            ids.update(row[0] for row in self.connection.execute(
                "SELECT account_id FROM instagram_username_aliases WHERE lower(ltrim(trim(username_norm),'@'))=?", (username,),
            ))
        identities = sorted(ids)
        result = {"username": username, "found": bool(ids), "matching_identity_count": len(ids)}
        result["accounts"] = self.details("instagram_accounts", "id", identities, (
            "id", "instagram_user_id", "current_username_norm", "current_username_display", "first_seen_at", "last_seen_at",
        ))
        result["aliases"] = self.details("instagram_username_aliases", "account_id", identities, ("account_id", "username_norm", "first_seen_at", "last_seen_at"))
        result["global_seen"] = self.details("global_seen", "account_id", identities, ("account_id", "first_seen_at", "last_seen_at"))
        for table in ("task_results", "workbench_candidates", "workbench_identity_claims", "workbench_collection_exclusions", "task_result_duplicate_archive", "global_identity_owners"):
            result[table] = self.details(table, "account_id", identities)
        candidate_ids = [row["id"] for row in result["workbench_candidates"]["rows"] if "id" in row]
        for table in ("workbench_review_decisions", "workbench_candidate_dismissals"):
            result[table] = self.details(table, "candidate_id", candidate_ids)
        return result


def inspect_database(path: Path, usernames: list[str]) -> dict:
    path = path.resolve()
    result = {"database_path": str(path), "exists": path.is_file()}
    if not result["exists"]:
        result["status"] = "not_found"
        return result
    # mode=ro does not create or migrate a database; immutable=1 must NOT be
    # used here, because it would silently omit current committed WAL records.
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("BEGIN")
            inspector = Inspector(connection)
            result["status"] = "inspected" if inspector.has("instagram_accounts", "id") else "unrecognized_schema"
            result["available_tables"] = sorted(inspector.columns)
            result["missing_tables"] = [table for table in TABLES if table not in inspector.columns]
            result["metrics"] = inspector.metrics()
            result["usernames"] = [inspector.username(username) for username in usernames]
            connection.rollback()
    except (sqlite3.Error, OSError) as error:
        # Do not echo arbitrary database exception text or embedded SQL content.
        result.update(status="read_failed", error_type=type(error).__name__,
                      message="Read failed. Check the path, access and database health; no repair was attempted.")
    return result


def build_report(paths: list[Path], usernames: list[str]) -> dict:
    databases = [inspect_database(path, usernames) for path in paths]
    existing = [item for item in databases if item["exists"]]
    return {
        "format": "juxin-dedupe-diagnostic-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "scope": "Identity keys, statuses and timestamps only. No profile, login, message, or credential content.",
        "requested_usernames": usernames,
        "existing_database_count": len(existing),
        "multiple_databases_found": len(existing) > 1,
        "active_database_identified": False,
        "interpretation": [
            "An existing pending record alone does not prove recollection; compare its original created_at with the earlier observation.",
            "Different files may belong to separate installations. This report does not infer which file the running app uses.",
            "A null metric means its required legacy tables or columns are unavailable, not zero.",
            "Each file is read in its own snapshot; actively collecting databases can change between files.",
        ],
        "databases": databases,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", action="append", default=[], help="Exact SQLite file; repeat to compare installations")
    parser.add_argument("--usernames", nargs="+", default=list(DEFAULT_USERNAMES), help="Instagram usernames or profile URLs to trace")
    parser.add_argument("--output", type=Path, default=ROOT / "installer-output" / "dedup-diagnostic.json")
    args = parser.parse_args(argv)
    try:
        usernames = list(dict.fromkeys(normalize_username(value) for value in args.usernames))
    except ValueError as error:
        parser.error(str(error))
    if len(usernames) > 100:
        parser.error("At most 100 usernames can be traced per report")
    paths = database_candidates(args.database)
    output = args.output.resolve()
    protected_paths = [item for path in paths for item in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal"))]
    protected = {os.path.normcase(str(item)) for item in protected_paths}
    same_existing_file = output.exists() and any(item.exists() and output.samefile(item) for item in protected_paths)
    if output.suffix.lower() != ".json" or os.path.normcase(str(output)) in protected or same_existing_file:
        parser.error("Output must be a JSON report file, never a database or SQLite sidecar")
    report = build_report(paths, usernames)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        print("Could not save the diagnostic report.", file=sys.stderr)
        return 1
    print(f"Report: {output}")
    print(f"Existing databases: {report['existing_database_count']}")
    print("No database records were changed. No files were uploaded.")
    return 0 if any(item["status"] == "inspected" for item in report["databases"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
