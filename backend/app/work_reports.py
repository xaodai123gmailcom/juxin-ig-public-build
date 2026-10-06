"""Read-only reports from complete durable business records, scoped to the owner.

Executor identity is captured at execution, never inferred from today's window
binding. Legacy rows without an executor remain explicitly unattributed.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from .errors import ValidationError
from .platform_scope import (validate_platform, account_platform_sql, username_platform_sql,
                             global_seen_platform_total, task_platform_sql)


async def capture_executor(worker, database, owner: str, profile_id: str) -> dict:
    from .instagram_home import OWN_PROFILE
    actor = {"profile_id": profile_id, "window_name": "", "username": "", "instagram_user_id": ""}
    with database.read() as c:
        row = c.execute("SELECT name FROM account_window_plans WHERE owner_user_id=? AND profile_id=? ORDER BY archived,serial LIMIT 1", (owner, profile_id)).fetchone()
        if row:
            actor["window_name"] = row["name"]
    try:
        context = getattr(worker, "_context", None)
        if context is None:
            return actor
        cookies = await asyncio.wait_for(context.cookies("https://www.instagram.com/"), timeout=2)
        actor["instagram_user_id"] = next((str(c["value"]) for c in cookies if c.get("name") == "ds_user_id" and str(c.get("value", "")).isdigit()), "")
        if not actor["instagram_user_id"]:
            return actor
        name = await asyncio.wait_for(worker.page.evaluate(OWN_PROFILE), timeout=2)
        import re
        if isinstance(name, str) and re.fullmatch(r"[a-zA-Z0-9._]{1,30}", name):
            actor["username"] = name.lower()
    except Exception:
        # Reporting metadata must not change a task's execution outcome.
        pass
    return actor


def _bound(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, AttributeError, TypeError):
        raise ValidationError("报表时间必须包含时区") from None


def _report_time(value):
    """Exact UTC key; old SQLite versions round microseconds across midnight.

    This function is local to read connections, never used by schema indexes or
    writes. Date indexes first narrow the input, including a one-second fringe.
    Legacy naive timestamps follow SQLite's UTC interpretation.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, AttributeError, TypeError, OverflowError):
        return None


# Seek the selected period before touching profile payloads. An identity first
# seen in an older period must not reappear when a later task/review copy exists:
# indexed anti-joins check all durable sources, with no retention window or cap.
# Julian-day indexes seek broadly; exact UTC keys protect local-date/DST
# boundaries, including microseconds on older packaged SQLite versions.
def _collection_period_sql(platform, *, identities_only=False):
    # Summary deduplicates the narrow period identities BEFORE the all-history
    # probes. A task result plus a review/exclusion must not repeat those seeks.
    result_scope = ("" if identities_only else account_platform_sql(platform, "r.account_id")) + task_platform_sql(platform, "t.settings_json")
    candidate_scope = "" if identities_only else account_platform_sql(platform, "r.account_id")
    identity_scope = account_platform_sql(platform, "e.account_id") if identities_only else ""
    result_columns = "r.account_id" if identities_only else "r.account_id,r.id AS evidence_id,0 AS priority,report_time(r.created_at) AS happened_at"
    candidate_columns = "r.account_id" if identities_only else "r.account_id,r.id,1,report_time(r.created_at)"
    exclusion_columns = "r.account_id" if identities_only else "r.account_id,r.id,2,report_time(r.excluded_at)"
    earlier_task_scope = task_platform_sql(platform, "t.settings_json")
    sql = f"""
WITH period_evidence AS (
 SELECT {result_columns}
 FROM task_results r INDEXED BY idx_results_report_period
 JOIN tasks t ON t.id=r.task_id
 WHERE t.owner_user_id=:owner {result_scope}
 AND julianday(r.created_at)>=julianday(:start)-1.0/86400 AND julianday(r.created_at)<julianday(:end)+1.0/86400
 AND (julianday(r.created_at)>=julianday(:start)+1.0/86400 OR report_time(r.created_at)>=:exact_start)
 AND (julianday(r.created_at)<julianday(:end)-1.0/86400 OR report_time(r.created_at)<:exact_end)
 UNION ALL
 SELECT {candidate_columns}
 FROM workbench_candidates r INDEXED BY idx_candidates_report_period
 WHERE r.owner_user_id=:owner {candidate_scope}
 AND julianday(r.created_at)>=julianday(:start)-1.0/86400 AND julianday(r.created_at)<julianday(:end)+1.0/86400
 AND (julianday(r.created_at)>=julianday(:start)+1.0/86400 OR report_time(r.created_at)>=:exact_start)
 AND (julianday(r.created_at)<julianday(:end)-1.0/86400 OR report_time(r.created_at)<:exact_end)
 UNION ALL
 SELECT {exclusion_columns}
 FROM workbench_collection_exclusions r INDEXED BY idx_exclusions_report_period
 WHERE r.owner_user_id=:owner {candidate_scope}
 AND julianday(r.excluded_at)>=julianday(:start)-1.0/86400 AND julianday(r.excluded_at)<julianday(:end)+1.0/86400
 AND (julianday(r.excluded_at)>=julianday(:start)+1.0/86400 OR report_time(r.excluded_at)>=:exact_start)
 AND (julianday(r.excluded_at)<julianday(:end)-1.0/86400 OR report_time(r.excluded_at)<:exact_end)
), eligible AS (
 SELECT e.* FROM period_evidence e
 WHERE 1=1 {identity_scope} AND NOT EXISTS (
   SELECT 1 FROM task_results r INDEXED BY idx_results_report_identity
   JOIN tasks t ON t.id=r.task_id
   WHERE r.account_id=e.account_id AND t.owner_user_id=:owner {earlier_task_scope}
   AND julianday(r.created_at)<julianday(:start)+1.0/86400 AND (julianday(r.created_at)<julianday(:start)-1.0/86400 OR report_time(r.created_at)<:exact_start)
 ) AND NOT EXISTS (
   SELECT 1 FROM workbench_candidates r INDEXED BY idx_candidates_report_identity WHERE r.account_id=e.account_id
   AND r.owner_user_id=:owner AND julianday(r.created_at)<julianday(:start)+1.0/86400 AND (julianday(r.created_at)<julianday(:start)-1.0/86400 OR report_time(r.created_at)<:exact_start)
 ) AND NOT EXISTS (
   SELECT 1 FROM workbench_collection_exclusions r INDEXED BY idx_exclusions_report_identity WHERE r.account_id=e.account_id
   AND r.owner_user_id=:owner AND julianday(r.excluded_at)<julianday(:start)+1.0/86400 AND (julianday(r.excluded_at)<julianday(:start)-1.0/86400 OR report_time(r.excluded_at)<:exact_start)
 )
)
"""
    return sql.replace("UNION ALL", "UNION") if identities_only else sql


def _events_sql(platform):
    return _collection_period_sql(platform) + """
, ranked AS (
 SELECT *,ROW_NUMBER() OVER(PARTITION BY account_id ORDER BY happened_at,priority,evidence_id) AS rank
 FROM eligible
), collection_payloads AS (
 SELECT e.happened_at,r.profile_json,r.target_id FROM ranked e
 JOIN task_results r ON e.priority=0 AND r.id=e.evidence_id WHERE e.rank=1
 UNION ALL
 SELECT e.happened_at,r.profile_json,r.source_target FROM ranked e
 JOIN workbench_candidates r ON e.priority=1 AND r.id=e.evidence_id WHERE e.rank=1
 UNION ALL
 SELECT e.happened_at,r.profile_snapshot_json,i.source_target FROM ranked e
 JOIN workbench_collection_exclusions r ON e.priority=2 AND r.id=e.evidence_id
 LEFT JOIN workbench_identity_claims i ON i.account_id=r.account_id WHERE e.rank=1
), events AS (
 SELECT 'collection' AS metric,r.happened_at,
        COALESCE(json_extract(r.profile_json,'$.executor.profile_id'),t.current_window_id,'') AS profile_id,
        COALESCE(json_extract(r.profile_json,'$.executor'),'{}') AS executor,1 AS amount
 FROM collection_payloads r LEFT JOIN task_targets t ON t.id=r.target_id
 UNION ALL
 SELECT s.operation,julianday(s.completed_at),c.profile_id,
        COALESCE(json_extract(a.details_json,'$.executor'),'{}'),1
 FROM action_success_ledger s INDEXED BY idx_actions_report_period
 JOIN action_campaigns c ON c.id=s.campaign_id AND c.owner_user_id=s.owner_user_id
 JOIN action_attempts a ON a.id=s.attempt_id
 WHERE s.owner_user_id=:owner AND a.status='confirmed'
 AND julianday(s.completed_at)>=julianday(:start)-1.0/86400 AND julianday(s.completed_at)<julianday(:end)+1.0/86400
 AND (julianday(s.completed_at)>=julianday(:start)+1.0/86400 OR report_time(s.completed_at)>=:exact_start)
 AND (julianday(s.completed_at)<julianday(:end)-1.0/86400 OR report_time(s.completed_at)<:exact_end)
 UNION ALL
 SELECT kind,julianday(COALESCE(json_extract(result_json,'$.confirmed_at'),updated_at)),profile_id,
        COALESCE(json_extract(result_json,'$.executor'),'{}'),1
 FROM studio_jobs WHERE owner_user_id=:owner AND status='completed'
 AND (kind='nurture' OR (kind='posting' AND json_extract(result_json,'$.published')=1))
 AND report_time(COALESCE(json_extract(result_json,'$.confirmed_at'),updated_at))>=:exact_start
 AND report_time(COALESCE(json_extract(result_json,'$.confirmed_at'),updated_at))<:exact_end
 UNION ALL
 SELECT 'check',julianday(checked_at),profile_id,json_object('username',owner_username),1
 FROM follow_monitor_rounds INDEXED BY idx_follow_rounds_report_period WHERE owner_user_id=:owner
 AND julianday(checked_at)>=julianday(:start)-1.0/86400 AND julianday(checked_at)<julianday(:end)+1.0/86400
 AND (julianday(checked_at)>=julianday(:start)+1.0/86400 OR report_time(checked_at)>=:exact_start)
 AND (julianday(checked_at)<julianday(:end)-1.0/86400 OR report_time(checked_at)<:exact_end)
 UNION ALL
 SELECT 'added',julianday(checked_at),profile_id,json_object('username',owner_username),added_count
 FROM follow_monitor_rounds INDEXED BY idx_follow_rounds_report_period WHERE owner_user_id=:owner AND added_count>0
 AND julianday(checked_at)>=julianday(:start)-1.0/86400 AND julianday(checked_at)<julianday(:end)+1.0/86400
 AND (julianday(checked_at)>=julianday(:start)+1.0/86400 OR report_time(checked_at)>=:exact_start)
 AND (julianday(checked_at)<julianday(:end)-1.0/86400 OR report_time(checked_at)<:exact_end)
 UNION ALL
 SELECT 'approved',julianday(reviewed_at),COALESCE(json_extract(profile_json,'$.executor.profile_id'),''),
        COALESCE(json_extract(profile_json,'$.executor'),'{}'),1
 FROM workbench_candidates INDEXED BY idx_candidates_report_reviewed
 WHERE owner_user_id=:owner AND status='approved' /*platform:candidate*/
 AND julianday(reviewed_at)>=julianday(:start)-1.0/86400 AND julianday(reviewed_at)<julianday(:end)+1.0/86400
 AND (julianday(reviewed_at)>=julianday(:start)+1.0/86400 OR report_time(reviewed_at)>=:exact_start)
 AND (julianday(reviewed_at)<julianday(:end)-1.0/86400 OR report_time(reviewed_at)<:exact_end)
)
""".replace('/*platform:candidate*/', account_platform_sql(platform, 'workbench_candidates.account_id'))


# Resolve the first completion across permanent facts and delayed history, but
# only rank generations with evidence in this period. Older duplicates use an
# identity/date seek instead of a lifetime history sort.
def _split_work_report_sql(platform):
    return """
WITH period_evidence AS (
 SELECT 'target:'||target_id AS generation,target_id AS source_target_id,
        report_time(completed_at) AS happened_at,source_window_id,0 AS priority,target_id AS evidence_id
 FROM split_completed_targets INDEXED BY idx_split_completed_targets_report_period
 WHERE owner_user_id=:owner /*platform:username*/
 AND julianday(completed_at)>=julianday(:start)-1.0/86400 AND julianday(completed_at)<julianday(:end)+1.0/86400
 AND (julianday(completed_at)>=julianday(:start)+1.0/86400 OR report_time(completed_at)>=:exact_start)
 AND (julianday(completed_at)<julianday(:end)-1.0/86400 OR report_time(completed_at)<:exact_end)
 UNION ALL
 SELECT COALESCE('target:'||NULLIF(source_target_id,''),'history:'||id),NULLIF(source_target_id,''),
        report_time(completed_at),source_window_id,1,id
 FROM split_candidate_history INDEXED BY idx_split_history_report_period
 WHERE owner_user_id=:owner AND source_status='completed' /*platform:username*/
 AND julianday(completed_at)>=julianday(:start)-1.0/86400 AND julianday(completed_at)<julianday(:end)+1.0/86400
 AND (julianday(completed_at)>=julianday(:start)+1.0/86400 OR report_time(completed_at)>=:exact_start)
 AND (julianday(completed_at)<julianday(:end)-1.0/86400 OR report_time(completed_at)<:exact_end)
), eligible AS (
 SELECT e.* FROM period_evidence e
 WHERE NOT EXISTS (
   SELECT 1 FROM split_completed_targets WHERE target_id=e.source_target_id
   AND owner_user_id=:owner /*platform:username*/ AND julianday(completed_at)<julianday(:start)+1.0/86400 AND (julianday(completed_at)<julianday(:start)-1.0/86400 OR report_time(completed_at)<:exact_start)
 ) AND NOT EXISTS (
   SELECT 1 FROM split_candidate_history INDEXED BY idx_split_history_report_identity
   WHERE owner_user_id=:owner AND source_target_id=e.source_target_id AND source_status='completed'
   /*platform:username*/ AND julianday(completed_at)<julianday(:start)+1.0/86400 AND (julianday(completed_at)<julianday(:start)-1.0/86400 OR report_time(completed_at)<:exact_start)
 )
), first_completions AS (
 SELECT *,ROW_NUMBER() OVER(PARTITION BY generation ORDER BY happened_at,priority,evidence_id) AS rank
 FROM eligible
)
""".replace("/*platform:username*/", username_platform_sql(platform, "username_norm"))


METRICS = ("collection", "check", "added", "nurture", "posting", "follow", "greet", "split", "approved", "confirmed_posting")
SUMMARY_METRICS = ("collection", "follow", "split", "added", "confirmed_posting")


def _parameters(owner, start, end):
    start, end = _bound(start), _bound(end)
    if start >= end:
        raise ValidationError("结束时间必须晚于开始时间")
    return {"owner": owner, "start": start, "end": end,
            "exact_start": _report_time(start), "exact_end": _report_time(end)}


def history_totals(database, owner: str, start: str, end: str, platform: str | None = None) -> dict:
    platform = validate_platform(platform)
    parameters = _parameters(owner, start, end)
    # Lifetime totals touch narrow identity indexes, while today's first-seen
    # dates use the same period seek as the report. Never normalize every
    # historical timestamp or load profiles just to count lifetime identities.
    sql = """
    WITH evidence AS (
      SELECT r.account_id FROM task_results r INDEXED BY idx_results_report_identity
      JOIN tasks t ON t.id=r.task_id WHERE t.owner_user_id=:owner /*platform:result*/
      UNION ALL
      SELECT account_id FROM workbench_candidates INDEXED BY idx_candidates_report_period
      WHERE owner_user_id=:owner /*platform:candidate*/
      UNION ALL
      SELECT account_id FROM workbench_collection_exclusions INDEXED BY idx_exclusions_report_period
      WHERE owner_user_id=:owner /*platform:exclusion*/
    ) SELECT COUNT(DISTINCT account_id) FROM evidence
    """
    sql = (sql.replace('/*platform:result*/', account_platform_sql(platform, 'r.account_id') + task_platform_sql(platform, 't.settings_json'))
        .replace('/*platform:candidate*/', account_platform_sql(platform, 'workbench_candidates.account_id'))
        .replace('/*platform:exclusion*/', account_platform_sql(platform, 'workbench_collection_exclusions.account_id')))
    with database.read() as c:
        c.create_function("report_time", 1, _report_time, deterministic=True)
        c.execute("BEGIN")
        total = c.execute(sql, parameters).fetchone()[0]
        today = c.execute(_collection_period_sql(platform, identities_only=True) +
            "SELECT COUNT(*) FROM eligible", parameters).fetchone()[0]
        return {**({"platform": platform} if platform is not None else {}), "total_collected": total, "today_collected": today,
                "global_dedupe": global_seen_platform_total(c, platform),
                "approved": c.execute("SELECT COUNT(*) FROM workbench_candidates WHERE owner_user_id=? AND status='approved'" + account_platform_sql(platform, "workbench_candidates.account_id"), (owner,)).fetchone()[0]}



def _confirmed_posting(connection, parameters, *, grouped=False):
    # Only the dedicated immutable native receipt proves a new-workflow post.
    # Keep the original Studio posting metric separately for historical CSVs.
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='posting_receipts'").fetchone():
        return [] if grouped else 0
    fields = ("'confirmed_posting' AS metric,profile_id,'' AS window_name,username,'' AS instagram_user_id,COUNT(*) AS amount"
              if grouped else "COUNT(*)")
    sql = "SELECT " + fields + """
        FROM posting_receipts WHERE owner_user_id=:owner
        AND CASE WHEN json_valid(evidence_json) THEN json_extract(evidence_json,'$.verification')='instagram_dialog' ELSE 0 END
        AND julianday(confirmed_at)>=julianday(:start)-1.0/86400
        AND julianday(confirmed_at)<julianday(:end)+1.0/86400
        AND (julianday(confirmed_at)>=julianday(:start)+1.0/86400 OR report_time(confirmed_at)>=:exact_start)
        AND (julianday(confirmed_at)<julianday(:end)-1.0/86400 OR report_time(confirmed_at)<:exact_end)
    """
    if grouped:
        return connection.execute(sql + " GROUP BY profile_id,username ORDER BY profile_id,username", parameters).fetchall()
    return int(connection.execute(sql, parameters).fetchone()[0])


def work_report(database, owner: str, start: str, end: str, platform: str | None = None, *, summary_only: bool = False) -> dict:
    platform = validate_platform(platform)
    parameters = _parameters(owner, start, end)
    with database.read() as c:
        # Every metric sees one WAL snapshot; no TTL/cache can hide fresh writes.
        c.create_function("report_time", 1, _report_time, deterministic=True)
        c.execute("BEGIN")
        if summary_only:
            totals = {key: 0 for key in SUMMARY_METRICS}
            totals['confirmed_posting'] = _confirmed_posting(c, parameters)
            totals['collection'] = c.execute(_collection_period_sql(platform, identities_only=True) +
                "SELECT COUNT(*) FROM eligible", parameters).fetchone()[0]
            totals['split'] = c.execute(_split_work_report_sql(platform) +
                "SELECT COUNT(*) FROM first_completions WHERE rank=1", parameters).fetchone()[0]
            totals['follow'] = c.execute("""SELECT COUNT(*) FROM action_success_ledger s INDEXED BY idx_actions_report_period
                JOIN action_campaigns c ON c.id=s.campaign_id AND c.owner_user_id=s.owner_user_id
                JOIN action_attempts a ON a.id=s.attempt_id
                WHERE s.owner_user_id=:owner AND s.operation='follow' AND a.status='confirmed'
                AND julianday(s.completed_at)>=julianday(:start)-1.0/86400 AND julianday(s.completed_at)<julianday(:end)+1.0/86400
                AND (julianday(s.completed_at)>=julianday(:start)+1.0/86400 OR report_time(s.completed_at)>=:exact_start)
 AND (julianday(s.completed_at)<julianday(:end)-1.0/86400 OR report_time(s.completed_at)<:exact_end)""", parameters).fetchone()[0]
            totals['added'] = c.execute("""SELECT COALESCE(SUM(added_count),0)
                FROM follow_monitor_rounds INDEXED BY idx_follow_rounds_report_period
                WHERE owner_user_id=:owner AND added_count>0
                AND julianday(checked_at)>=julianday(:start)-1.0/86400 AND julianday(checked_at)<julianday(:end)+1.0/86400
                AND (julianday(checked_at)>=julianday(:start)+1.0/86400 OR report_time(checked_at)>=:exact_start)
 AND (julianday(checked_at)<julianday(:end)-1.0/86400 OR report_time(checked_at)<:exact_end)""", parameters).fetchone()[0]
            return {**({"platform": platform} if platform is not None else {}),
                    "start": parameters['start'], "end": parameters['end'], "totals": totals}
        rows = c.execute(_events_sql(platform) + """
        SELECT metric,profile_id,
               COALESCE(json_extract(executor,'$.window_name'),'') AS window_name,
               COALESCE(json_extract(executor,'$.username'),'') AS username,
               COALESCE(json_extract(executor,'$.instagram_user_id'),'') AS instagram_user_id,
               SUM(amount) AS amount
        FROM events GROUP BY metric,profile_id,window_name,username,instagram_user_id
        ORDER BY profile_id,username,metric
        """, parameters).fetchall()
        rows += c.execute(_split_work_report_sql(platform) + """
            SELECT 'split' AS metric,COALESCE(source_window_id,'') AS profile_id,
                   '' AS window_name,'' AS username,'' AS instagram_user_id,COUNT(*) AS amount
            FROM first_completions WHERE rank=1 GROUP BY COALESCE(source_window_id,'') ORDER BY profile_id
            """, parameters).fetchall()
        rows += _confirmed_posting(c, parameters, grouped=True)
    totals = {key: 0 for key in METRICS}
    grouped = {}
    for row in rows:
        key = (row["profile_id"], row["window_name"], row["username"], row["instagram_user_id"])
        entry = grouped.setdefault(key, {"profile_id": key[0], "window_name": key[1], "username": key[2], "instagram_user_id": key[3], **{m: 0 for m in METRICS}})
        entry[row["metric"]] += row["amount"]
        totals[row["metric"]] += row["amount"]
    return {**({"platform": platform} if platform is not None else {}), "start": parameters['start'], "end": parameters['end'], "totals": totals, "rows": list(grouped.values()),
            "unattributed": sum(sum(r[m] for m in METRICS) for r in grouped.values() if not r["username"])}


# Completion is taken from immutable target history, never from manual labels.
# A delayed split generation may also appear in task_targets; prefer its richer
# history row, and keep ordinary directly-created collection targets visible.
SPLIT_REVIEW_SQL = """
WITH completed AS (
 SELECT 'history:'||h.id AS id,h.username_display AS username,h.completed_at,
        h.source_target_id,h.source_task_id,h.source_window_id,h.profile_json,h.username_norm
 FROM split_candidate_history h
 WHERE h.owner_user_id=:owner AND h.source_status='completed' /*platform:history*/
   AND julianday(h.completed_at)>=julianday(:start)
   AND julianday(h.completed_at)<julianday(:end)
 UNION ALL
 SELECT 'target:'||t.target_id,t.username_display,t.completed_at,t.target_id,t.source_task_id,
        t.source_window_id,'{}',t.username_norm
 FROM split_completed_targets t
 WHERE t.owner_user_id=:owner /*platform:target*/
   AND julianday(t.completed_at)>=julianday(:start)
   AND julianday(t.completed_at)<julianday(:end)
   AND NOT EXISTS(
     SELECT 1 FROM split_candidate_history h
     WHERE h.owner_user_id=:owner AND h.source_target_id=t.target_id
       AND h.source_status='completed' /*platform:history*/
   )
)
"""


def _split_review_sql(platform):
    return (SPLIT_REVIEW_SQL
        .replace('/*platform:history*/', username_platform_sql(platform, 'h.username_norm'))
        .replace('/*platform:target*/', username_platform_sql(platform, 't.username_norm')))


def _private_follow_review_sql(platform):
    return PRIVATE_FOLLOW_REVIEW_SQL.replace('/*platform:ig-only*/',
        username_platform_sql(platform, 'username_norm'))


def _review_period(start, end, utc_offset_minutes, now):
    """Read only the recent seven local dates; never prune business/dedup history.

    The current explicit offset preserves the renderer's local calendar rather
    than using the core machine's UTC date or a historical day's DST offset. A non-midnight end freezes today's pagination.
    Different boundary offsets support a DST transition without a 168-hour rule.
    """
    from datetime import timedelta
    if type(utc_offset_minutes) is not int or not -840 <= utc_offset_minutes <= 840:
        raise ValidationError('无效的本地时区偏移')
    try:
        lower = datetime.fromisoformat(start.replace('Z', '+00:00'))
        upper = datetime.fromisoformat(end.replace('Z', '+00:00'))
        if lower.tzinfo is None or upper.tzinfo is None:
            raise ValueError()
        if lower.time().replace(tzinfo=None) != datetime.min.time():
            raise ValueError()
        if abs(lower.utcoffset()) > timedelta(hours=14) or abs(upper.utcoffset()) > timedelta(hours=14):
            raise ValueError()
        today = (now or datetime.now(timezone.utc)).astimezone(
            timezone(timedelta(minutes=utc_offset_minutes))
        ).date()
        latest_date = upper.date() - (timedelta(days=1) if upper.time() == datetime.min.time() else timedelta())
        if lower >= upper or lower.date() < today - timedelta(days=6) or latest_date > today:
            raise ValueError()
        if (latest_date - lower.date()).days not in range(7):
            raise ValueError()
    except (ValueError, AttributeError, TypeError, OverflowError):
        raise ValidationError('审查报表仅支持最近 7 个本地日期，开始时间须为含时区的本地零点') from None
    return _bound(start), _bound(end), today


def _daily_review_bounds(today, utc_offset_minutes, daily_bounds, start, end):
    from datetime import timedelta
    dates = [today - timedelta(days=6 - index) for index in range(7)]
    zone = timezone(timedelta(minutes=utc_offset_minutes))
    if daily_bounds is None:
        result = []
        upper = datetime.fromisoformat(end.replace('Z', '+00:00'))
        for day in dates:
            lower = datetime.combine(day, datetime.min.time(), zone)
            higher = lower + timedelta(days=1)
            if day == today and upper.date() == today and upper.time().replace(tzinfo=None) != datetime.min.time():
                higher = upper
            result.append({'key': day.isoformat(), 'start': lower.isoformat(), 'end': higher.isoformat()})
        return result
    try:
        if not isinstance(daily_bounds, list) or len(daily_bounds) != 7:
            raise ValueError()
        daily_bounds = sorted(daily_bounds, key=lambda item: item['key'])
        result, previous_end = [], None
        selected_start, selected_end = datetime.fromisoformat(start.replace('Z', '+00:00')), datetime.fromisoformat(end.replace('Z', '+00:00'))
        for day, bounds in zip(dates, daily_bounds):
            lower = datetime.fromisoformat(bounds['start'].replace('Z', '+00:00'))
            upper = datetime.fromisoformat(bounds['end'].replace('Z', '+00:00'))
            if bounds['key'] != day.isoformat() or lower.date() != day or lower.time().replace(tzinfo=None) != datetime.min.time():
                raise ValueError()
            if lower.tzinfo is None or upper.tzinfo is None or abs(lower.utcoffset()) > timedelta(hours=14) or abs(upper.utcoffset()) > timedelta(hours=14):
                raise ValueError()
            full_day = upper.date() == day + timedelta(days=1) and upper.time().replace(tzinfo=None) == datetime.min.time()
            frozen_today = day == today and upper.date() == day
            if lower >= upper or not (full_day or frozen_today) or upper - lower > timedelta(hours=26):
                raise ValueError()
            if full_day and upper - lower < timedelta(hours=22):
                raise ValueError()
            if previous_end is not None and lower != previous_end:
                raise ValueError()
            if lower.date() == selected_start.date() and lower != selected_start:
                raise ValueError()
            if selected_end.date() == today and selected_end.time().replace(tzinfo=None) != datetime.min.time() and day == today and upper != selected_end:
                raise ValueError()
            result.append({'key': bounds['key'], 'start': lower.isoformat(), 'end': upper.isoformat()})
            previous_end = upper
        return result
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise ValidationError('每日审查数量必须使用连续的最近 7 个本地日期边界') from None


def _daily_review_counts(connection, sql, owner, bounds):
    parameters = {'owner': owner, 'start': _bound(bounds[0]['start']), 'end': _bound(bounds[-1]['end'])}
    values = []
    for index, item in enumerate(bounds):
        parameters.update({f'key{index}': item['key'], f'start{index}': _bound(item['start']), f'end{index}': _bound(item['end'])})
        values.append(f'(:key{index},:start{index},:end{index})')
    rows = connection.execute(sql + ',days(day,start_at,end_at) AS (VALUES ' + ','.join(values) + ''')
        SELECT days.day,COUNT(completed.id) AS count FROM days LEFT JOIN completed
        ON julianday(completed.completed_at)>=julianday(days.start_at)
        AND julianday(completed.completed_at)<julianday(days.end_at)
        GROUP BY days.day ORDER BY days.day''', parameters).fetchall()
    return {row['day']: row['count'] for row in rows}


def _review_decision_counts(connection, sql, parameters, kind):
    row = connection.execute(sql + '''SELECT
        COUNT(CASE WHEN d.decision='passed' THEN 1 END) AS passed,
        COUNT(CASE WHEN d.decision='failed' THEN 1 END) AS failed
        FROM completed LEFT JOIN report_review_decisions d
        ON d.owner_user_id=:owner AND d.review_kind=:kind AND d.record_id=completed.id''',
        {**parameters, 'kind': kind}).fetchone()
    return {'passed': row['passed'], 'failed': row['failed']}


def set_report_review_decision(database, owner: str, kind: str, record_id: str,
                               decision: str, start: str, end: str,
                               utc_offset_minutes: int, platform: str | None = None) -> dict:
    platform = validate_platform(platform)
    if kind not in ('split', 'private_follow') or decision not in ('passed', 'failed'):
        raise ValidationError('无效的审查判定')
    if not isinstance(record_id, str) or not 1 <= len(record_id) <= 160:
        raise ValidationError('无效的审查记录')
    lower, upper, _ = _review_period(start, end, utc_offset_minutes, None)
    sql = _split_review_sql(platform) if kind == 'split' else _private_follow_review_sql(platform)
    parameters = {'owner': owner, 'start': lower, 'end': upper, 'record_id': record_id}
    with database.write() as connection:
        exists = connection.execute(sql + 'SELECT 1 FROM completed WHERE id=:record_id LIMIT 1', parameters).fetchone()
        if not exists:
            raise ValidationError('审查记录已过期或不存在，请刷新记录')
        connection.execute('''INSERT INTO report_review_decisions
            (owner_user_id,review_kind,record_id,decision,reviewed_at)
            VALUES(?,?,?,?,?) ON CONFLICT(owner_user_id,review_kind,record_id)
            DO UPDATE SET decision=excluded.decision,reviewed_at=excluded.reviewed_at''',
            (owner, kind, record_id, decision, datetime.now(timezone.utc).isoformat()))
        counts = _review_decision_counts(connection, sql, parameters, kind)
    return {**({'platform': platform} if platform is not None else {}), 'record_id': record_id, 'decision': decision, 'decision_counts': counts}


def split_review_report(database, owner: str, start: str, end: str, *,
                        utc_offset_minutes: int, offset: int = 0, limit: int = 100,
                        now: datetime | None = None, daily_bounds: list[dict] | None = None,
                        platform: str | None = None) -> dict:
    platform = validate_platform(platform)
    from .review_layers import _page
    _page(offset, limit)
    sql = _split_review_sql(platform)
    start_utc, end_utc, today = _review_period(start, end, utc_offset_minutes, now)
    days = _daily_review_bounds(today, utc_offset_minutes, daily_bounds, start, end)
    parameters = {'owner': owner, 'start': start_utc, 'end': end_utc, 'offset': offset, 'limit': limit}
    with database.read() as connection:
        connection.execute('BEGIN')
        total = connection.execute(sql + 'SELECT COUNT(*) FROM completed', parameters).fetchone()[0]
        rows = connection.execute(
            sql + """SELECT completed.*,facts.completion_details_json,
                d.decision AS review_decision,
                admissions.successful_adds AS split_count_recorded,
                admissions.history_complete AS split_count_complete
                FROM completed LEFT JOIN split_admission_totals admissions
                ON admissions.owner_user_id=:owner AND admissions.username_norm=completed.username_norm
                LEFT JOIN split_completed_targets facts
                ON facts.owner_user_id=:owner AND facts.target_id=completed.source_target_id
                LEFT JOIN report_review_decisions d
                ON d.owner_user_id=:owner AND d.review_kind='split' AND d.record_id=completed.id
                ORDER BY julianday(completed.completed_at) DESC,completed.id DESC LIMIT :limit OFFSET :offset""",
            parameters,
        ).fetchall()
        daily_counts = _daily_review_counts(connection, sql, owner, days)
        decision_counts = _review_decision_counts(connection, sql, parameters, 'split')
    items = []
    for row in rows:
        item = dict(row)
        item.pop('username_norm', None)
        item['split_count_complete'] = bool(item['split_count_complete'])
        item['split_count_recorded'] = int(item['split_count_recorded'] or 0)
        item['split_count'] = item['split_count_recorded'] if item['split_count_complete'] else None
        try:
            profile = json.loads(item.pop('profile_json'))
        except (ValueError, TypeError):
            profile = {}
        item['profile'] = profile if isinstance(profile, dict) else {}
        from .split_completion_details import report_details
        item.update(report_details(item.pop('completion_details_json'), item['profile'], item['username']))
        items.append(item)
    return {**({'platform': platform} if platform is not None else {}), 'items': items, 'total': total, 'offset': offset, 'limit': limit,
            'has_more': offset + len(items) < total, 'start': start, 'end': end, 'retention_days': 7,
            'daily_counts': daily_counts, 'decision_counts': decision_counts}


PRIVATE_FOLLOW_REVIEW_SQL = """WITH completed AS (
    SELECT attempt_id AS id,username_display AS username,completed_at,source_window_id,
           profile_json,executor_json,confirmation
    FROM private_follow_completions WHERE owner_user_id=:owner /*platform:ig-only*/
      AND julianday(completed_at)>=julianday(:start) AND julianday(completed_at)<julianday(:end)
)
"""


def private_follow_review_report(database, owner: str, start: str, end: str, *,
        utc_offset_minutes: int, offset: int = 0, limit: int = 100,
        now: datetime | None = None, daily_bounds: list[dict] | None = None,
                        platform: str | None = None) -> dict:
    platform = validate_platform(platform)
    from .review_layers import _page
    from .split_completion_details import count, object_json
    from .private_follow_reviews import follow_state
    _page(offset, limit)
    sql = _private_follow_review_sql(platform)
    start_utc, end_utc, today = _review_period(start, end, utc_offset_minutes, now)
    days = _daily_review_bounds(today, utc_offset_minutes, daily_bounds, start, end)
    parameters = {'owner': owner, 'start': start_utc, 'end': end_utc, 'offset': offset, 'limit': limit}
    with database.read() as connection:
        connection.execute('BEGIN')
        total = connection.execute(sql + 'SELECT COUNT(*) FROM completed', parameters).fetchone()[0]
        rows = connection.execute(sql + '''SELECT completed.*,d.decision AS review_decision
            FROM completed LEFT JOIN report_review_decisions d
            ON d.owner_user_id=:owner AND d.review_kind='private_follow' AND d.record_id=completed.id
            ORDER BY julianday(completed_at) DESC,id DESC LIMIT :limit OFFSET :offset''', parameters).fetchall()
        daily_counts = _daily_review_counts(connection, sql, owner, days)
        decision_counts = _review_decision_counts(connection, sql, parameters, 'private_follow')
    items = []
    for row in rows:
        item = dict(row)
        profile, executor = object_json(item.pop('profile_json')), object_json(item.pop('executor_json'))
        item.update(profile=profile, status='confirmed', follow_state=follow_state(item['confirmation']),
            window_name=executor.get('window_name', ''), executor_username=executor.get('username', ''),
            **{field: count(profile.get(field)) for field in ('followers', 'following', 'posts')})
        items.append(item)
    return {**({'platform': platform} if platform is not None else {}), 'items': items, 'total': total, 'offset': offset, 'limit': limit,
        'has_more': offset + len(items) < total, 'start': start, 'end': end, 'retention_days': 7,
        'daily_counts': daily_counts, 'decision_counts': decision_counts}
