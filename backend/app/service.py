from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

from . import __source_revision__, __version__
from .database import Database
from .discovery_write_session import DiscoveryWriteSession
from .platform_scope import (validate_platform, username_platform_sql, task_platform_sql,
    account_platform_sql, assert_username_platform, assert_candidate_platforms, global_seen_platform_total)
from .social_platform import collection_platform, stored_task_settings
from .split_admissions import guard_new_direct_source, record_split_admission, was_split_executed
from .identity_registry import (
    identity_has_durable_history,
    merge_identity_owners,
    remember_registered_identity,
    remember_identity_owner,
    reserve_split_identity,
)
from .errors import (
    AuthenticationError,
    ConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from .browser_admission import assert_no_durable_window_hold
from .posting_retirement import legacy_studio_lease_entities, legacy_window_holds
from .person_recognition import (
    PERSON_RECOGNIZER_VERSION,
    normalize_person_category_fields,
)
from .security import hash_password, new_session_token, token_digest, verify_password

TASK_MODES = {"followers", "following", "post_likers"}
CREATABLE_TASK_MODES = {"followers", "following"}
RETIRED_TASK_MODES = {"likes", "post_likers"}
RETIRED_LIKER_SETTING_KEYS = {"like_posts_to_check", "max_likers_per_post"}
CANDIDATE_BATCH_MAX = 100
LIVE_TASK_RECENT_TARGET_LIMIT = 200
# Keep the two-branch campaign eligibility query below the historical SQLite
# 999-variable ceiling: each branch binds owner/operation plus the target batch.
ACTION_ELIGIBILITY_BATCH_MAX = 400
CANDIDATE_TERMINAL_STATES = {"recorded", "deduped"}
# Historical standalone exclusions had no follow-up task_result write. Current
# exclusions (including hover cards) write the exclusion and result separately;
# they remain resumable until that second durable write has committed.
_HISTORICAL_TERMINAL_EXCLUSION_REASONS = (
    "public_zero_posts", "legacy_public_secondary_review",
)
WORKBENCH_PROFILE_MAX_BYTES = 64 * 1024
WORKBENCH_SCREENING_MAX_BYTES = 64 * 1024
WORKBENCH_REVIEW_CACHE_MAX_BYTES = 256 * 1024
WORKBENCH_PENDING_CACHE_BUDGET_BYTES = 16 * 1024 * 1024
TASK_STATES = {
    "draft", "queued", "running", "waiting_network", "paused", "stopped",
    "completed", "failed", "recoverable",
}
VISIBILITY_VALUES = {"public", "private", "unknown", "not_visible"}
_IG_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_SENSITIVE_KEYS = {"password", "passwd", "cookie", "cookies", "authorization", "access_token", "refresh_token"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(value: str) -> Any:
    return json.loads(value)


def _has_historical_terminal_exclusion(connection: sqlite3.Connection, account_id: str) -> bool:
    return connection.execute(
        """SELECT 1 FROM workbench_collection_exclusions
           WHERE account_id=? AND reason_code IN (?, ?) LIMIT 1""",
        (account_id, *_HISTORICAL_TERMINAL_EXCLUSION_REASONS),
    ).fetchone() is not None


def _normalize_allowed_window_ids(values: Iterable[Any]) -> list[str]:
    """Validate and preserve the operator's eligible-window order."""

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_profile_id in values:
        profile_id = str(raw_profile_id).strip()
        if (
            not profile_id
            or len(profile_id) > 128
            or any(ord(character) < 32 for character in profile_id)
        ):
            raise ValidationError("Invalid BitBrowser profile id")
        if profile_id not in seen:
            seen.add(profile_id)
            normalized.append(profile_id)
    return normalized


def _replace_split_candidate_window_affinity(
    connection: sqlite3.Connection,
    candidate_id: str,
    allowed_window_ids: Iterable[str],
) -> None:
    """Replace one waiting generation's hard window pool inside its transaction."""

    ordered = list(allowed_window_ids)
    connection.execute(
        "DELETE FROM split_candidate_window_affinity WHERE candidate_id=?",
        (candidate_id,),
    )
    connection.executemany(
        """
        INSERT INTO split_candidate_window_affinity(
            candidate_id, profile_id, queue_order
        ) VALUES(?, ?, ?)
        """,
        (
            (candidate_id, profile_id, queue_order)
            for queue_order, profile_id in enumerate(ordered, start=1)
        ),
    )


_KNOWN_PERSON_CATEGORIES = {"male", "female", "couple"}
_STABLE_UNKNOWN_PERSON_REASONS = {"no_face_detected", "child_only"}


def _person_recognition_rank(
    profile: dict[str, Any] | None,
    screening: dict[str, Any] | None,
) -> int:
    """Rank durable recognition so concurrent backfill can only improve it."""

    profile = profile if isinstance(profile, dict) else {}
    screening = screening if isinstance(screening, dict) else {}
    recognition = screening.get("person_recognition")
    recognition = recognition if isinstance(recognition, dict) else {}
    category = str(
        profile.get("person_category") or recognition.get("category") or "unknown"
    ).strip().casefold()
    if category in _KNOWN_PERSON_CATEGORIES:
        return 2
    if (
        recognition.get("checked") is True
        and str(recognition.get("recognizer_version") or "")
        == PERSON_RECOGNIZER_VERSION
        and str(recognition.get("reason") or "")
        in _STABLE_UNKNOWN_PERSON_REASONS
    ):
        # Only a current-version semantic unknown is stable. Old recognizers and
        # operational/quality failures (timeout, missing avatar, tiny face, low
        # confidence) remain eligible for a later high-quality backfill.
        return 1
    return 0


def _stored_person_recognition_rank(row: sqlite3.Row | None) -> int:
    if row is None:
        return 0
    try:
        profile = _loads(row["profile_json"])
        screening = _loads(row["screening_json"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 0
    return _person_recognition_rank(profile, screening)


def _without_retired_liker_settings(settings: Any) -> dict[str, Any]:
    """Return a defensive public/execution view of legacy task settings.

    Installed databases can contain the removed post-liker mode and its two limit
    keys.  Keeping result/checkpoint rows is important for history, but exposing
    those configuration keys makes a new client believe the feature is still
    runnable.  The cleanup is deliberately non-mutating so it is safe at every
    serialization boundary as well as during the database migration.
    """

    if not isinstance(settings, dict):
        return {}
    cleaned = {
        str(key): value
        for key, value in settings.items()
        if key not in RETIRED_LIKER_SETTING_KEYS
    }
    raw_limits = cleaned.get("mode_limits")
    if isinstance(raw_limits, dict):
        cleaned["mode_limits"] = {
            str(mode): {
                str(key): value
                for key, value in limits.items()
                if key not in RETIRED_LIKER_SETTING_KEYS
            }
            for mode, limits in raw_limits.items()
            if mode not in RETIRED_TASK_MODES and isinstance(limits, dict)
        }
    return cleaned


def _parse_stored_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reconcile_browser_leases(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    active_collection_entity_ids: set[str] | None = None,
    active_action_entity_ids: set[str] | None = None,
    active_monitor_entity_ids: set[str] | None = None,
    active_studio_entity_ids: set[str] | None = None,
    active_posting_entity_ids: set[str] | None = None,
    inactive_grace_seconds: int = 5,
    remove_terminal_without_manager: bool = True,
    protected_tokens: set[str] | None = None,
) -> int:
    """Remove leases that cannot represent live work in this application instance.

    Posting holds are durable and remain fenced until explicit cleanup; other
    expired and orphaned rows can be reconciled as stale. When the caller supplies the current
    in-memory manager ids, a recent heartbeat receives a short start/cleanup grace so
    an HTTP profiles refresh cannot race task startup or worker disconnect.
    """
    rows = connection.execute(
        """
        SELECT
            lease.profile_id,
            lease.owner_user_id,
            lease.operation_type,
            lease.entity_id,
            lease.lease_token,
            lease.heartbeat_at,
            lease.expires_at,
            task.owner_user_id AS task_owner_user_id,
            task.status AS task_status,
            campaign.owner_user_id AS campaign_owner_user_id,
            campaign.status AS campaign_status
        FROM browser_operation_leases lease
        LEFT JOIN tasks task
          ON lease.operation_type='collection' AND task.id=lease.entity_id
        LEFT JOIN action_campaigns campaign
          ON lease.operation_type='action' AND campaign.id=lease.entity_id
        """
    ).fetchall()
    # Retired work has no executor authorized to release its old generation.
    # Keep exact legacy studio entity locks even when row ownership mismatches.
    retired_studio_ids = legacy_studio_lease_entities(connection)
    known_nurture_owners = {(row['id'], row['owner_user_id'], row['profile_id'])
                           for row in connection.execute("SELECT job.id,job.owner_user_id,job.profile_id FROM browser_operation_leases lease "
                               "JOIN studio_jobs job ON lease.operation_type='studio' AND job.id=lease.entity_id WHERE job.kind='nurture'")}
    cutoff = now - timedelta(seconds=max(0, inactive_grace_seconds))
    pending_nurture_profiles = {item[0] for item in connection.execute(
        "SELECT profile_id FROM studio_jobs WHERE kind='nurture' AND status='completed' "
        "AND json_extract(result_json,'$.window_hold')=1")}
    stale_profile_ids: list[str] = []
    for row in rows:
        # Keep even a mismatched successor for manual reconciliation. An old
        # cleanup receipt grants no authority to scavenge that new generation.
        if row['profile_id'] in pending_nurture_profiles:
            continue
        if row["lease_token"] in (protected_tokens or ()):
            continue
        active_ids = {
            "collection": active_collection_entity_ids,
            "action": active_action_entity_ids,
            "monitor": active_monitor_entity_ids,
            "studio": active_studio_entity_ids,
            "posting": active_posting_entity_ids,
        }.get(row["operation_type"])
        if active_ids is not None and row["entity_id"] in active_ids:
            continue
        expires_at = _parse_stored_datetime(row["expires_at"])
        heartbeat_at = _parse_stored_datetime(row["heartbeat_at"])
        expired = expires_at is None or expires_at <= now
        heartbeat_is_old = heartbeat_at is None or heartbeat_at <= cutoff
        operation_type = row["operation_type"]
        entity_id = row["entity_id"]
        stale = expired
        if operation_type == "collection":
            manager_active = (
                active_collection_entity_ids is not None
                and entity_id in active_collection_entity_ids
            )
            orphaned = (
                row["task_status"] is None
                or row["task_owner_user_id"] != row["owner_user_id"]
            )
            manager_known_inactive = (
                active_collection_entity_ids is not None
                and entity_id not in active_collection_entity_ids
            )
            terminal_inactive = (
                row["task_status"] in {"completed", "stopped", "failed", "recoverable"}
                and not manager_active
                and (
                    active_collection_entity_ids is not None
                    or remove_terminal_without_manager
                )
            )
            stale = stale or orphaned or terminal_inactive or (
                manager_known_inactive and heartbeat_is_old
            )
        elif operation_type == "action":
            manager_active = (
                active_action_entity_ids is not None
                and entity_id in active_action_entity_ids
            )
            orphaned = (
                row["campaign_status"] is None
                or row["campaign_owner_user_id"] != row["owner_user_id"]
            )
            manager_known_inactive = (
                active_action_entity_ids is not None
                and entity_id not in active_action_entity_ids
            )
            terminal_inactive = (
                row["campaign_status"] in {"completed", "stopped", "failed", "recoverable"}
                and not manager_active
                and (
                    active_action_entity_ids is not None
                    or remove_terminal_without_manager
                )
            )
            stale = stale or orphaned or (
                terminal_inactive or manager_known_inactive and heartbeat_is_old
            )
        elif operation_type == "account":
            # Short, explicit management operation; preserve until release or TTL.
            stale = expired
        elif operation_type == "posting":
            # Expiry and token-free terminal rows cannot prove browser cleanup.
            stale = False
        elif operation_type == "studio":
            stale = False if (entity_id in retired_studio_ids or
                              (entity_id, row['owner_user_id'], row['profile_id']) not in known_nurture_owners) else (
                stale or (active_studio_entity_ids is not None
                          and entity_id not in active_studio_entity_ids and heartbeat_is_old))
        elif operation_type == "monitor":
            manager_known_inactive = (
                active_monitor_entity_ids is not None
                and entity_id not in active_monitor_entity_ids
            )
            stale = stale or (manager_known_inactive and heartbeat_is_old) or (
                active_monitor_entity_ids is None and heartbeat_is_old and remove_terminal_without_manager
            )
        else:
            stale = True
        if stale:
            stale_profile_ids.append(row["profile_id"])
    if stale_profile_ids:
        placeholders = ",".join("?" for _ in stale_profile_ids)
        connection.execute(
            f"DELETE FROM browser_operation_leases WHERE profile_id IN ({placeholders})",
            stale_profile_ids,
        )
    return len(stale_profile_ids)


def _normalize_stored_visibility(value: Any, profile: dict[str, Any] | None = None) -> str:
    """Normalize current and legacy visibility representations at the API boundary.

    New writes always use the canonical text values validated by ``record_result``.
    Older databases and imported result payloads may instead carry ``account_type``,
    ``privacy`` or boolean ``is_private`` fields inside ``profile_json``.  Treat an
    explicit canonical row value as authoritative, but use those rendered-profile
    fields when the stored row is unknown so a known private account is not exposed
    to the desktop UI as unknown.
    """

    def classify(candidate: Any) -> str | None:
        if isinstance(candidate, bool):
            return "private" if candidate else "public"
        if isinstance(candidate, int) and candidate in {0, 1}:
            return "private" if candidate else "public"
        if not isinstance(candidate, str):
            return None
        compact = re.sub(r"[\s_-]+", "", candidate.strip().casefold())
        if compact in {"private", "privateaccount", "私密", "私密账户", "true", "1"}:
            return "private"
        if compact in {"public", "publicaccount", "公开", "公开账户", "false", "0"}:
            return "public"
        return None

    direct = classify(value)
    if direct is not None:
        return direct
    profile = profile or {}
    for key in ("visibility", "account_type", "accountType", "privacy", "is_private", "isPrivate", "private"):
        normalized = classify(profile.get(key))
        if normalized is not None:
            return normalized
    return "not_visible" if isinstance(value, str) and value.strip().casefold() == "not_visible" else "unknown"


def normalize_app_username(username: str) -> tuple[str, str]:
    display = username.strip()
    if not 3 <= len(display) <= 64:
        raise ValidationError("Username must contain between 3 and 64 characters")
    if any(ord(character) < 32 for character in display):
        raise ValidationError("Username contains invalid control characters")
    return display.casefold(), display


def normalize_instagram_username(value: str) -> tuple[str, str]:
    candidate = value.strip()
    if candidate.lower().startswith("fb:"):
        raise ValidationError("此版本仅支持 Instagram 账号，不接受 FB 标识")
    if candidate.lower().startswith(("http://", "https://")):
        parsed = urlparse(candidate)
        if parsed.hostname not in {"instagram.com", "www.instagram.com"}:
            raise ValidationError("Only instagram.com profile URLs are accepted")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValidationError("Instagram profile URL does not contain a username")
        candidate = parts[0]
    candidate = candidate.lstrip("@").strip()
    if not _IG_USERNAME_RE.fullmatch(candidate):
        raise ValidationError(f"Invalid Instagram username: {value!r}")
    return candidate.casefold(), candidate


def validate_task_settings(settings: dict[str, Any]) -> dict[str, Any]:
    settings = _without_retired_liker_settings(settings)
    defaults: dict[str, Any] = {
        "platform": "instagram",
        "followers_min": 0,
        "followers_max": 0,
        "following_min": 0,
        "following_max": 0,
        "posts_min": 0,
        "posts_max": 0,
        "active_days_max": 0,
        # Location remains mandatory after the count gate. New/legacy tasks that omitted the
        # field must not silently skip it and turn an entire batch into UNKNOWN.
        "location_enabled": True,
        "gpt_enabled": False,
        "local_person_recognition": False,
        # Missing on legacy/restored tasks means no gender-based exclusion.
        "exclude_male_avatar": False,
        "live_queue_enabled": False,
        "exclude_verified": False,
        # Public profiles with a confirmed zero count are discarded by default
        # at every entry point; an explicitly saved False still opts out.
        "exclude_public_zero_posts": True,
        "discard_count_limits_enabled": True,
        "private_discard_followers_max": 4000,
        "private_discard_following_max": 4000,
        "private_discard_posts_max": 4000,
        "public_discard_followers_max": 4000,
        "public_discard_following_max": 4000,
        "public_discard_posts_max": 4000,
        "public_discard_active_days_max": 0,
        "dedupe_enabled": True,
        "auto_classify": False,
        "parallel_screening_workers": 1,
        "unlimited_relation_collection": False,
        "mode_limits": {},
    }
    unknown = set(settings) - set(defaults)
    if unknown:
        raise ValidationError("Unknown task settings", details={"keys": sorted(unknown)})
    result = defaults | settings
    result["platform"] = collection_platform(result)

    numeric_keys = (
        "followers_min",
        "followers_max",
        "following_min",
        "following_max",
        "posts_min",
        "posts_max",
        "active_days_max",
    )
    for key in numeric_keys:
        value = result[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValidationError(f"{key} must be a non-negative integer or null")

    for visibility in ("private", "public"):
        for count_name in ("followers", "following", "posts"):
            key = f"{visibility}_discard_{count_name}_max"
            value = result[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError(f"{key} must be a non-negative integer")

    activity_discard_max = result["public_discard_active_days_max"]
    if (
        isinstance(activity_discard_max, bool)
        or not isinstance(activity_discard_max, int)
        or activity_discard_max < 0
    ):
        raise ValidationError("public_discard_active_days_max must be a non-negative integer")

    parallel_screening_workers = result["parallel_screening_workers"]
    if (
        isinstance(parallel_screening_workers, bool)
        or not isinstance(parallel_screening_workers, int)
        or not 1 <= parallel_screening_workers <= 3
    ):
        raise ValidationError("parallel_screening_workers must be an integer from 1 to 3")

    for minimum, maximum in (
        ("followers_min", "followers_max"),
        ("following_min", "following_max"),
        ("posts_min", "posts_max"),
    ):
        if (
            result[minimum] not in (None, 0)
            and result[maximum] not in (None, 0)
            and result[minimum] > result[maximum]
        ):
            raise ValidationError(f"{minimum} cannot exceed {maximum}")

    for key in (
        "location_enabled",
        "gpt_enabled",
        "local_person_recognition",
        "exclude_male_avatar",
        "live_queue_enabled",
        "exclude_verified",
        "exclude_public_zero_posts",
        "discard_count_limits_enabled",
        "dedupe_enabled",
        "auto_classify",
        "unlimited_relation_collection",
    ):
        if not isinstance(result[key], bool):
            raise ValidationError(f"{key} must be a boolean")
    # Gender inference/filtering is retired, including saved tasks and older clients.
    result["exclude_male_avatar"] = False
    result["local_person_recognition"] = False
    # The service is also called by crash recovery and older on-disk jobs, not
    # only by the strict HTTP schemas.  Coerce legacy false to the mandatory
    # NewGen invariant instead of allowing it to bypass location or making an
    # otherwise recoverable task unreadable.
    result["location_enabled"] = True
    mode_limits = result["mode_limits"]
    if not isinstance(mode_limits, dict) or set(mode_limits) - TASK_MODES:
        raise ValidationError("mode_limits must contain only enabled collection mode names")
    allowed_limit_keys = {
        "followers_min",
        "followers_max",
        "following_min",
        "following_max",
        "posts_min",
        "posts_max",
        "active_days_max",
        "per_target_limit",
    }
    cleaned_limits: dict[str, dict[str, int | None]] = {}
    for mode, limits in mode_limits.items():
        if not isinstance(limits, dict) or set(limits) - allowed_limit_keys:
            raise ValidationError(f"Invalid mode limits for {mode}")
        cleaned: dict[str, int | None] = {}
        for key, value in limits.items():
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValidationError(f"mode_limits.{mode}.{key} must be a non-negative integer or null")
            if key == "per_target_limit" and value is not None and value < 1:
                raise ValidationError("per_target_limit must be a positive integer")
            cleaned[key] = value
        for minimum, maximum in (
            ("followers_min", "followers_max"),
            ("following_min", "following_max"),
            ("posts_min", "posts_max"),
        ):
            lower = cleaned.get(minimum)
            upper = cleaned.get(maximum)
            if lower not in (None, 0) and upper not in (None, 0) and lower > upper:
                raise ValidationError(
                    f"mode_limits.{mode}.{minimum} cannot exceed {maximum}"
                )
        cleaned_limits[mode] = cleaned
    result["mode_limits"] = cleaned_limits
    return result


def _reject_sensitive_fields(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized == "platform" and isinstance(child, str) and child.strip().casefold() in {"facebook", "fb", "messenger"}:
                raise ValidationError("此版本不接受 Facebook 资料")
            if normalized in _SENSITIVE_KEYS or "password" in normalized or "cookie" in normalized:
                raise ValidationError(f"Sensitive field is not allowed: {path}.{key}")
            _reject_sensitive_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_fields(child, f"{path}[{index}]")


def _event(
    connection: sqlite3.Connection,
    owner_user_id: str,
    entity_type: str,
    entity_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO event_log(owner_user_id, entity_type, entity_id, event_type, payload_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (owner_user_id, entity_type, entity_id, event_type, _json(payload), isoformat()),
    )


def _bounded_workbench_json(
    value: dict[str, Any],
    *,
    label: str,
    max_bytes: int,
    allow_inline_images: bool = False,
) -> str:
    """Serialize a workbench object while keeping the durable database bounded."""

    _reject_sensitive_fields(value, label)

    def reject_inline_binary(candidate: Any, path: str) -> None:
        if isinstance(candidate, dict):
            for key, child in candidate.items():
                reject_inline_binary(child, f"{path}.{key}")
        elif isinstance(candidate, list):
            for index, child in enumerate(candidate):
                reject_inline_binary(child, f"{path}[{index}]")
        elif isinstance(candidate, str):
            compact = candidate.lstrip().casefold()
            if compact.startswith("data:image/") and allow_inline_images:
                return
            if compact.startswith(("data:image/", "data:video/", "data:application/octet-stream")):
                raise ValidationError(
                    f"Inline binary data is not allowed: {path}",
                    details={"store_as": "temporary file reference"},
                )

    reject_inline_binary(value, label)
    encoded = _json(value)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValidationError(
            f"{label} is too large",
            details={"max_bytes": max_bytes},
        )
    return encoded


def _shed_inline_review_previews(value: Any) -> Any:
    """Remove disposable inline images while retaining useful review metadata."""

    if isinstance(value, dict):
        return {
            key: _shed_inline_review_previews(child)
            for key, child in value.items()
            if not (
                isinstance(child, str)
                and child.lstrip().casefold().startswith("data:image/")
            )
        }
    if isinstance(value, list):
        return [
            _shed_inline_review_previews(child)
            for child in value
            if not (
                isinstance(child, str)
                and child.lstrip().casefold().startswith("data:image/")
            )
        ]
    return value


def _disposable_review_cache_json(
    review_cache: dict[str, Any],
    *,
    shed_previews: bool = False,
) -> str:
    """Serialize optional review previews without blocking a business record.

    The cache is only a rendering convenience.  Validate it as before, but if
    valid preview data crosses the per-row boundary, first remove inline images
    and finally discard the cache rather than rejecting the candidate itself.
    """

    full_json: str | None = None
    try:
        # Run this validation even when the caller already wants a smaller cache:
        # stripping an image must not conceal a sensitive key or a forbidden
        # non-image inline payload.
        full_json = _bounded_workbench_json(
            review_cache,
            label="review_cache",
            max_bytes=WORKBENCH_REVIEW_CACHE_MAX_BYTES,
            allow_inline_images=True,
        )
    except ValidationError as error:
        if not (
            error.message == "review_cache is too large"
            and error.details.get("max_bytes") == WORKBENCH_REVIEW_CACHE_MAX_BYTES
        ):
            raise

    if full_json is not None and not shed_previews:
        return full_json

    without_inline_previews = _shed_inline_review_previews(review_cache)
    try:
        return _bounded_workbench_json(
            without_inline_previews,
            label="review_cache",
            max_bytes=WORKBENCH_REVIEW_CACHE_MAX_BYTES,
            allow_inline_images=False,
        )
    except ValidationError as error:
        if not (
            error.message == "review_cache is too large"
            and error.details.get("max_bytes") == WORKBENCH_REVIEW_CACHE_MAX_BYTES
        ):
            raise
        # Review-cache metadata itself can also be unexpectedly large.  It is
        # safe to lose this optional cache; the candidate, dedupe identity,
        # task/checkpoint and later review decision are durable business data.
        return "{}"


def _bump_workbench_revision(connection: sqlite3.Connection, now: str) -> int:
    connection.execute(
        """
        UPDATE workbench_state_revision
        SET revision=revision+1, updated_at=?
        WHERE singleton_id=1
        """,
        (now,),
    )
    row = connection.execute(
        "SELECT revision FROM workbench_state_revision WHERE singleton_id=1"
    ).fetchone()
    return int(row["revision"])


def _global_seen_total(connection: sqlite3.Connection) -> int:
    """Return the trigger-maintained IG total without scanning the ledger."""

    row = connection.execute(
        "SELECT total_count FROM global_seen_platform_stats WHERE platform='instagram'"
    ).fetchone()
    if row is None:
        raise RuntimeError("Global dedupe statistics are not initialized")
    return int(row["total_count"])


def _user_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username_display"],
        "created_at": row["created_at"],
    }


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Read an optional projection/migrated column from a sqlite row."""

    return row[key] if key in row.keys() else default


def _pending_relation_cursor_names(raw: str | dict[str, Any]) -> Any:
    cursor = _loads(raw) if isinstance(raw, str) else raw
    for _ in range(6):
        if not isinstance(cursor, dict):
            return []
        nested = cursor.get("resume_cursor")
        if not isinstance(nested, dict):
            return cursor.get("pending_relation_usernames", [])
        cursor = nested
    return cursor.get("pending_relation_usernames", []) if isinstance(cursor, dict) else []


def _target_dict(
    row: sqlite3.Row,
    mode_progress: dict[str, dict[str, int | None]] | None = None,
    mode_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assert_username_platform(None, row["username_display"])
    status = row["status"]
    current_stage = _row_value(row, "current_stage")
    try:
        allowed_window_ids = _loads(
            _row_value(row, "allowed_window_ids_json", "[]") or "[]"
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        allowed_window_ids = []
    if not isinstance(allowed_window_ids, list):
        allowed_window_ids = []
    allowed_window_ids = [
        str(profile_id)
        for profile_id in allowed_window_ids
        if isinstance(profile_id, str) and profile_id
    ]
    return {
        "id": row["id"],
        "username": row["username_display"],
        "queue_order": row["queue_order"],
        "status": row["status"],
        "preferred_window_id": row["preferred_window_id"],
        "current_window_id": row["current_window_id"],
        "allowed_window_ids": allowed_window_ids,
        "current_stage": current_stage,
        "last_success_at": _row_value(row, "last_success_at"),
        "last_error": _row_value(row, "last_error"),
        "collection_list_dismissed": bool(_row_value(row, "collection_list_dismissed", 0)),
        "completion_policy": _row_value(row, "completion_policy"),
        "source_recheck": {
            "mode": _row_value(row, "source_recheck_mode"),
            "state": _row_value(row, "source_recheck_state"),
            "completed_at": _row_value(row, "source_recheck_completed_at"),
            "requested_at": _row_value(row, "source_recheck_requested_at"),
        } if _row_value(row, "source_recheck_mode") else None,
        "manual_recovery_required": bool(
            _row_value(row, "manual_recovery_required", 0)
        ),
        "network_state": (
            "waiting_network"
            if status == "waiting_network" and current_stage == "waiting_network"
            else "interrupted_recoverable"
            if current_stage == "interrupted_recoverable"
            else None
        ),
        # Progress is mode-scoped.  In particular, `saved` must never be derived
        # from the task-wide result count because one account may be observed by
        # several source modes and global dedupe can skip it in a later mode.
        "mode_progress": mode_progress or {},
        "mode_coverage": mode_coverage or {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _task_dict(
    row: sqlite3.Row,
    targets: Iterable[sqlite3.Row] = (),
    windows: Iterable[sqlite3.Row] = (),
    mode_progress_by_target: dict[str, dict[str, dict[str, int | None]]] | None = None,
    coverage_by_target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_rows = list(targets)
    # Persisted v0.2.25 tasks keep their original mode and settings so recovery,
    # checkpoints and history remain readable after the UI removes post-likers.
    modes = _loads(row["modes_json"])
    progress_by_target = mode_progress_by_target or {}
    for target in target_rows:
        target_progress = progress_by_target.setdefault(target["id"], {})
        for mode in modes:
            target_progress.setdefault(
                mode,
                {
                    "source_total": None,
                    "discovered": 0,
                    "processed": 0,
                    "saved": 0,
                    "skipped_global_duplicates": 0,
                    "qualified_for_review": 0,
                },
            )
    settings = _without_retired_liker_settings(stored_task_settings(row["settings_json"]))
    # Match new task defaults when restoring an older row that predates this
    # field, without replacing the operator's explicitly saved False.
    collection_platform(settings)
    settings.pop("facebook_relation_strategy", None)
    settings.setdefault("platform", "instagram")
    settings.setdefault("exclude_public_zero_posts", True)
    # Databases upgraded from an earlier release may contain false here.  The
    # execution manager consumes this serialized task view, so forcing the gate
    # on also prevents an old paused task from resuming without the location gate.
    settings["location_enabled"] = True
    # Relationship collection no longer has an application quantity ceiling.
    # Older databases may still contain ``unlimited_relation_collection=false``
    # and a finite per-target limit.  The execution manager consumes this
    # serialized view during restart/recovery, so normalize those legacy tasks at
    # the durable boundary as well as new Desktop requests.
    if any(mode in {"followers", "following"} for mode in modes):
        settings["unlimited_relation_collection"] = True
        mode_limits = settings.get("mode_limits")
        if isinstance(mode_limits, dict):
            for mode in {"followers", "following"}:
                limits = mode_limits.get(mode)
                if isinstance(limits, dict):
                    limits.pop("per_target_limit", None)
    return {
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "modes": modes,
        "settings": settings,
        "assignment_mode": row["assignment_mode"],
        "version": row["version"],
        "restart_count": row["restart_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "last_error": row["last_error"],
        "targets": [
            _target_dict(target, progress_by_target.get(target["id"]), (coverage_by_target or {}).get(target["id"]))
            for target in target_rows
        ],
        "window_ids": [window["profile_id"] for window in windows],
    }


def _normalize_private_review_screening(screening: dict[str, Any]) -> dict[str, Any]:
    """Route a new/recovered pending review without mutating historical evidence."""
    screening = dict(screening)
    legacy_tier = str(screening.get("review_tier") or "").strip().casefold()
    legacy_route = str(screening.get("routing_result") or "").strip().casefold()
    if legacy_tier == "secondary" or legacy_route == "private_secondary_review":
        screening.setdefault("legacy_private_review", {
            "review_tier": screening.get("review_tier"),
            "review_reason": screening.get("review_reason"),
            "routing_result": screening.get("routing_result"),
        })
        screening["review_reason"] = "private_secondary_retired_pending_review"
        screening["routing_result"] = "private_review"
    screening["review_tier"] = "primary"
    return screening


def _review_requires_exclusion(visibility: str, profile: dict[str, Any], screening: dict[str, Any]) -> bool:
    # Older imports can contain the saved terminal decision without its separate
    # exclusion row. That evidence must never turn into a private review on resume.
    if str(screening.get("routing_result") or "").strip().casefold().startswith("excluded_"):
        return True
    public_requires_exclusion = False
    if visibility == "public":
        review_tier = str(screening.get("review_tier") or "").strip().casefold()
        routing_result = str(
            screening.get("routing_result") or ""
        ).strip().casefold()
        public_requires_exclusion = review_tier in {"secondary", "excluded"} or (
            routing_result
            in {"public_secondary_review", "excluded_public_conditions", "excluded_non_us"}
        )
        basic = screening.get("basic")
        if isinstance(basic, dict) and "passed" in basic:
            public_requires_exclusion = (
                public_requires_exclusion or basic.get("passed") is not True
            )
        location = screening.get("location")
        if isinstance(location, dict) and "passed" in location:
            # None means the visible profile did not provide enough evidence.
            # It remains neutral for human review; only an explicit False is
            # proof of a non-US location and therefore collection-excluded.
            location_unknown = location.get("passed") is None
            public_requires_exclusion = public_requires_exclusion or (
                location.get("passed") is not True and not location_unknown
            )
        activity = screening.get("activity")
        if isinstance(activity, dict) and "passed" in activity:
            activity_passed = activity.get("passed")
            activity_enabled = activity.get("enabled")
            profile_posts = profile.get("posts")
            zero_post_activity_review = (
                not isinstance(profile_posts, bool)
                and isinstance(profile_posts, (int, float))
                and profile_posts == 0
                and activity.get("checked") is True
                and activity_passed is None
                and activity.get("reason") == "activity_no_posts"
            )
            activity_disabled = (
                activity_enabled is not True
                and activity_passed is None
                and activity.get("reason") == "collected_for_review"
            )
            public_requires_exclusion = public_requires_exclusion or (
                activity_passed is False
                or (
                    activity_enabled is True
                    and activity_passed is not True
                    and not zero_post_activity_review
                )
                or (
                    activity_enabled is not False
                    and activity_passed is not True
                    and not activity_disabled
                    and not zero_post_activity_review
                )
            )
        verified = screening.get("verified")
        if (
            isinstance(verified, dict)
            and verified.get("enabled") is True
            and "passed" in verified
        ):
            public_requires_exclusion = (
                public_requires_exclusion or verified.get("passed") is not True
            )
    return public_requires_exclusion


class CoreService:
    def __init__(self, database: Database, *, session_hours: int = 168) -> None:
        self.database = database
        self.session_hours = session_hours
        # Access is serialized by the same Database.write lock as queue mutations.
        # These operation tokens fence live teardown, never survive a Core crash.
        self._target_removal_tokens: dict[tuple[str, str], object] = {}

    def begin_target_removal(self, owner_user_id: str, task_id: str, target_id: str) -> object:
        with self.database.write() as connection:
            self._owned_task(connection, owner_user_id, task_id)
            if connection.execute("SELECT 1 FROM task_targets WHERE id=? AND task_id=?", (target_id, task_id)).fetchone() is None:
                raise NotFoundError("Task target not found")
            key = (owner_user_id, target_id)
            if key in self._target_removal_tokens:
                raise ConflictError("该目标正在移除，清理完成前不能重新调度")
            token = object()
            self._target_removal_tokens[key] = token
            return token

    def end_target_removal(self, owner_user_id: str, target_id: str, token: object) -> None:
        with self.database.write():
            key = (owner_user_id, target_id)
            if self._target_removal_tokens.get(key) is token:
                self._target_removal_tokens.pop(key, None)

    def _guard_target_removal(self, owner_user_id: str, target_id: str, token: object | None = None) -> None:
        active = self._target_removal_tokens.get((owner_user_id, target_id))
        if active is not None and active is not token:
            raise ConflictError("该目标正在移除，清理完成前不能重新调度")

    async def acquire_browser_lease_async(self, *args, **kwargs) -> str:
        import asyncio
        from .async_cleanup import finish_owned
        token = None
        async def acquire():
            nonlocal token
            token = await asyncio.to_thread(self.acquire_browser_lease, *args, **kwargs)
            return token
        try:
            return await finish_owned(acquire())
        except BaseException:
            if token is not None:
                profile = args[1] if len(args) > 1 else kwargs['profile_id']
                await finish_owned(asyncio.to_thread(self.release_browser_lease, profile, token))
            raise

    @staticmethod
    def _mode_progress_for_targets(
        connection: sqlite3.Connection,
        target_ids: Iterable[str],
    ) -> dict[str, dict[str, dict[str, int | None]]]:
        """Project durable, per-mode collection progress for task APIs.

        The trigger-maintained candidate counter is the authoritative live count
        for discovery and processing.  Checkpoint counters provide the most recent
        source-header total and preserve progress after terminal technical spool
        compaction.  This intentionally never consults task_results, whose global
        dedupe and multi-source merge semantics make it unsuitable for mode
        progress.
        """

        normalized_ids = list(dict.fromkeys(str(item) for item in target_ids if item))
        if not normalized_ids:
            return {}
        result: dict[str, dict[str, dict[str, int | None]]] = {}
        # Keep each statement below SQLite's conservative host-parameter limit.
        for offset in range(0, len(normalized_ids), 400):
            batch = normalized_ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            candidate_rows = connection.execute(
                f"""
                SELECT target_id, mode,
                       total AS discovered,
                       recorded + deduped AS processed,
                       recorded AS saved,
                       deduped AS skipped_global_duplicates
                FROM task_mode_candidate_counters
                WHERE target_id IN ({placeholders})
                """,
                batch,
            ).fetchall()
            live_modes = {(row["target_id"], row["mode"]) for row in candidate_rows}
            for row in candidate_rows:
                result.setdefault(row["target_id"], {})[row["mode"]] = {
                    "source_total": None,
                    "discovered": int(row["discovered"] or 0),
                    "processed": int(row["processed"] or 0),
                    "saved": int(row["saved"] or 0),
                    "skipped_global_duplicates": int(row["skipped_global_duplicates"] or 0),
                    "qualified_for_review": 0,
                }

            checkpoint_rows = connection.execute(
                f"""
                SELECT target_id, mode, counters_json
                FROM task_checkpoints
                WHERE target_id IN ({placeholders})
                """,
                batch,
            ).fetchall()
            for row in checkpoint_rows:
                try:
                    counters = _loads(row["counters_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    counters = {}
                if not isinstance(counters, dict):
                    counters = {}
                progress = result.setdefault(row["target_id"], {}).setdefault(
                    row["mode"],
                    {
                        "source_total": None,
                        "discovered": None,
                        "processed": None,
                        "saved": None,
                        "skipped_global_duplicates": None,
                        "qualified_for_review": 0,
                    },
                )

                layers: list[dict[str, Any]] = []
                layer = counters
                for _ in range(5):
                    if not isinstance(layer, dict):
                        break
                    layers.append(layer)
                    previous = layer.get("previous_counters")
                    if not isinstance(previous, dict):
                        break
                    layer = previous

                def layer_int(layer_value: dict[str, Any], key: str) -> int | None:
                    value = layer_value.get(key)
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        return None
                    return value

                discovery_restored = False
                processing_restored = False
                for layer_value in layers:
                    source_total = layer_int(layer_value, "source_total")
                    if source_total is not None and progress["source_total"] is None:
                        progress["source_total"] = source_total
                    # A recovery checkpoint can predate a spool repair.  It may
                    # supply the header count, but must never hide live pending
                    # work by inflating discovery/processing from an old run.
                    if (row["target_id"], row["mode"]) in live_modes:
                        continue
                    discovered = layer_int(layer_value, "discovered")
                    if discovered is None:
                        # Legacy spool checkpoints called this value visible_accounts.
                        discovered = layer_int(layer_value, "visible_accounts")
                    if discovered is not None and not discovery_restored:
                        progress["discovered"] = discovered
                        discovery_restored = True
                    if processing_restored:
                        continue
                    saved = layer_int(layer_value, "saved")
                    processed = layer_int(layer_value, "processed")
                    deduped = layer_int(layer_value, "skipped_global_duplicates")
                    if saved is None and processed is None and deduped is None:
                        continue
                    # Recover one snapshot, never independently take maxima from
                    # different recovery generations.  A corrected latest count
                    # (including zero) must not be overwritten by older counters.
                    # Exactly two valid values can determine the third. Legacy
                    # checkpoints with only a processed count cannot prove dedupe.
                    if processed is None and saved is not None and deduped is not None:
                        processed = saved + deduped
                    elif saved is None and processed is not None and deduped is not None:
                        if processed >= deduped:
                            saved = processed - deduped
                        else:
                            deduped = None
                    elif deduped is None and processed is not None and saved is not None:
                        if processed >= saved:
                            deduped = processed - saved
                    if (
                        processed is not None and saved is not None and deduped is not None
                        and processed != saved + deduped
                    ):
                        # Imported/corrupt counters are not evidence for a
                        # confident dedupe count. Keep the unknown visible.
                        deduped = None
                    if processed is not None:
                        progress["processed"] = processed
                    if saved is not None:
                        progress["saved"] = saved
                    progress["skipped_global_duplicates"] = deduped
                    processing_restored = True

            # Exact source evidence is updated in the same transaction as each
            # result/claim/review/exclusion mutation. Seek only displayed targets;
            # never recount a growing target's lifetime identities on each poll.
            progress_rows = connection.execute(
                f"""SELECT target_id, mode, qualified, recorded_total,
                           excluded_total, discarded, hover_discarded
                    FROM workbench_progress_totals
                    WHERE target_id IN ({placeholders})""", batch,
            ).fetchall()
            progress_evidence = {
                (row["target_id"], row["mode"]): row for row in progress_rows
            }
            review_evidence = {
                key: int(row["qualified"]) for key, row in progress_evidence.items()
                if int(row["qualified"]) > 0
            }
            for (target_id, mode) in review_evidence:
                result.setdefault(target_id, {}).setdefault(
                    mode,
                    {
                        "source_total": None,
                        "discovered": None,
                        "processed": None,
                        "saved": None,
                        "skipped_global_duplicates": None,
                        "qualified_for_review": 0,
                    },
                )
            old_unattributed_modes: dict[tuple[str, str], int | None] = {}
            for target_id in batch:
                for mode, progress in result.get(target_id, {}).items():
                    qualified = review_evidence.get((target_id, mode), 0)
                    saved = progress.get("saved")
                    # Legacy checkpoint totals alone cannot attribute old saved
                    # accounts to a review queue.  A live source counter proves
                    # that an empty review lookup means zero actual admissions.
                    old_unattributed = (
                        qualified == 0
                        and (target_id, mode) not in live_modes
                        and (
                            (saved is not None and saved > 0)
                            or (saved is None and (progress.get("processed") or 0) > 0)
                        )
                    )
                    if old_unattributed:
                        old_unattributed_modes[(target_id, mode)] = saved
                    progress["qualified_for_review"] = (
                        None if old_unattributed else qualified
                    )
            if old_unattributed_modes:
                # Only this legacy, counter-only case needs to examine stored
                # results.  If every old saved result is demonstrably a direct
                # collection exclusion, zero admissions is known even after
                # the technical spool was compacted.  Never guess from the
                # checkpoint when results or their provenance are missing.
                excluded_evidence = {
                    key: row for key, row in progress_evidence.items()
                    if int(row["recorded_total"]) > 0
                }
                for (target_id, mode), saved in old_unattributed_modes.items():
                    row = excluded_evidence.get((target_id, mode))
                    if (
                        row is not None
                        and int(row["recorded_total"]) == int(row["excluded_total"])
                        and (saved is None or int(row["recorded_total"]) >= saved)
                    ):
                        result[target_id][mode]["qualified_for_review"] = 0
            # Exclusions have their own durable identity/source attribution.
            # Hover rejection is part of saved records, not a previously-seen
            # duplicate. Keep those counters separate instead of subtracting
            # manual-review admissions from an unrelated displayed total.
            exclusions = progress_evidence
            for target_id in batch:
                for mode, progress in result.get(target_id,{}).items():
                    row=exclusions.get((target_id,mode))
                    progress['discarded']=int(row['discarded'] or 0) if row else 0
                    progress['hover_discarded']=int(row['hover_discarded'] or 0) if row else 0
        return result

    # Authentication -----------------------------------------------------
    def register_user(self, username: str, password: str) -> dict[str, Any]:
        username_norm, username_display = normalize_app_username(username)
        password_hash = hash_password(password)
        user_id = str(uuid.uuid4())
        created_at = isoformat()
        try:
            with self.database.write() as connection:
                connection.execute(
                    """
                    INSERT INTO app_users(id, username_norm, username_display, password_hash, created_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (user_id, username_norm, username_display, password_hash, created_at),
                )
                row = connection.execute("SELECT * FROM app_users WHERE id=?", (user_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Username is already registered") from exc
        return _user_dict(row)

    def login(
        self,
        username: str,
        password: str,
        *,
        remember_login: bool = False,
        auto_login: bool = False,
    ) -> dict[str, Any]:
        username_norm, _ = normalize_app_username(username)
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM app_users WHERE username_norm=? AND disabled_at IS NULL", (username_norm,)
            ).fetchone()
        if row is None or not verify_password(password, row["password_hash"]):
            raise AuthenticationError("Invalid username or password")

        token = new_session_token()
        now = utc_now()
        expires_at = now + timedelta(hours=self.session_hours)
        session_id = str(uuid.uuid4())
        remember_login = bool(remember_login or auto_login)
        with self.database.write() as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions(
                    id, user_id, token_hash, remember_login, auto_login, created_at, expires_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    row["id"],
                    token_digest(token),
                    int(remember_login),
                    int(auto_login),
                    isoformat(now),
                    isoformat(expires_at),
                ),
            )
        return {
            "token": token,
            "token_type": "bearer",
            "expires_at": isoformat(expires_at),
            "remember_login": remember_login,
            "auto_login": bool(auto_login),
            "user": _user_dict(row),
        }

    def authenticate(self, token: str) -> dict[str, Any]:
        if not token:
            raise AuthenticationError("Missing application session token")
        now = isoformat()
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT u.*, s.id AS session_id
                FROM auth_sessions s
                JOIN app_users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>? AND u.disabled_at IS NULL
                """,
                (token_digest(token), now),
            ).fetchone()
        if row is None:
            raise AuthenticationError("Session is invalid, expired or revoked")
        result = _user_dict(row)
        result["session_id"] = row["session_id"]
        return result

    def logout(self, token: str) -> None:
        digest = token_digest(token)
        with self.database.write() as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (isoformat(), digest),
            )

    # Tasks --------------------------------------------------------------
    def check_completed_targets(
        self, owner_user_id: str, targets: list[str]
    ) -> dict[str, Any]:
        """Return source accounts that previously completed collection.

        This legacy read-only endpoint reports completions. Admission guards also
        reject other existing task histories; retries use their original controls.
        """
        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for target in targets:
            username_norm, username_display = normalize_instagram_username(target)
            if username_norm not in seen:
                normalized.append((username_norm, username_display))
                seen.add(username_norm)
        if not normalized:
            raise ValidationError("At least one target is required")
        placeholders = ",".join("?" for _ in normalized)
        with self.database.read() as connection:
            rows = connection.execute(
                f"""
                SELECT target.username_norm, target.username_display,
                       target.updated_at AS completed_at, task.id AS task_id,
                       task.modes_json
                FROM task_targets target
                JOIN tasks task ON task.id=target.task_id
                WHERE task.owner_user_id=? AND target.status='completed'
                  AND target.username_norm IN ({placeholders})
                ORDER BY target.updated_at DESC
                """,
                (owner_user_id, *(item[0] for item in normalized)),
            ).fetchall()
        latest = {}
        for row in rows:
            latest.setdefault(row["username_norm"], row)
        completed = [
            {
                "username": display,
                "username_norm": norm,
                "completed_at": latest[norm]["completed_at"],
                "task_id": latest[norm]["task_id"],
                "modes": _loads(latest[norm]["modes_json"]),
            }
            for norm, display in normalized if norm in latest
        ]
        return {
            "completed": completed,
            "completed_count": len(completed),
            "clear_count": len(normalized) - len(completed),
        }

    def create_task(
        self,
        owner_user_id: str,
        *,
        name: str,
        modes: list[str],
        targets: list[str],
        settings: dict[str, Any],
        window_ids: list[str] | None = None,
        assignment_mode: str = "sequential",
        allow_completed_targets: bool = False,
    ) -> dict[str, Any]:
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise ValidationError("Task name must contain between 1 and 100 characters")
        requested_modes = list(dict.fromkeys(str(mode) for mode in modes))
        unknown_modes = set(requested_modes) - CREATABLE_TASK_MODES - RETIRED_TASK_MODES
        if unknown_modes:
            raise ValidationError("At least one valid collection mode is required")
        normalized_modes = [mode for mode in requested_modes if mode in CREATABLE_TASK_MODES]
        if not normalized_modes:
            raise ValidationError(
                "帖子点赞采集已取消；请至少选择粉丝采集或关注采集"
            )
        if assignment_mode not in {"sequential", "manual"}:
            raise ValidationError("assignment_mode must be sequential or manual")
        validated_settings = validate_task_settings(settings)

        normalized_targets: list[tuple[str, str]] = []
        seen_targets: set[str] = set()
        for target in targets:
            username_norm, username_display = normalize_instagram_username(target)
            if username_norm not in seen_targets:
                normalized_targets.append((username_norm, username_display))
                seen_targets.add(username_norm)

        normalized_windows: list[str] = []
        for raw_profile_id in window_ids or []:
            profile_id = str(raw_profile_id).strip()
            if not profile_id or len(profile_id) > 128 or any(ord(character) < 32 for character in profile_id):
                raise ValidationError("Invalid BitBrowser profile id")
            if profile_id not in normalized_windows:
                normalized_windows.append(profile_id)

        task_id = str(uuid.uuid4())
        now = isoformat()
        with self.database.write() as connection:
            if connection.execute("SELECT 1 FROM app_users WHERE id=?", (owner_user_id,)).fetchone() is None:
                raise AuthenticationError("Application user no longer exists")
            for username_norm, _ in normalized_targets:
                # Do not let an explicit task insertion relink a locked waiting
                # row to a pending target and make its normal unlock API unusable.
                self._guard_locked_split_username(connection, owner_user_id, username_norm)
            for username_norm, _ in normalized_targets:
                guard_new_direct_source(connection, owner_user_id, username_norm, allow_completed=allow_completed_targets)
            if not normalized_targets:
                if not validated_settings.get("live_queue_enabled") or not normalized_windows:
                    raise ValidationError(
                        "An empty task requires a live split queue and at least one window"
                    )
                selected_window_placeholders = ",".join(
                    "?" for _ in normalized_windows
                )
                if connection.execute(
                    f"""
                    SELECT 1 FROM split_candidates
                    WHERE owner_user_id=? AND candidate_kind='manual'
                  {username_platform_sql(validated_settings["platform"], "username_norm")}
                      AND queue_state='queued'
                      AND queued_target_id IS NULL
                  AND dispatch_locked=0
                      AND NOT EXISTS(SELECT 1 FROM collection_dispatch_locks gate WHERE gate.owner_user_id=split_candidates.owner_user_id AND gate.locked=1)
                      AND COALESCE(manual_category_override, '')!='completed'
                      AND COALESCE(source_status, '')!='dismissed_by_user'
                      AND (
                        source_target_id IS NULL OR NOT EXISTS (
                          SELECT 1
                          FROM task_targets source
                          JOIN task_windows source_window
                            ON source_window.task_id=source.task_id
                          WHERE source.id=split_candidates.source_target_id
                            AND NOT EXISTS (
                              SELECT 1 FROM task_list_dismissals dismissal
                              WHERE dismissal.task_id=source.task_id
                                AND dismissal.owner_user_id=split_candidates.owner_user_id
                            )
                        )
                      )
                      AND (
                        NOT EXISTS(
                          SELECT 1
                          FROM split_candidate_window_affinity affinity
                          WHERE affinity.candidate_id=split_candidates.id
                        )
                        OR EXISTS(
                          SELECT 1
                          FROM split_candidate_window_affinity affinity
                          WHERE affinity.candidate_id=split_candidates.id
                            AND affinity.profile_id IN ({selected_window_placeholders})
                        )
                      )
                    LIMIT 1
                    """,
                    (owner_user_id, *normalized_windows),
                ).fetchone() is None:
                    raise ValidationError(
                        "No split candidate is waiting for dispatch in the selected windows"
                    )
            connection.execute(
                """
                INSERT INTO tasks(
                    id, owner_user_id, name, status, modes_json, settings_json,
                    assignment_mode, created_at, updated_at
                ) VALUES(?, ?, ?, 'draft', ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    owner_user_id,
                    name,
                    _json(normalized_modes),
                    _json(validated_settings),
                    assignment_mode,
                    now,
                    now,
                ),
            )
            for queue_order, (username_norm, username_display) in enumerate(normalized_targets, start=1):
                record_split_admission(connection, owner_user_id, username_norm, now)
                reserve_split_identity(
                    connection, username_norm, username_display, now,
                    source="task_source", owner_user_id=owner_user_id,
                )
                connection.execute(
                    """
                    INSERT INTO task_targets(
                        id, task_id, username_norm, username_display, queue_order, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), task_id, username_norm, username_display, queue_order, now, now),
                )
            for queue_order, profile_id in enumerate(normalized_windows, start=1):
                connection.execute(
                    "INSERT INTO task_windows(task_id, profile_id, queue_order) VALUES(?, ?, ?)",
                    (task_id, profile_id, queue_order),
                )
            _event(connection, owner_user_id, "task", task_id, "task.created", {"target_count": len(normalized_targets)})
            task_row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            target_rows = connection.execute(
                "SELECT * FROM task_targets WHERE task_id=? ORDER BY queue_order", (task_id,)
            ).fetchall()
            window_rows = connection.execute(
                "SELECT * FROM task_windows WHERE task_id=? ORDER BY queue_order", (task_id,)
            ).fetchall()
        return _task_dict(task_row, target_rows, window_rows)

    def has_queued_split_candidates(self, owner_user_id: str, *, platform: str | None = None) -> bool:
        with self.database.read() as connection:
            return connection.execute(
                f"""
                SELECT 1 FROM split_candidates
                WHERE owner_user_id=? AND candidate_kind='manual'
                  {username_platform_sql(platform, "username_norm")}
                  AND queue_state='queued'
                  AND queued_target_id IS NULL
                  AND dispatch_locked=0
                      AND NOT EXISTS(SELECT 1 FROM collection_dispatch_locks gate WHERE gate.owner_user_id=split_candidates.owner_user_id AND gate.locked=1)
                  AND COALESCE(manual_category_override, '')!='completed'
                  AND COALESCE(source_status, '')!='dismissed_by_user'
                  AND (
                    source_target_id IS NULL OR NOT EXISTS (
                      SELECT 1
                      FROM task_targets source
                      JOIN task_windows source_window
                        ON source_window.task_id=source.task_id
                      WHERE source.id=split_candidates.source_target_id
                        AND NOT EXISTS (
                          SELECT 1 FROM task_list_dismissals dismissal
                          WHERE dismissal.task_id=source.task_id
                            AND dismissal.owner_user_id=split_candidates.owner_user_id
                        )
                    )
                  )
                LIMIT 1
                """,
                (owner_user_id,),
            ).fetchone() is not None

    def has_claimable_split_candidates(
        self, owner_user_id: str, task_id: str
    ) -> bool:
        """Return whether this task may atomically claim a delayed split row.

        Recovery rows whose source task is still visible and owns at least one
        window are intentionally pinned to that source task so a different task
        cannot discard their checkpoint. Generic pasted rows, hidden source tasks,
        windowless source tasks, and orphaned legacy recoveries may be claimed by
        any live task.
        """

        with self.database.read() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            if not _loads(task["settings_json"]).get("live_queue_enabled"):
                return False
            return connection.execute(
                f"""
                SELECT 1 FROM split_candidates candidate
                WHERE candidate.owner_user_id=?
                  {username_platform_sql(collection_platform(_loads(task["settings_json"])), "candidate.username_norm")}
                  AND candidate.candidate_kind='manual'
                  AND candidate.queue_state='queued'
                  AND candidate.queued_target_id IS NULL
                  AND candidate.dispatch_locked=0
                  AND NOT EXISTS(SELECT 1 FROM collection_dispatch_locks gate WHERE gate.owner_user_id=candidate.owner_user_id AND gate.locked=1)
                  AND COALESCE(candidate.manual_category_override, '')!='completed'
                  AND (
                    candidate.source_target_id IS NULL
                    OR candidate.source_task_id=?
                    OR NOT EXISTS(
                      SELECT 1
                      FROM task_targets source
                      JOIN task_windows source_window
                        ON source_window.task_id=source.task_id
                      WHERE source.id=candidate.source_target_id
                        AND NOT EXISTS (
                          SELECT 1 FROM task_list_dismissals dismissal
                          WHERE dismissal.task_id=source.task_id
                            AND dismissal.owner_user_id=candidate.owner_user_id
                        )
                    )
                  )
                  AND (
                    NOT EXISTS(
                      SELECT 1
                      FROM split_candidate_window_affinity affinity
                      WHERE affinity.candidate_id=candidate.id
                    )
                    OR EXISTS(
                      SELECT 1
                      FROM split_candidate_window_affinity affinity
                      JOIN task_windows selected_window
                        ON selected_window.task_id=?
                       AND selected_window.profile_id=affinity.profile_id
                      WHERE affinity.candidate_id=candidate.id
                    )
                  )
                  AND NOT EXISTS(
                    SELECT 1 FROM task_targets existing
                    WHERE existing.task_id=?
                      AND existing.username_norm=candidate.username_norm
                      AND (existing.status IN (
                        'running', 'waiting_network', 'completed'
                      ) OR existing.current_stage='deleted_archived')
                  )
                LIMIT 1
                """,
                (owner_user_id, task_id, task_id, task_id),
            ).fetchone() is not None

    def add_targets(self, owner_user_id: str, task_id: str, targets: list[str]) -> dict[str, Any]:
        if not targets:
            raise ValidationError("At least one target is required")
        with self.database.read() as connection:
            self._owned_task(connection, owner_user_id, task_id)
        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for target in targets:
            username_norm, username_display = normalize_instagram_username(target)
            if username_norm not in seen:
                normalized.append((username_norm, username_display))
                seen.add(username_norm)
        now = isoformat()
        added_targets: list[dict[str, Any]] = []
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            for username_norm, _ in normalized:
                self._guard_locked_split_username(connection, owner_user_id, username_norm)
            if task["status"] in {"completed", "failed"}:
                raise ConflictError("Restart the task before adding targets")
            next_order = connection.execute(
                "SELECT COALESCE(MAX(queue_order), 0) + 1 FROM task_targets WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            for username_norm, username_display in normalized:
                if connection.execute("SELECT 1 FROM task_targets WHERE task_id=? AND username_norm=?", (task_id, username_norm)).fetchone():
                    continue
                guard_new_direct_source(connection, owner_user_id, username_norm)
                record_split_admission(connection, owner_user_id, username_norm, now)
                reserve_split_identity(
                    connection, username_norm, username_display, now,
                    source="task_source", owner_user_id=owner_user_id,
                )
                target_id = str(uuid.uuid4())
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO task_targets(
                        id, task_id, username_norm, username_display, queue_order, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (target_id, task_id, username_norm, username_display, next_order, now, now),
                )
                if cursor.rowcount:
                    row = connection.execute(
                        "SELECT * FROM task_targets WHERE id=?", (target_id,)
                    ).fetchone()
                    added_targets.append(_target_dict(row))
                    next_order += 1
            connection.execute(
                "UPDATE tasks SET updated_at=?, version=version+1 WHERE id=?", (now, task_id)
            )
            _event(
                connection,
                owner_user_id,
                "task",
                task_id,
                "task.targets_added",
                {"added": len(added_targets)},
            )
        return {
            "added": len(added_targets),
            "targets": added_targets,
            "task": self.get_task(owner_user_id, task_id),
        }

    def add_task_windows(
        self, owner_user_id: str, task_id: str, window_ids: list[str]
    ) -> dict[str, Any]:
        """Persist additional windows after the execution manager fences all of them."""
        normalized: list[str] = []
        for raw_profile_id in window_ids:
            profile_id = str(raw_profile_id).strip()
            if not profile_id or len(profile_id) > 128 or any(
                ord(character) < 32 for character in profile_id
            ):
                raise ValidationError("Invalid BitBrowser profile id")
            if profile_id in normalized:
                raise ConflictError(
                    "BitBrowser window is repeated in the request",
                    details={"profile_id": profile_id},
                )
            normalized.append(profile_id)
        if not normalized:
            raise ValidationError("At least one BitBrowser window is required")

        now = isoformat()
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            if not _loads(task["settings_json"]).get("live_queue_enabled"):
                raise ConflictError("Windows can only be added to a live queue task")
            if task["status"] not in {"running", "waiting_network", "paused"}:
                raise ConflictError(
                    "Task is not accepting additional windows",
                    details={"status": task["status"]},
                )
            existing = {
                row["profile_id"]
                for row in connection.execute(
                    "SELECT profile_id FROM task_windows WHERE task_id=?", (task_id,)
                ).fetchall()
            }
            repeated = next((item for item in normalized if item in existing), None)
            if repeated:
                raise ConflictError(
                    "BitBrowser window is already assigned to this task",
                    details={"profile_id": repeated, "entity_id": task_id},
                )
            max_order = connection.execute(
                "SELECT COALESCE(MAX(queue_order), 0) FROM task_windows WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
            for offset, profile_id in enumerate(normalized, start=1):
                connection.execute(
                    "INSERT INTO task_windows(task_id, profile_id, queue_order) VALUES(?, ?, ?)",
                    (task_id, profile_id, max_order + offset),
                )
            connection.execute(
                "UPDATE tasks SET updated_at=?, version=version+1 WHERE id=?",
                (now, task_id),
            )
            _event(
                connection,
                owner_user_id,
                "task",
                task_id,
                "task.windows_added",
                {"window_ids": normalized},
            )
        return {
            "added": len(normalized),
            "window_ids": normalized,
            "task": self.get_task(owner_user_id, task_id),
        }

    def remove_task_windows(
        self, owner_user_id: str, task_id: str, window_ids: list[str]
    ) -> None:
        """Rollback only windows appended by a failed in-memory scheduling step."""
        normalized = list(
            dict.fromkeys(str(item).strip() for item in window_ids if str(item).strip())
        )
        if not normalized:
            return
        placeholders = ",".join("?" for _ in normalized)
        now = isoformat()
        with self.database.write() as connection:
            self._owned_task(connection, owner_user_id, task_id)
            connection.execute(
                f"DELETE FROM task_windows WHERE task_id=? AND profile_id IN ({placeholders})",
                (task_id, *normalized),
            )
            connection.execute(
                "UPDATE tasks SET updated_at=?, version=version+1 WHERE id=?",
                (now, task_id),
            )

    def recheck_task_source(
        self, owner_user_id: str, task_id: str, target_id: str, mode: str,
        *, profile_id: str, lease_token: str, live_parent: bool = False,
        queue_only: bool = False, pending_requested_at: str | None = None,
        recover_pending: bool = False,
    ) -> dict[str, Any]:
        """Explicitly invalidate one settled source cursor, never its saved rows.

        The manager must settle the old worker, or reserve a live parent under
        its pipeline checkpoint lock. Live navigation waits for parent activity
        to join, without moving child or lease ownership. Lease verification and
        durable changes share a transaction, including the completed-generation
        exception; a rejected/repeated request cannot partially rewind progress.
        """
        if mode not in {"followers", "following"}:
            raise ValidationError("只能复查 Instagram 粉丝或关注来源")
        now = isoformat()
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            if collection_platform(_loads(task["settings_json"])) != "instagram":
                raise ValidationError("该复查操作仅适用于 Instagram 来源")
            self._guard_target_removal(owner_user_id, target_id)
            target = self._owned_candidate_target(connection, owner_user_id, task_id, target_id, mode)
            if connection.execute(
                "SELECT 1 FROM task_target_list_dismissals WHERE target_id=? AND owner_user_id=?",
                (target_id, owner_user_id),
            ).fetchone():
                raise ConflictError("该采集任务卡已删除，不能再次复查",
                                    details={"reason": "collection_card_dismissed", "target_id": target_id})
            if target["current_stage"] == "deleted_archived":
                raise ConflictError("已删除的目标不能复查")
            if live_parent:
                if task["status"] not in {"running", "paused", "waiting_network"}:
                    raise ConflictError("原采集任务已结束，未重置来源")
                if target["status"] != "running" or target["current_window_id"] != profile_id:
                    raise ConflictError("原采集窗口已变化，未重置来源")
            elif target["status"] in {"running", "waiting_network"}:
                raise ConflictError("当前来源尚未到安全复查点，请稍后再试")
            self._guard_collection_dispatch(connection, owner_user_id, target["username_norm"])
            allowed = _loads(target["allowed_window_ids_json"] or "[]")
            if (allowed and profile_id not in allowed) or not connection.execute(
                "SELECT 1 FROM task_windows WHERE task_id=? AND profile_id=?", (task_id, profile_id)
            ).fetchone():
                raise ConflictError("请选择该目标允许使用的任务窗口")
            lease = connection.execute(
                "SELECT 1 FROM browser_operation_leases WHERE profile_id=? AND lease_token=? "
                "AND owner_user_id=? AND operation_type='collection' AND entity_id=?",
                (profile_id, lease_token, owner_user_id, task_id),
            ).fetchone()
            if not lease:
                raise ConflictError("窗口占用已变化，请刷新后再复查")
            existing_request = connection.execute(
                "SELECT * FROM task_source_rechecks WHERE target_id=?", (target_id,),
            ).fetchone()
            activating = bool(pending_requested_at)
            if activating:
                if (not live_parent or not existing_request
                        or existing_request["state"] != "prepared" or existing_request["mode"] != mode
                        or existing_request["requested_at"] != pending_requested_at):
                    raise ConflictError("复查请求已变化，未重置来源")
                same_owner = (existing_request["request_profile_id"] == profile_id
                              and existing_request["request_lease_token"] == lease_token)
                if not same_owner:
                    # A newly started/retried owner may adopt the same persisted
                    # generation only after the previous exact lease is gone.
                    old_lease = connection.execute(
                        "SELECT 1 FROM browser_operation_leases WHERE profile_id=? AND lease_token=?",
                        (existing_request["request_profile_id"], existing_request["request_lease_token"]),
                    ).fetchone()
                    if not recover_pending or old_lease:
                        raise ConflictError("复查原窗口仍被占用，未重置来源")
                now = existing_request["requested_at"]
            elif existing_request and existing_request["state"] in {"prepared", "active"}:
                if (queue_only and existing_request["mode"] == mode
                        and existing_request["request_profile_id"] == profile_id
                        and existing_request["request_lease_token"] == lease_token):
                    return {**_target_dict(target), "source_recheck_requested_at": existing_request["requested_at"],
                            "waiting_for_safe_point": existing_request["state"] == "prepared"}
                raise ConflictError("该目标的复查已经安排，请继续现有复查，不要重复重置")
            # A returned/competing generation belongs to its durable queue. This
            # command must never take that claim away from a different worker.
            if connection.execute(
                "SELECT 1 FROM split_candidates WHERE owner_user_id=? AND username_norm=? "
                "AND candidate_kind='manual' AND ((queue_state='queued' AND queued_target_id IS NULL) "
                "OR (queue_state='claimed' AND queued_target_id!=?)) LIMIT 1",
                (owner_user_id, target["username_norm"], target_id),
            ).fetchone():
                raise ConflictError("该目标已在等待列表或由其他任务领取，请先处理原队列")
            from .collection_coverage import coverage_for_targets
            progress = self._mode_progress_for_targets(connection, [target_id])
            coverage = coverage_for_targets(connection, progress).get(target_id, {}).get(mode, {})
            if not activating and not queue_only and (not coverage.get("discovery_finished") or not (coverage.get("unobserved_count") or 0) > 0):
                raise ConflictError("该来源没有已确认的未发现差额，或复查已开始；无需重复复查")
            connection.execute(
                "INSERT INTO task_source_rechecks(target_id,mode,state,requested_at,completed_at,request_profile_id,request_lease_token) "
                "VALUES(?,?,'prepared',?,NULL,?,?) ON CONFLICT(target_id) DO UPDATE SET "
                "mode=excluded.mode,state='prepared',requested_at=excluded.requested_at,completed_at=NULL,"
                "request_profile_id=excluded.request_profile_id,request_lease_token=excluded.request_lease_token",
                (target_id, mode, now, profile_id, lease_token),
            )
            if queue_only:
                if not live_parent:
                    raise ValidationError("仅正在运行的父页可等待安全复查点")
                connection.execute("UPDATE tasks SET updated_at=?,version=version+1 WHERE id=?", (now, task_id))
                _event(connection, owner_user_id, "task_target", target_id, "task_source.recheck_queued",
                       {"task_id": task_id, "mode": mode, "saved_data_preserved": True})
                return {**_target_dict(target), "source_recheck_requested_at": now, "waiting_for_safe_point": True}
            # An explicit cursor rewind is not proof that a previously visible
            # unconfirmed identity was processed. Preserve that obligation.
            previous_checkpoint = connection.execute(
                "SELECT cursor_json FROM task_checkpoints WHERE target_id=? AND mode=?", (target_id, mode),
            ).fetchone()
            pending_relation_usernames = _pending_relation_cursor_names(previous_checkpoint["cursor_json"]) if previous_checkpoint else []
            previous_cursor = _loads(previous_checkpoint["cursor_json"]) if previous_checkpoint else {}
            automatic_budget = False
            for _ in range(6):
                if not isinstance(previous_cursor, dict):
                    break
                automatic_budget = automatic_budget or previous_cursor.get("automatic_gap_recheck_started") is True
                previous_cursor = previous_cursor.get("resume_cursor")
            # Counters JSON deliberately remains byte-for-byte unchanged. It
            # contains authoritative header counts, including wrapped evidence.
            connection.execute(
                "UPDATE task_checkpoints SET stage='source_recheck_requested', cursor_json=?, "
                "recoverable=1,updated_at=? WHERE task_id=? AND target_id=? AND mode=?",
                (_json({"candidate_spool_version": 1, "candidate_spool_complete": False,
                        "candidate_spool_natural_end": False, "source_recheck_requested": True,
                        "pending_relation_usernames": pending_relation_usernames,
                        **({"automatic_gap_recheck_started": True} if automatic_budget else {})}),
                 now, task_id, target_id, mode),
            )
            if live_parent:
                # The live parent/children retain their exact target and lease.
                # Only the source cursor is rewound; no split claim is requeued.
                connection.execute("UPDATE task_source_rechecks SET state='active' WHERE target_id=?", (target_id,))
                connection.execute("UPDATE tasks SET updated_at=?,version=version+1 WHERE id=?", (now, task_id))
                _event(connection, owner_user_id, "task_target", target_id, "task_source.recheck_requested",
                       {"task_id": task_id, "mode": mode, "saved_data_preserved": True, "live_parent": True})
                return {**_target_dict(target), "source_recheck_requested_at": now, "waiting_for_safe_point": False}
            connection.execute(
                "UPDATE task_target_recovery_controls SET state='requeued',updated_at=? "
                "WHERE target_id=? AND owner_user_id=?", (now, target_id, owner_user_id),
            )
            connection.execute(
                "UPDATE split_candidates SET candidate_kind='manual',source_status='requeued_by_user', "
                "queue_state='queued',queued_task_id=?,queued_target_id=?,updated_at=? "
                "WHERE owner_user_id=? AND source_target_id=? AND candidate_kind='failure'",
                (task_id, target_id, now, owner_user_id, target_id),
            )
            connection.execute(
                "UPDATE task_targets SET status='pending',current_stage='source_recheck_requested', "
                "current_window_id=NULL,preferred_window_id=?,last_error=NULL,updated_at=? WHERE id=?",
                (profile_id, now, target_id),
            )
            # Consume the one completed->pending authorization before commit.
            # No other transaction can observe a reusable 'prepared' generation.
            connection.execute("UPDATE task_source_rechecks SET state='active' WHERE target_id=?", (target_id,))
            connection.execute(
                "UPDATE tasks SET status=CASE WHEN status IN ('completed','failed','stopped','recoverable') "
                "THEN 'queued' ELSE status END,finished_at=NULL,last_error=NULL,updated_at=?,version=version+1 "
                "WHERE id=?", (now, task_id),
            )
            _event(connection, owner_user_id, "task_target", target_id, "task_source.recheck_requested",
                   {"task_id": task_id, "mode": mode, "saved_data_preserved": True})
            row = connection.execute("SELECT * FROM task_targets WHERE id=?", (target_id,)).fetchone()
        return _target_dict(row)

    def restore_pending_source_recheck(
        self, owner_user_id: str, task_id: str, target_id: str, mode: str,
        *, profile_id: str, lease_token: str,
    ) -> dict[str, Any] | None:
        """Adopt saved intent at a new owned entry; never start or revive work."""
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            target = self._owned_candidate_target(connection, owner_user_id, task_id, target_id, mode)
            request = connection.execute(
                "SELECT * FROM task_source_rechecks WHERE target_id=? AND mode=? AND state IN ('prepared','active')",
                (target_id, mode),
            ).fetchone()
            if request is None:
                return None
            if request["state"] == "active":
                lease = connection.execute(
                    "SELECT 1 FROM browser_operation_leases WHERE profile_id=? AND lease_token=? "
                    "AND owner_user_id=? AND operation_type='collection' AND entity_id=?",
                    (profile_id, lease_token, owner_user_id, task_id),
                ).fetchone()
                same_owner = request["request_profile_id"] == profile_id and request["request_lease_token"] == lease_token
                old_lease = connection.execute(
                    "SELECT 1 FROM browser_operation_leases WHERE profile_id=? AND lease_token=?",
                    (request["request_profile_id"], request["request_lease_token"]),
                ).fetchone()
                if (not lease or (not same_owner and old_lease) or target["status"] != "running"
                        or target["current_window_id"] != profile_id
                        or task["status"] not in {"running", "paused", "waiting_network"}):
                    raise ConflictError("原复查窗口占用已变化，未恢复来源")
                connection.execute(
                    "UPDATE task_source_rechecks SET request_profile_id=?,request_lease_token=? "
                    "WHERE target_id=? AND requested_at=? AND state='active'",
                    (profile_id, lease_token, target_id, request["requested_at"]),
                )
                return {**_target_dict(target), "source_recheck_requested_at": request["requested_at"],
                        "waiting_for_safe_point": False}
        # Rebind a prepared generation without rewinding unfinished initial/auto
        # work. The owned producer will activate it after that work has joined.
        return self.recheck_task_source(owner_user_id, task_id, target_id, mode,
            profile_id=profile_id, lease_token=lease_token, live_parent=True, queue_only=True,
            pending_requested_at=request["requested_at"], recover_pending=True)

    def finish_live_source_recheck(
        self, owner_user_id: str, task_id: str, target_id: str, mode: str,
        *, profile_id: str, lease_token: str, requested_at: str,
    ) -> None:
        """Retire only this source pass; children and target completion stay owned."""
        with self.database.write() as connection:
            self._owned_task(connection, owner_user_id, task_id)
            target = self._owned_candidate_target(connection, owner_user_id, task_id, target_id, mode)
            lease = connection.execute(
                "SELECT 1 FROM browser_operation_leases WHERE profile_id=? AND lease_token=? "
                "AND owner_user_id=? AND operation_type='collection' AND entity_id=?",
                (profile_id, lease_token, owner_user_id, task_id),
            ).fetchone()
            if not lease or target["current_window_id"] != profile_id or target["status"] != "running":
                raise ConflictError("复查原窗口占用已变化，未确认旧复查完成")
            checkpoint = connection.execute(
                "SELECT cursor_json FROM task_checkpoints WHERE target_id=? AND mode=?", (target_id, mode),
            ).fetchone()
            cursor = _loads(checkpoint["cursor_json"]) if checkpoint else {}
            if not (cursor.get("candidate_spool_complete") is True and cursor.get("candidate_spool_natural_end") is True):
                raise ConflictError("复查尚未确认名单结尾")
            connection.execute(
                "UPDATE task_source_rechecks SET state='completed',completed_at=? "
                "WHERE target_id=? AND mode=? AND requested_at=? AND state='active'",
                (isoformat(), target_id, mode, requested_at),
            )

    def active_source_recheck_mode(self, owner_user_id: str, task_id: str, target_id: str) -> str | None:
        with self.database.read() as connection:
            self._owned_task(connection, owner_user_id, task_id)
            row = connection.execute(
                "SELECT recheck.mode FROM task_source_rechecks recheck JOIN task_targets target "
                "ON target.id=recheck.target_id WHERE target.id=? AND target.task_id=? AND recheck.state='active'",
                (target_id, task_id),
            ).fetchone()
        return row["mode"] if row else None

    def source_recheck_completion_status(self, owner_user_id: str, task_id: str, target_id: str, mode: str) -> str:
        """Finish only the requested source; leave other modes for explicit resume."""
        with self.database.read() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            unfinished = False
            for other_mode in _loads(task["modes_json"]):
                if other_mode == mode:
                    continue
                checkpoint = connection.execute(
                    "SELECT stage,cursor_json FROM task_checkpoints WHERE target_id=? AND mode=?",
                    (target_id, other_mode),
                ).fetchone()
                if checkpoint is None or checkpoint["stage"] != "mode_completed":
                    unfinished = True
                    break
                cursor = _loads(checkpoint["cursor_json"])
                if other_mode in {"followers", "following"} and not (
                    cursor.get("candidate_spool_complete") is True and cursor.get("candidate_spool_natural_end") is True
                ):
                    unfinished = True
                    break
            if connection.execute("SELECT 1 FROM task_mode_candidates WHERE target_id=? AND state='pending' LIMIT 1",
                                  (target_id,)).fetchone():
                unfinished = True
        return "recoverable" if unfinished else "completed"

    def retry_task_target(
        self, owner_user_id: str, task_id: str, target_id: str
    ) -> dict[str, Any]:
        """Put one unfinished target back into the durable queue.

        This is a *resume* operation, not a destructive restart.  Checkpoints and
        already-persisted results intentionally remain in place: the execution
        manager skips ``mode_completed`` checkpoints and, for an interrupted mode,
        uses durable per-mode results/global dedupe to avoid screening the same
        account twice.
        """
        now = isoformat()
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            self._guard_target_removal(owner_user_id, target_id)
            target = connection.execute(
                "SELECT * FROM task_targets WHERE id=? AND task_id=?",
                (target_id, task_id),
            ).fetchone()
            if target is None:
                raise NotFoundError("Task target not found")
            self._guard_collection_dispatch(connection, owner_user_id, target["username_norm"])
            if target["current_stage"] == "deleted_archived":
                raise ConflictError(
                    "该执行记录已直接删除，无法恢复；已有分裂记录的账号不能重新加入",
                    details={"target_id": target_id},
                )
            if target["status"] in {"running", "waiting_network"}:
                raise ConflictError("The target is already running")
            if target["status"] == "completed":
                raise ConflictError(
                    "该分裂号已采集完成，不能恢复或再次加入分裂号",
                    details={"target_id": target_id, "status": target["status"]},
                )
            # A returned failure is owned by the durable waiting queue.  Direct
            # retry must never publish a second in-memory payload for that target:
            # another selected window can claim the waiting row at the same time.
            waiting = connection.execute(
                """
                SELECT * FROM split_candidates
                WHERE owner_user_id=? AND candidate_kind='manual'
                  AND source_task_id=? AND source_target_id=?
                  AND queue_state='queued' AND queued_target_id IS NULL
                """,
                (owner_user_id, task_id, target_id),
            ).fetchone()
            if waiting is not None:
                if _loads(task["settings_json"]).get("live_queue_enabled"):
                    raise ConflictError(
                        "该失败项已返回等待列表，请通过原任务窗口领取，不要单独重试旧目标",
                        details={"target_id": target_id, "reason": "split_candidate_waiting"},
                    )
                if waiting["dispatch_locked"] or waiting["manual_category_override"] == "completed":
                    raise ConflictError(
                        "该失败项已返回等待列表且当前不可领取",
                        details={"target_id": target_id, "reason": "split_candidate_waiting"},
                    )
                allowed_windows = [
                    row["profile_id"] for row in connection.execute(
                        "SELECT profile_id FROM split_candidate_window_affinity "
                        "WHERE candidate_id=? ORDER BY queue_order", (waiting["id"],)
                    ).fetchall()
                ]
                if allowed_windows and not connection.execute(
                    "SELECT 1 FROM task_windows WHERE task_id=? AND profile_id IN ("
                    + ",".join("?" for _ in allowed_windows) + ") LIMIT 1",
                    (task_id, *allowed_windows),
                ).fetchone():
                    raise ConflictError(
                        "等待目标指定的窗口不属于原任务，请选择指定窗口继续",
                        details={"target_id": target_id, "reason": "split_candidate_window_mismatch"},
                    )
                rebound = connection.execute(
                    """
                    UPDATE split_candidates
                    SET queue_state='claimed', queued_task_id=?, queued_target_id=?,
                        claimed_at=COALESCE(claimed_at, ?),
                        source_status='claimed_generation', updated_at=?
                    WHERE id=? AND owner_user_id=? AND candidate_kind='manual'
                      AND source_target_id=? AND queue_state='queued'
                      AND queued_target_id IS NULL
                    """,
                    (task_id, target_id, now, now, waiting["id"], owner_user_id, target_id),
                )
                if rebound.rowcount != 1:
                    raise ConflictError("Returned failure changed while retrying")
                if allowed_windows:
                    connection.execute(
                        "UPDATE task_targets SET allowed_window_ids_json=? WHERE id=?",
                        (_json(allowed_windows), target_id),
                    )
            competing = connection.execute(
                """
                SELECT 1 FROM split_candidates candidate
                JOIN task_targets active ON active.id=candidate.queued_target_id
                WHERE candidate.owner_user_id=? AND candidate.candidate_kind='manual'
                  AND candidate.username_norm=? AND candidate.queue_state='claimed'
                  AND candidate.queued_target_id!=?
                  AND active.status IN ('pending', 'running', 'waiting_network')
                LIMIT 1
                """,
                (owner_user_id, target["username_norm"], target_id),
            ).fetchone()
            if competing is not None:
                raise ConflictError(
                    "该账号已由另一个任务领取，不能再次继续旧目标",
                    details={"target_id": target_id, "reason": "split_target_already_owned"},
                )
            # This endpoint is an explicit user retry. Resolve the matching failure
            # gate in the same transaction so the in-memory enqueue cannot race a
            # still-visible failure inbox row.
            connection.execute(
                """
                UPDATE split_candidates
                SET candidate_kind='manual', source_status='requeued_by_user',
                    queue_state='queued', queued_task_id=?, queued_target_id=?,
                    queued_at=COALESCE(queued_at, ?), updated_at=?
                WHERE owner_user_id=? AND source_target_id=?
                  AND candidate_kind='failure'
                """,
                (task_id, target_id, now, now, owner_user_id, target_id),
            )
            connection.execute(
                """
                UPDATE task_target_recovery_controls
                SET state='requeued', updated_at=?
                WHERE target_id=? AND owner_user_id=?
                """,
                (now, target_id, owner_user_id),
            )
            connection.execute(
                "UPDATE task_source_rechecks SET state='resuming' WHERE target_id=? AND state='completed'", (target_id,)
            )
            connection.execute(
                "UPDATE task_targets SET status='pending', current_window_id=NULL, preferred_window_id=NULL, "
                "last_error=NULL, updated_at=? WHERE id=?",
                (now, target_id),
            )
            if task["status"] in {"completed", "failed", "stopped", "recoverable"}:
                connection.execute(
                    "UPDATE tasks SET status='queued', finished_at=NULL, last_error=NULL, updated_at=?, version=version+1 WHERE id=?",
                    (now, task_id),
                )
            else:
                updated = connection.execute(
                    "UPDATE tasks SET updated_at=?, version=version+1 WHERE id=?",
                    (now, task_id),
                )
            _event(
                connection,
                owner_user_id,
                "task_target",
                target_id,
                "task_target.requeued",
                {"task_id": task_id, "checkpoint_preserved": True},
            )
            row = connection.execute(
                "SELECT * FROM task_targets WHERE id=?", (target_id,)
            ).fetchone()
        return _target_dict(row)

    def delete_task_target(
        self, owner_user_id: str, task_id: str, target_id: str, *, dismiss: bool = False
    ) -> None:
        """Archive one unfinished target for manual recovery without data loss.

        Older releases physically deleted the row and cascaded its checkpoint,
        candidate spool, and saved results.  The task-page delete action now means
        "remove from execution": the target becomes stopped and its failure
        generation is retained until the user requeues or dismisses it. Direct
        deletion atomically tombstones that generation and removes only its queue
        marker; checkpoints, saved accounts, history and dedupe stay intact.
        """
        with self.database.write() as connection:
            self._owned_task(connection, owner_user_id, task_id)
            target = connection.execute(
                "SELECT * FROM task_targets WHERE id=? AND task_id=?",
                (target_id, task_id),
            ).fetchone()
            if target is None:
                raise NotFoundError("Task target not found")
            if target["current_stage"] == "deleted_archived" and not dismiss:
                raise ConflictError("该执行记录已直接删除，不能由旧请求退回等待")
            if target["status"] in {"running", "waiting_network"}:
                raise ConflictError("Stop the running target before deleting it")
            if target["status"] == "completed":
                raise ConflictError(
                    "Completed target history is permanent and cannot be deleted",
                    details={"target_id": target_id, "status": target["status"]},
                )
            now = isoformat()
            connection.execute(
                """
                UPDATE task_targets
                SET status='stopped', current_window_id=NULL,
                    preferred_window_id=NULL,
                    current_stage=?,
                    last_error=COALESCE(last_error, 'removed_from_collection_queue'),
                    updated_at=?
                WHERE id=?
                """,
                ("deleted_archived" if dismiss else "interrupted_recoverable", now, target_id),
            )
            if dismiss:
                # Status triggers create the recovery generation. Dismiss it in
                # this same SQLite transaction: no reader can see a failure queue
                # entry between removal and its permanent suppression.
                connection.execute(
                    "UPDATE task_target_recovery_controls SET state='dismissed', updated_at=? "
                    "WHERE owner_user_id=? AND target_id=?",
                    (now, owner_user_id, target_id),
                )
                connection.execute(
                    """
                    DELETE FROM split_candidates
                    WHERE owner_user_id=? AND candidate_kind IN ('manual', 'failure')
                      AND (
                        queued_target_id=?
                        OR (source_target_id=? AND queued_target_id IS NULL)
                      )
                    """,
                    (owner_user_id, target_id, target_id),
                )
            _event(
                connection,
                owner_user_id,
                "task_target",
                target_id,
                "task_target.deleted_from_execution" if dismiss else "task_target.archived_for_recovery",
                {
                    "task_id": task_id,
                    "status": target["status"],
                    "history_preserved": True,
                    "direct_delete": dismiss,
                },
            )

    def requeue_removed_task_target(
        self,
        owner_user_id: str,
        target_id: str,
        *,
        removed_profile_id: str | None = None,
        _removal_token: object | None = None,
    ) -> dict[str, Any]:
        """Return an archived execution target to the manual waiting queue.

        ``delete_task_target`` creates the recovery generation through the database
        trigger. Resolving that exact generation here keeps delete-and-return a
        single backend operation and prevents a stale UI request from colliding with
        the failure duplicate guard.  When the removed execution window belonged to
        a hard affinity pool, drop only that window; an exhausted pool deliberately
        becomes automatic dispatch instead of leaving an unclaimable queue row.
        """
        normalized_removed_profile_id = str(removed_profile_id or "").strip()
        with self.database.write() as connection:
            self._guard_target_removal(owner_user_id, target_id, _removal_token)
            recovery = connection.execute(
                """
                SELECT recovery.candidate_id, recovery.state,
                       target.allowed_window_ids_json, target.current_stage
                FROM task_target_recovery_controls recovery
                LEFT JOIN task_targets target ON target.id=recovery.target_id
                WHERE recovery.owner_user_id=? AND recovery.target_id=?
                """,
                (owner_user_id, target_id),
            ).fetchone()
            if recovery is None:
                raise NotFoundError("Recoverable task target not found")

            if recovery["current_stage"] == "deleted_archived":
                raise ConflictError("该执行记录已直接删除，不能由旧请求退回等待")
            candidate_id = recovery["candidate_id"]
            if recovery["state"] == "dismissed":
                # ``delete_failed_split_candidate`` intentionally tombstones one
                # failure generation.  A later click on "delete and return to
                # waiting" is a new, explicit operator decision and must be able
                # to supersede that tombstone.  Rotate the generation id so a
                # delayed request carrying the dismissed id cannot affect the new
                # waiting generation.
                candidate_id = str(uuid.uuid4())
                updated = connection.execute(
                    """
                    UPDATE task_target_recovery_controls
                    SET candidate_id=?, state='pending', updated_at=?
                    WHERE owner_user_id=? AND target_id=? AND state='dismissed'
                    """,
                    (candidate_id, isoformat(), owner_user_id, target_id),
                )
                if updated.rowcount != 1:
                    raise ConflictError(
                        "Recoverable task target changed while it was being returned"
                    )
        allowed_window_ids: list[str] | None = None
        raw_allowed_window_ids = recovery["allowed_window_ids_json"]
        if raw_allowed_window_ids is not None:
            try:
                parsed_allowed_window_ids = _loads(raw_allowed_window_ids)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_allowed_window_ids = None
            if isinstance(parsed_allowed_window_ids, list) and all(
                isinstance(profile_id, str)
                for profile_id in parsed_allowed_window_ids
            ):
                allowed_window_ids = _normalize_allowed_window_ids(
                    parsed_allowed_window_ids
                )
                if normalized_removed_profile_id:
                    allowed_window_ids = [
                        profile_id
                        for profile_id in allowed_window_ids
                        if profile_id != normalized_removed_profile_id
                    ]
        return self.requeue_split_candidate(
            owner_user_id,
            candidate_id,
            allowed_window_ids=allowed_window_ids,
            _removal_token=_removal_token,
        )

    def archive_completed_task_target_from_list(
        self, owner_user_id: str, task_id: str, target_id: str
    ) -> None:
        """Hide a completed execution card while retaining results and dedupe history."""
        now = isoformat()
        with self.database.write() as connection:
            self._owned_task(connection, owner_user_id, task_id)
            updated = connection.execute(
                """
                UPDATE task_targets
                SET current_window_id=NULL, preferred_window_id=NULL,
                    current_stage='completed_archived', updated_at=?
                WHERE id=? AND task_id=? AND status='completed'
                """,
                (now, target_id, task_id),
            )
            if updated.rowcount != 1:
                raise ConflictError("Only a completed target can be archived")
            _event(
                connection, owner_user_id, "task_target", target_id,
                "task_target.completed_card_archived",
                {"task_id": task_id, "history_preserved": True},
            )

    def set_manual_assignments(
        self,
        owner_user_id: str,
        task_id: str,
        assignments: dict[str, str],
    ) -> dict[str, Any]:
        """Assign one initial target to each selected window for manual scheduling."""
        normalized: list[tuple[str, str]] = []
        seen_targets: set[str] = set()
        for raw_profile_id, raw_target in assignments.items():
            profile_id = str(raw_profile_id).strip()
            username_norm, _ = normalize_instagram_username(raw_target)
            if not profile_id or len(profile_id) > 128:
                raise ValidationError("Invalid BitBrowser profile id")
            if username_norm in seen_targets:
                raise ValidationError("A manual target may only be assigned to one window")
            seen_targets.add(username_norm)
            normalized.append((profile_id, username_norm))
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            if task["status"] == "running":
                raise ConflictError("Pause the task before changing manual assignments")
            if task["assignment_mode"] != "manual":
                raise ValidationError("Task is not in manual assignment mode")
            valid_windows = {
                row["profile_id"]
                for row in connection.execute(
                    "SELECT profile_id FROM task_windows WHERE task_id=?", (task_id,)
                ).fetchall()
            }
            connection.execute(
                "UPDATE task_targets SET preferred_window_id=NULL WHERE task_id=?", (task_id,)
            )
            for profile_id, username_norm in normalized:
                if profile_id not in valid_windows:
                    raise ValidationError("Manual assignment references an unselected window")
                cursor = connection.execute(
                    """
                    UPDATE task_targets SET preferred_window_id=?
                    WHERE task_id=? AND username_norm=?
                    """,
                    (profile_id, task_id, username_norm),
                )
                if cursor.rowcount != 1:
                    raise ValidationError("Manual assignment references an unknown target")
            connection.execute(
                "UPDATE tasks SET updated_at=?, version=version+1 WHERE id=?",
                (isoformat(), task_id),
            )
        return self.get_task(owner_user_id, task_id)

    def list_tasks(self, owner_user_id: str, *, platform: str | None = None) -> list[dict[str, Any]]:
        platform_scope = task_platform_sql(platform, 'task.settings_json')
        with self.database.read() as connection:
            rows = connection.execute(
                f"""SELECT task.* FROM tasks task
                   WHERE task.owner_user_id=? {platform_scope}
                     AND NOT EXISTS(
                       SELECT 1 FROM task_list_dismissals dismissal
                       WHERE dismissal.task_id=task.id
                         AND dismissal.owner_user_id=task.owner_user_id
                     )
                   ORDER BY task.updated_at DESC""", (owner_user_id,)
            ).fetchall()
        return [_task_dict(row) for row in rows]

    def list_tasks_with_details(
        self,
        owner_user_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
        platform: str | None = None,
        detail_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return a task page with targets/windows using bounded bulk queries.

        The desktop polls this view frequently.  The previous route fetched the task
        list and then opened two more SQLite connections per task via ``get_task``,
        which became increasingly expensive as history accumulated.
        """

        platform_scope = task_platform_sql(platform, 'task.settings_json')
        with self.database.read() as connection:
            if detail_limit is not None:
                # Priority IDs, their payloads and the sentinel must describe
                # one committed view even if a source completes between reads.
                connection.execute("BEGIN")
            bounded_offset = max(0, int(offset))
            if limit is None and bounded_offset == 0:
                task_rows = connection.execute(
                    f"""
                    SELECT task.* FROM tasks task WHERE task.owner_user_id=? {platform_scope}
                      AND NOT EXISTS(
                        SELECT 1 FROM task_list_dismissals dismissal
                        WHERE dismissal.task_id=task.id
                          AND dismissal.owner_user_id=task.owner_user_id
                      )
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (owner_user_id,),
                ).fetchall()
            elif limit is None:
                task_rows = connection.execute(
                    f"""
                    SELECT task.* FROM tasks task WHERE task.owner_user_id=? {platform_scope}
                      AND NOT EXISTS(
                        SELECT 1 FROM task_list_dismissals dismissal
                        WHERE dismissal.task_id=task.id
                          AND dismissal.owner_user_id=task.owner_user_id
                      )
                    ORDER BY updated_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                    """,
                    (owner_user_id, bounded_offset),
                ).fetchall()
            else:
                bounded_limit = max(1, min(int(limit), 5001))
                task_rows = connection.execute(
                    f"""
                    SELECT task.* FROM tasks task
                    WHERE task.owner_user_id=? {platform_scope}
                      AND NOT EXISTS(
                        SELECT 1 FROM task_list_dismissals dismissal
                        WHERE dismissal.task_id=task.id
                          AND dismissal.owner_user_id=task.owner_user_id
                      )
                    ORDER BY CASE
                        WHEN status IN ('queued', 'running', 'waiting_network',
                                        'paused', 'recoverable') THEN 0 ELSE 1 END,
                             updated_at DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (owner_user_id, bounded_limit, bounded_offset),
                ).fetchall()
            if not task_rows:
                return []
            task_ids = [row["id"] for row in task_rows]
            placeholders = ",".join("?" for _ in task_ids)
            detail_filter = ""
            detail_parameters: list[Any] = [*task_ids]
            if detail_limit is not None:
                detail_filter = "WHERE detail_rank<=?"
                detail_budget = max(1, min(int(detail_limit), 50001))
                # Share one global detail budget fairly across the outer page.
                # Workbench callers keep detail_budget >= len(task_ids), so this
                # yields at least one row per task without multiplying the budget.
                detail_parameters.append(max(1, detail_budget // len(task_ids)))
            # Seek each selected task's priority index before loading payloads.
            # The extra ID is only a truncation sentinel, never a public count.
            # Lifetime completed sources must not enter a ROW_NUMBER sorter.
            target_truncated: dict[str, bool] = {}
            if detail_limit is not None:
                per_task_budget = max(1, detail_budget // len(task_ids))
                selected_pages = connection.execute(
                    """SELECT selected.value AS task_id,
                           (SELECT json_group_array(id) FROM (
                               SELECT target.id
                               FROM task_targets target
                               WHERE target.task_id=selected.value
                                 AND target.username_norm NOT GLOB 'fb:*'
                               ORDER BY CASE
                                   WHEN target.status IN ('running', 'waiting_network') THEN 0
                                   WHEN target.status IN ('failed', 'recoverable') THEN 1
                                   WHEN target.status IN ('pending', 'paused') THEN 2
                                   ELSE 3 END,
                                   target.queue_order, target.id
                               LIMIT ?
                           )) AS target_ids_json
                       FROM json_each(?) selected""",
                    (per_task_budget + 1, _json(task_ids)),
                ).fetchall()
                selected_ids: list[str] = []
                for page in selected_pages:
                    page_ids = _loads(page["target_ids_json"])
                    target_truncated[page["task_id"]] = len(page_ids) > per_task_budget
                    selected_ids.extend(page_ids[:per_task_budget])
                target_selection = "SELECT value AS id FROM json_each(?)"
                target_parameters: tuple[Any, ...] = (_json(selected_ids),)
            else:
                target_selection = f"""SELECT target.id FROM task_targets target
                    WHERE target.task_id IN ({placeholders})
                      AND target.username_norm NOT GLOB 'fb:*'"""
                target_parameters = tuple(task_ids)
            target_rows = connection.execute(
                f"""
                WITH ranked_targets AS ({target_selection})
                SELECT target.*,
                       dismissal.target_id IS NOT NULL AS collection_list_dismissed,
                       (SELECT 'automatic' FROM task_automatic_completions automatic WHERE automatic.target_id=target.id) AS completion_policy,
                       recheck.mode AS source_recheck_mode,
                       recheck.state AS source_recheck_state,
                       recheck.completed_at AS source_recheck_completed_at,
                       recheck.requested_at AS source_recheck_requested_at,
                           (
                           EXISTS(
                             SELECT 1 FROM task_target_recovery_controls recovery
                             WHERE recovery.owner_user_id=task.owner_user_id
                               AND recovery.target_id=target.id
                               AND recovery.state IN ('pending', 'dismissed')
                           )
                           OR EXISTS(
                             SELECT 1 FROM split_candidates failure
                             WHERE failure.owner_user_id=task.owner_user_id
                               AND failure.source_target_id=target.id
                               AND NOT (
                                 (
                                   failure.queue_state IN ('queued', 'claimed')
                                   AND failure.queued_target_id IS NOT NULL
                                   AND failure.queued_target_id=target.id
                                 )
                                 OR (
                                   failure.candidate_kind='manual'
                                   AND failure.queue_state='queued'
                                   AND failure.queued_target_id IS NULL
                                   AND COALESCE(
                                     json_extract(task.settings_json, '$.live_queue_enabled'), 0
                                   )=0
                                 )
                               )
                           )) AS manual_recovery_required,
                       task.updated_at AS parent_updated_at
                FROM ranked_targets
                JOIN task_targets target ON target.id=ranked_targets.id
                JOIN tasks task ON task.id=target.task_id
                LEFT JOIN task_target_list_dismissals dismissal
                  ON dismissal.target_id=target.id AND dismissal.owner_user_id=task.owner_user_id
                LEFT JOIN task_source_rechecks recheck ON recheck.target_id=target.id
                ORDER BY parent_updated_at DESC, target.queue_order, target.id
                """,
                target_parameters,
            ).fetchall()
            window_totals: dict[str, int] = {}
            if detail_limit is not None:
                window_totals = {
                    row["task_id"]: int(row["count"])
                    for row in connection.execute(
                        f"""
                        SELECT task_id, COUNT(*) AS count
                        FROM task_windows WHERE task_id IN ({placeholders})
                        GROUP BY task_id
                        """,
                        tuple(task_ids),
                    ).fetchall()
                }
            mode_progress = self._mode_progress_for_targets(
                connection, (row["id"] for row in target_rows)
            )
            from .collection_coverage import coverage_for_targets
            mode_coverage = coverage_for_targets(connection, mode_progress)
            window_rows = connection.execute(
                f"""
                WITH ranked_windows AS (
                    SELECT window.*,
                           task.updated_at AS parent_updated_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY window.task_id
                               ORDER BY CASE
                                   WHEN window.status IN ('running', 'waiting_network')
                                   THEN 0
                                   WHEN window.status='paused' THEN 1
                                   WHEN window.status='selected' THEN 2
                                   ELSE 3 END,
                                   window.queue_order, window.profile_id
                           ) AS detail_rank
                    FROM task_windows window
                    JOIN tasks task ON task.id=window.task_id
                    WHERE task.id IN ({placeholders})
                )
                SELECT * FROM ranked_windows
                {detail_filter}
                ORDER BY parent_updated_at DESC, queue_order, profile_id
                """,
                tuple(detail_parameters),
            ).fetchall()
        targets_by_task: dict[str, list[sqlite3.Row]] = {}
        for row in target_rows:
            targets_by_task.setdefault(row["task_id"], []).append(row)
        windows_by_task: dict[str, list[sqlite3.Row]] = {}
        for row in window_rows:
            windows_by_task.setdefault(row["task_id"], []).append(row)
        result: list[dict[str, Any]] = []
        for row in task_rows:
            task = _task_dict(
                row,
                targets_by_task.get(row["id"], ()),
                windows_by_task.get(row["id"], ()),
                mode_progress,
                mode_coverage,
            )
            task["targets_truncated"] = target_truncated.get(row["id"], False)
            task["windows_truncated"] = (
                window_totals.get(row["id"], 0)
                > len(windows_by_task.get(row["id"], ()))
            )
            result.append(task)
        return result

    def count_tasks(self, owner_user_id: str, *, platform: str | None = None) -> int:
        platform_scope = task_platform_sql(platform, 'task.settings_json')
        with self.database.read() as connection:
            return int(
                connection.execute(
                    f"""SELECT COUNT(*) FROM tasks task WHERE task.owner_user_id=? {platform_scope}
                       AND NOT EXISTS(
                         SELECT 1 FROM task_list_dismissals dismissal
                         WHERE dismissal.task_id=task.id
                           AND dismissal.owner_user_id=task.owner_user_id
                       )""",
                    (owner_user_id,),
                ).fetchone()[0]
            )

    def get_task(self, owner_user_id: str, task_id: str) -> dict[str, Any]:
        return self._read_task_snapshot(owner_user_id, task_id)

    def get_task_status(self, owner_user_id: str, task_id: str) -> str:
        """Read an owned task's status without any target/history projection."""
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT status, settings_json FROM tasks WHERE id=? AND owner_user_id=?",
                (task_id, owner_user_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("Task not found")
        stored_task_settings(row["settings_json"])
        return str(row["status"])

    def get_task_live_status(
        self,
        owner_user_id: str,
        task_id: str,
        *,
        current_target_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Return current window targets plus a bounded recent-change overlay.

        A long-lived task can retain many completed sources. The heartbeat must
        not read and resend that whole history, but it must include newly terminal
        targets and every occupied window even when there are more than 200.
        """
        normalized_ids: tuple[str, ...] | None = None
        if current_target_ids is not None:
            if isinstance(current_target_ids, (str, bytes)):
                raise ValidationError("Live target ids must be a collection of ids")
            ids: list[str] = []
            for target_id in current_target_ids:
                if not isinstance(target_id, str) or not target_id.strip() or len(target_id) > 128:
                    raise ValidationError("Invalid live target id")
                ids.append(target_id.strip())
            normalized_ids = tuple(dict.fromkeys(ids))
        return self._read_task_snapshot(
            owner_user_id, task_id,
            recent_target_limit=LIVE_TASK_RECENT_TARGET_LIMIT,
            current_target_ids=normalized_ids,
        )

    def _read_task_snapshot(
        self,
        owner_user_id: str,
        task_id: str,
        *,
        recent_target_limit: int | None = None,
        current_target_ids: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        with self.database.read() as connection:
            if recent_target_limit is not None:
                # Task status, target bindings, and progress must belong to one
                # read snapshot even when a worker completes or claims a source.
                connection.execute("BEGIN")
            task = self._owned_task(connection, owner_user_id, task_id)
            selection = ""
            target_source = "task_targets target"
            parameters: list[Any] = []
            if recent_target_limit is not None:
                binding_filter = ""
                runtime_selection = ""
                parameters.append(task_id)
                if current_target_ids is not None:
                    # Completed archives deliberately retain their former window
                    # for audit. It is not proof that the window still owns them.
                    # Runtime IDs retain terminal cleanup targets; nonterminal
                    # bindings also retain new claims made after runtime sampling.
                    binding_filter = " AND status NOT IN ('completed', 'failed', 'stopped')"
                    runtime_selection = """
                        UNION
                        SELECT target.id FROM json_each(?) requested
                        CROSS JOIN task_targets target
                        WHERE target.id=requested.value AND target.task_id=?
                    """
                    parameters.extend((_json(current_target_ids), task_id))
                selection = f"""
                    WITH selected_target_ids AS (
                        SELECT id FROM task_targets
                        WHERE task_id=? AND current_window_id IS NOT NULL
                          {binding_filter}
                        {runtime_selection}
                        UNION
                        SELECT id FROM (
                            SELECT id FROM task_targets WHERE task_id=? AND username_norm NOT GLOB 'fb:*'
                            ORDER BY updated_at DESC, id DESC LIMIT ?
                        )
                        UNION
                        SELECT target_id FROM (
                            SELECT dismissal.target_id
                            FROM task_target_list_dismissals dismissal
                            WHERE dismissal.owner_user_id=? AND dismissal.task_id=?
                            ORDER BY dismissal.dismissed_at DESC, dismissal.target_id DESC LIMIT ?
                        )
                    )
                """
                target_source = (
                    "selected_target_ids selected "
                    "CROSS JOIN task_targets target ON target.id=selected.id"
                )
                parameters.extend((task_id, recent_target_limit, owner_user_id, task_id, recent_target_limit))
            parameters.extend((
                owner_user_id,
                owner_user_id,
                int(not bool(_loads(task["settings_json"]).get("live_queue_enabled"))),
                owner_user_id,
                task_id,
            ))
            targets = connection.execute(
                f"""
                {selection}
                SELECT target.*,
                       dismissal.target_id IS NOT NULL AS collection_list_dismissed,
                       (SELECT 'automatic' FROM task_automatic_completions automatic WHERE automatic.target_id=target.id) AS completion_policy,
                       recheck.mode AS source_recheck_mode,
                       recheck.state AS source_recheck_state,
                       recheck.completed_at AS source_recheck_completed_at,
                       recheck.requested_at AS source_recheck_requested_at,
                       (
                       EXISTS(
                         SELECT 1 FROM task_target_recovery_controls recovery
                         WHERE recovery.owner_user_id=?
                           AND recovery.target_id=target.id
                           AND recovery.state IN ('pending', 'dismissed')
                       )
                       OR EXISTS(
                         SELECT 1 FROM split_candidates failure
                         WHERE failure.owner_user_id=?
                           AND failure.source_target_id=target.id
                           AND NOT (
                             (
                               failure.queue_state IN ('queued', 'claimed')
                               AND failure.queued_target_id IS NOT NULL
                               AND failure.queued_target_id=target.id
                             )
                             OR (
                               failure.candidate_kind='manual'
                               AND failure.queue_state='queued'
                               AND failure.queued_target_id IS NULL
                               AND ?=1
                             )
                           )
                       )) AS manual_recovery_required
                FROM {target_source}
                LEFT JOIN task_target_list_dismissals dismissal
                  ON dismissal.target_id=target.id AND dismissal.owner_user_id=?
                LEFT JOIN task_source_rechecks recheck ON recheck.target_id=target.id
                WHERE target.task_id=? AND target.username_norm NOT GLOB 'fb:*' ORDER BY target.queue_order
                """,
                tuple(parameters),
            ).fetchall()
            windows = connection.execute(
                "SELECT * FROM task_windows WHERE task_id=? ORDER BY queue_order", (task_id,)
            ).fetchall()
            mode_progress = self._mode_progress_for_targets(
                connection, (row["id"] for row in targets)
            )
            from .collection_coverage import coverage_for_targets
            mode_coverage = coverage_for_targets(connection, mode_progress)
        result = _task_dict(task, targets, windows, mode_progress, mode_coverage)
        if recent_target_limit is not None:
            result["targets_partial"] = True
        return result

    def delete_task(self, owner_user_id: str, task_id: str) -> None:
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            if task["status"] in {"running", "waiting_network", "paused"}:
                raise ConflictError("Stop the task before deleting it")
            completed_count = connection.execute(
                "SELECT COUNT(*) FROM task_targets WHERE task_id=? AND status='completed'",
                (task_id,),
            ).fetchone()[0]
            if completed_count:
                raise ConflictError(
                    "Tasks containing completed history cannot be deleted",
                    details={"completed_targets": completed_count},
                )
            now = isoformat()
            connection.execute(
                "UPDATE task_targets SET status='stopped', current_window_id=NULL, "
                "preferred_window_id=NULL, current_stage=CASE WHEN current_stage='deleted_archived' "
                "THEN 'deleted_archived' ELSE 'interrupted_recoverable' END, "
                "last_error=COALESCE(last_error, 'removed_from_collection_queue'), updated_at=? WHERE task_id=?",
                (now, task_id),
            )
            connection.execute("UPDATE tasks SET status='stopped', updated_at=?, version=version+1 WHERE id=?", (now, task_id))
            connection.execute(
                "INSERT INTO task_list_dismissals(task_id,owner_user_id,dismissed_at) VALUES(?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET dismissed_at=excluded.dismissed_at",
                (task_id, owner_user_id, now),
            )
            _event(
                connection,
                owner_user_id,
                "task",
                task_id,
                "task.archived_for_recovery",
                {"previous_status": task["status"], "history_retained": True},
            )

    def dismiss_completed_task_target(
        self, owner_user_id: str, task_id: str, target_id: str,
    ) -> dict[str, Any]:
        """Hide exactly one completed card; retain all execution/history rows."""
        with self.database.write() as connection:
            self._owned_task(connection, owner_user_id, task_id)
            target = connection.execute(
                "SELECT status FROM task_targets WHERE id=? AND task_id=?", (target_id, task_id),
            ).fetchone()
            if target is None:
                raise NotFoundError("Task target not found")
            if target["status"] != "completed":
                raise ConflictError("只有已完成的采集任务卡可以删除",
                                    details={"target_id": target_id, "status": target["status"]})
            cursor = connection.execute(
                "INSERT INTO task_target_list_dismissals(target_id,task_id,owner_user_id,dismissed_at) "
                "VALUES(?,?,?,?) ON CONFLICT(target_id) DO NOTHING",
                (target_id, task_id, owner_user_id, isoformat()),
            )
            if cursor.rowcount:
                _event(connection, owner_user_id, "task_target", target_id,
                       "task_target.dismissed_from_collection_list",
                       {"task_id": task_id, "history_retained": True})
        return {"task_id": task_id, "target_id": target_id, "collection_list_dismissed": True}

    def dismiss_task_from_list(self, owner_user_id: str, task_id: str) -> None:
        """Hide a terminal task without deleting collection or dedupe history."""
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            if task["status"] in {"running", "waiting_network", "paused", "queued"}:
                raise ConflictError("Pause or stop the task before removing it")
            now = isoformat()
            connection.execute(
                """INSERT INTO task_list_dismissals(task_id, owner_user_id, dismissed_at)
                   VALUES(?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET dismissed_at=excluded.dismissed_at""",
                (task_id, owner_user_id, now),
            )
            _event(
                connection,
                owner_user_id,
                "task",
                task_id,
                "task.dismissed_from_list",
                {"previous_status": task["status"], "history_retained": True},
            )

    def get_history_target_state(
        self, owner_user_id: str, target_id: str, *, platform: str | None = None
    ) -> dict[str, str]:
        """Return only the control fields needed before deleting one history row."""

        platform_scope = task_platform_sql(platform, 'task.settings_json') + username_platform_sql(platform, 'target.username_norm')
        with self.database.read() as connection:
            row = connection.execute(
                f"""
                SELECT target.task_id, target.status AS target_status,
                       task.status AS task_status
                FROM task_targets target
                JOIN tasks task ON task.id=target.task_id
                WHERE target.id=? AND task.owner_user_id=? {platform_scope}
                """,
                (target_id, owner_user_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("History target not found")
        return {
            "task_id": row["task_id"],
            "target_status": row["target_status"],
            "task_status": row["task_status"],
        }

    def delete_history_target(self, owner_user_id: str, target_id: str, *, platform: str | None = None) -> str:
        """Archive unfinished history with its recovery data and identity claims intact."""
        platform_scope = task_platform_sql(platform, 't.settings_json') + username_platform_sql(platform, 'tt.username_norm')
        with self.database.write() as connection:
            row = connection.execute(
                f"""
                SELECT tt.task_id, tt.status AS target_status, t.status AS task_status
                FROM task_targets tt
                JOIN tasks t ON t.id=tt.task_id
                WHERE tt.id=? AND t.owner_user_id=? {platform_scope}
                """,
                (target_id, owner_user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("History target not found")
            if row["target_status"] == "completed":
                raise ConflictError(
                    "Completed history is permanent and cannot be deleted",
                    details={"target_id": target_id},
                )
            if row["target_status"] in {"running", "waiting_network"} or row["task_status"] in {"running", "waiting_network", "paused"}:
                raise ConflictError("Stop the task before deleting a history target")
            task_id = row["task_id"]
            # Retain checkpoints, candidate spool, results and their identity claims.
            # The recovery trigger exposes this archived generation for explicit retry.
            now = isoformat()
            connection.execute(
                "UPDATE task_targets SET status='stopped', current_window_id=NULL, "
                "preferred_window_id=NULL, current_stage=CASE WHEN current_stage='deleted_archived' "
                "THEN 'deleted_archived' ELSE 'interrupted_recoverable' END, "
                "last_error=COALESCE(last_error, 'removed_from_history'), updated_at=? WHERE id=?",
                (now, target_id),
            )
            _event(
                connection,
                owner_user_id,
                "task_target",
                target_id,
                "history_target.archived_for_recovery",
                {"task_id": task_id, "history_retained": True},
            )
        return task_id

    def control_task(
        self,
        owner_user_id: str,
        task_id: str,
        action: str,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        transitions = {
            "start": ({"draft", "queued"}, "running"),
            "pause": ({"running", "waiting_network"}, "paused"),
            "resume": ({"paused", "recoverable"}, "running"),
            "stop": ({"queued", "running", "waiting_network", "paused", "recoverable"}, "stopped"),
            "restart": ({"completed", "failed", "stopped", "recoverable"}, "queued"),
        }
        if action not in transitions:
            raise ValidationError("Unsupported task control action")
        allowed_from, destination = transitions[action]
        now = isoformat()
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            if expected_version is not None and task["version"] != expected_version:
                raise ConflictError(
                    "Task was modified by another operation",
                    details={"expected_version": expected_version, "actual_version": task["version"]},
                )
            if task["status"] not in allowed_from:
                raise InvalidTransitionError(
                    f"Cannot {action} a task in {task['status']} state",
                    details={"status": task["status"], "action": action},
                )
            started_at = task["started_at"]
            finished_at = task["finished_at"]
            restart_increment = 0
            if action in {"start", "resume"}:
                started_at = started_at or now
                finished_at = None
            elif action == "stop":
                finished_at = now
            elif action == "restart":
                restart_increment = 1
                finished_at = None
                # Restart resumes unfinished work only. Completed source history
                # and its checkpoints must never become new collection work.
                connection.execute(
                    "UPDATE task_targets SET status='pending', current_window_id=NULL, "
                    "last_error=NULL, updated_at=? "
                    "WHERE task_id=? AND status!='completed' "
                    "AND COALESCE(current_stage, '')!='deleted_archived'",
                    (now, task_id),
                )
            connection.execute(
                """
                UPDATE tasks
                SET status=?, version=version+1, restart_count=restart_count+?, updated_at=?,
                    started_at=?, finished_at=?
                WHERE id=?
                """,
                (destination, restart_increment, now, started_at, finished_at, task_id),
            )
            _event(
                connection,
                owner_user_id,
                "task",
                task_id,
                f"task.{action}",
                {"from": task["status"], "to": destination},
            )
        return self.get_task(owner_user_id, task_id)

    # Checkpoints --------------------------------------------------------
    def upsert_checkpoint(
        self,
        owner_user_id: str,
        task_id: str,
        target_id: str,
        *,
        mode: str,
        stage: str,
        cursor: dict[str, Any],
        counters: dict[str, Any],
        recoverable: bool = True,
    ) -> dict[str, Any]:
        if mode not in TASK_MODES:
            raise ValidationError("Invalid checkpoint mode")
        if not stage.strip() or len(stage) > 100:
            raise ValidationError("Checkpoint stage is required and may not exceed 100 characters")
        _reject_sensitive_fields(cursor, "cursor")
        _reject_sensitive_fields(counters, "counters")
        now = isoformat()
        error_stages = {
            "waiting_network",
            "interrupted_recoverable",
            "mode_unavailable",
            # The relationship dialog is still being scanned; a stalled page is
            # neither a saved-business result nor proof of network disconnection.
            # Preserve the previous real success timestamp during its backoff.
            "collecting_list",
        }
        checkpoint_error = str(
            counters.get("message")
            or counters.get("reason")
            or stage
        ) if stage in error_stages else None
        checkpoint_id = str(uuid.uuid4())
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            if mode not in _loads(task["modes_json"]):
                raise ValidationError("Checkpoint mode is not enabled for this task")
            target = connection.execute(
                "SELECT id, status, current_stage FROM task_targets WHERE id=? AND task_id=?", (target_id, task_id)
            ).fetchone()
            if target is None:
                raise NotFoundError("Target not found")
            closed_target = (task['status'] == 'completed' or target['status'] == 'completed'
                             or target['current_stage'] == 'completed_archived')
            if target['current_stage'] == 'deleted_archived':
                raise ConflictError('A closed target cannot receive checkpoints')
            if closed_target:
                if _pending_relation_cursor_names(cursor):
                    raise ConflictError('A closed target cannot receive unconfirmed list identities')
                if stage.strip() != 'mode_completed' or recoverable:
                    raise ConflictError('A closed target cannot receive progress checkpoints')
                # Legacy completion callbacks may fill a missing terminal record,
                # but cannot overwrite saved progress or revive the archived card.
                existing = connection.execute(
                    'SELECT * FROM task_checkpoints WHERE target_id=? AND mode=?',
                    (target_id, mode),
                ).fetchone()
                if existing is not None:
                    return self._checkpoint_dict(existing)
            connection.execute(
                """
                INSERT INTO task_checkpoints(
                    id, task_id, target_id, mode, stage, cursor_json, counters_json, recoverable, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id, mode) DO UPDATE SET
                    stage=excluded.stage,
                    cursor_json=excluded.cursor_json,
                    counters_json=excluded.counters_json,
                    recoverable=excluded.recoverable,
                    updated_at=excluded.updated_at
                """,
                (
                    checkpoint_id,
                    task_id,
                    target_id,
                    mode,
                    stage.strip(),
                    _json(cursor),
                    _json(counters),
                    int(recoverable),
                    now,
                ),
            )
            if not closed_target and stage in error_stages:
                connection.execute(
                    """
                    UPDATE task_targets
                    SET current_stage=?, last_error=?, updated_at=?
                    WHERE id=? AND task_id=?
                    """,
                    (stage.strip(), checkpoint_error, now, target_id, task_id),
                )
            elif not closed_target:
                updated = connection.execute(
                    """
                    UPDATE task_targets
                    SET current_stage=?, last_success_at=?, last_error=NULL,
                        updated_at=?
                    WHERE id=? AND task_id=?
                    """,
                    (stage.strip(), now, now, target_id, task_id),
                )
            _event(connection, owner_user_id, "task", task_id, "task.checkpoint_saved", {"target_id": target_id, "mode": mode})
            row = connection.execute(
                "SELECT * FROM task_checkpoints WHERE target_id=? AND mode=?", (target_id, mode)
            ).fetchone()
        return self._checkpoint_dict(row)

    def list_checkpoints(self, owner_user_id: str, task_id: str) -> list[dict[str, Any]]:
        with self.database.read() as connection:
            self._owned_task(connection, owner_user_id, task_id)
            rows = connection.execute(
                "SELECT * FROM task_checkpoints WHERE task_id=? ORDER BY updated_at DESC", (task_id,)
            ).fetchall()
        return [self._checkpoint_dict(row) for row in rows]

    # Durable source candidates -----------------------------------------
    @staticmethod
    def _candidate_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "target_id": row["target_id"],
            "mode": row["mode"],
            "username": row["username_display"],
            "username_norm": row["username_norm"],
            "discovery_order": row["discovery_order"],
            "state": row["state"],
            "discovered_at": row["discovered_at"],
            "processed_at": row["processed_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _candidate_stats_from_connection(
        connection: sqlite3.Connection, target_id: str, mode: str
    ) -> dict[str, int]:
        row = connection.execute(
            """
            SELECT total, pending, recorded, deduped
            FROM task_mode_candidate_counters
            WHERE target_id=? AND mode=?
            """,
            (target_id, mode),
        ).fetchone()
        if row is None:
            return {"total": 0, "pending": 0, "recorded": 0, "deduped": 0}
        return {
            "total": int(row["total"] or 0),
            "pending": int(row["pending"] or 0),
            "recorded": int(row["recorded"] or 0),
            "deduped": int(row["deduped"] or 0),
        }

    def _owned_candidate_target(
        self,
        connection: sqlite3.Connection,
        owner_user_id: str,
        task_id: str,
        target_id: str,
        mode: str,
    ) -> sqlite3.Row:
        if mode not in TASK_MODES:
            raise ValidationError("Invalid candidate source mode")
        task = self._owned_task(connection, owner_user_id, task_id)
        if mode not in _loads(task["modes_json"]):
            raise ValidationError("Candidate source mode is not enabled for this task")
        target = connection.execute(
            "SELECT * FROM task_targets WHERE id=? AND task_id=?",
            (target_id, task_id),
        ).fetchone()
        if target is None:
            raise NotFoundError("Target not found")
        assert_username_platform(None, target["username_norm"])
        return target

    @staticmethod
    def _primary_task_mode_candidate_usernames(
        connection: sqlite3.Connection,
        target_id: str,
        mode: str,
        *,
        username_norms: Iterable[str] | None = None,
    ) -> set[str]:
        """Choose each identity's first pending alias once per repair transaction.

        A per-candidate earlier-alias query scans the growing source repeatedly.
        Scoped repairs expand only the requested identities; a full resume makes
        one pass over this source. The choice stays valid while pending rows only
        become terminal in the same write transaction.
        """
        if username_norms is None:
            selected_accounts_sql = """
                SELECT DISTINCT pending_alias.account_id
                FROM task_mode_candidates pending
                CROSS JOIN instagram_username_aliases pending_alias
                WHERE pending.target_id=? AND pending.mode=? AND pending.state='pending'
                  AND pending_alias.username_norm=pending.username_norm
            """
            parameters: tuple[Any, ...] = (target_id, mode)
        else:
            scoped_usernames = tuple(dict.fromkeys(username_norms))
            if not scoped_usernames:
                return set()
            selected_accounts_sql = """
                SELECT DISTINCT requested_alias.account_id
                FROM json_each(?) requested
                CROSS JOIN instagram_username_aliases requested_alias
                WHERE requested_alias.username_norm=requested.value
            """
            parameters = (_json(scoped_usernames),)
        rows = connection.execute(
            f"""
            WITH selected_accounts AS ({selected_accounts_sql}),
            identity_rows AS (
                SELECT alias.account_id, candidate.discovery_order, candidate.state
                FROM selected_accounts selected
                CROSS JOIN instagram_username_aliases alias
                CROSS JOIN task_mode_candidates candidate
                WHERE alias.account_id=selected.account_id
                  AND candidate.target_id=? AND candidate.mode=?
                  AND candidate.username_norm=alias.username_norm
                  AND candidate.state IN ('pending', 'recorded')
            ),
            identity_summary AS (
                SELECT account_id,
                    MIN(CASE WHEN state='pending' THEN discovery_order END) AS first_pending,
                    MAX(CASE WHEN state='recorded' THEN 1 ELSE 0 END) AS has_recorded
                FROM identity_rows
                GROUP BY account_id
            )
            SELECT candidate.username_norm
            FROM identity_summary summary
            CROSS JOIN task_mode_candidates candidate
            WHERE summary.has_recorded=0
              AND candidate.target_id=? AND candidate.mode=?
              AND candidate.discovery_order=summary.first_pending
            """,
            (*parameters, target_id, mode, target_id, mode),
        ).fetchall()
        return {row["username_norm"] for row in rows}

    @staticmethod
    def _reconcile_task_mode_candidates_in_connection(
        connection: sqlite3.Connection,
        task_id: str,
        target_id: str,
        mode: str,
        now: str,
        *,
        username_norms: Iterable[str] | None = None,
        primary_result_usernames: set[str] | None = None,
    ) -> None:
        """Converge crash gaps without reopening an already processed profile.

        ``record_result`` and the terminal candidate update intentionally remain two
        short transactions.  If the process exits between them, the result/global
        dedupe rows are authoritative and this reconciliation closes the gap on the
        next append or resume.
        """

        scoped_usernames = (
            tuple(dict.fromkeys(username_norms))
            if username_norms is not None
            else None
        )
        if scoped_usernames is not None and not scoped_usernames:
            return
        scope_sql = ""
        if scoped_usernames is not None:
            scope_sql = (
                " AND candidate.username_norm IN ("
                + ",".join("?" for _ in scoped_usernames)
                + ")"
            )

        # A renamed account may have two discovered rows but one saved result.
        # Reserve the recorded counter for its first processed alias. If Core
        # exits after stable-ID confirmation and before finishing the new alias,
        # recovering that alias as another recording would inflate saved totals.
        if primary_result_usernames is None:
            primary_result_usernames = CoreService._primary_task_mode_candidate_usernames(
                connection, target_id, mode, username_norms=scoped_usernames
            )
        primary_result_alias_sql = """
            AND candidate.username_norm IN (SELECT value FROM json_each(?))
        """
        primary_result_parameters = (_json(sorted(primary_result_usernames)),)

        def dedupe_additional_recorded_aliases() -> None:
            connection.execute(
                f"""
                UPDATE task_mode_candidates AS candidate
                SET state='deduped', processed_at=COALESCE(processed_at, ?), updated_at=?
                WHERE candidate.target_id=? AND candidate.mode=? AND candidate.state='pending'
                  {scope_sql}
                  AND EXISTS (
                      SELECT 1
                      FROM instagram_username_aliases alias
                      JOIN instagram_username_aliases saved_alias
                        ON saved_alias.account_id=alias.account_id
                      JOIN task_mode_candidates saved
                        ON saved.username_norm=saved_alias.username_norm
                       AND saved.target_id=candidate.target_id
                       AND saved.mode=candidate.mode
                       AND saved.state='recorded'
                      WHERE alias.username_norm=candidate.username_norm
                  )
                """,
                (now, now, target_id, mode, *(scoped_usernames or ())),
            )

        task = connection.execute(
            "SELECT owner_user_id, settings_json FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        # A result may commit immediately before the process exits, leaving its
        # review insert unfinished. Recover from that exact saved evidence before
        # marking the spool row recorded; never reopen/recollect its profile and
        # never recreate an existing (including approved/rejected) review.
        unfinished_reviews = connection.execute(
            f"""
            SELECT result.*, claim.claimed_by_user_id
            FROM task_mode_candidates candidate
            JOIN instagram_username_aliases alias ON alias.username_norm=candidate.username_norm
            JOIN task_results result ON result.account_id=alias.account_id
                AND result.target_id=candidate.target_id AND result.task_id=?
            JOIN workbench_identity_claims claim ON claim.account_id=result.account_id
                AND claim.claimed_by_user_id=?
            WHERE candidate.target_id=? AND candidate.mode=? AND candidate.state='pending'
                {scope_sql}
                AND result.visibility IN ('private','public')
                AND claim.source IN (?, 'collection')
                AND (claim.source_target IS NULL OR claim.source_target=candidate.target_id)
                AND EXISTS(SELECT 1 FROM json_each(result.sources_json) WHERE value=candidate.mode)
                AND NOT EXISTS(SELECT 1 FROM workbench_candidates saved WHERE saved.account_id=result.account_id)
                AND NOT EXISTS(SELECT 1 FROM workbench_collection_exclusions excluded WHERE excluded.account_id=result.account_id)
            """,
            (task_id, task["owner_user_id"], target_id, mode, *(scoped_usernames or ()), mode),
        ).fetchall()
        restored_reviews = 0
        for saved in unfinished_reviews:
            saved_profile = _loads(saved["profile_json"])
            saved_screening = _loads(saved["screening_json"])
            if not isinstance(saved_profile, dict) or not isinstance(saved_screening, dict):
                continue
            if _review_requires_exclusion(saved["visibility"], saved_profile, saved_screening):
                continue
            if saved["visibility"] == "public" and saved_screening.get("routing_result") != "public_primary_review":
                continue
            recovered_screening_json = (
                _json(_normalize_private_review_screening(saved_screening))
                if saved["visibility"] == "private"
                else saved["screening_json"]
            )
            restored = connection.execute(
                """
                INSERT OR IGNORE INTO workbench_candidates(
                    id, owner_user_id, account_id, visibility, status, profile_json,
                    screening_json, review_cache_json, source_mode, source_target,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'pending', ?, ?, '{}', ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), task["owner_user_id"], saved["account_id"], saved["visibility"],
                 saved["profile_json"], recovered_screening_json, mode, target_id, saved["created_at"], now),
            )
            if restored.rowcount != 1:
                continue
            restored_reviews += 1
            _event(connection, task["owner_user_id"], "task", task_id,
                   "task.review_recovered", {"target_id": target_id, "account_id": saved["account_id"], "result_id": saved["id"]})
        if restored_reviews:
            _bump_workbench_revision(connection, now)
        connection.execute(
            f"""
            UPDATE task_mode_candidates AS candidate
            SET state='recorded', processed_at=COALESCE(processed_at, ?), updated_at=?
            WHERE candidate.target_id=? AND candidate.mode=? AND candidate.state='pending'
              {scope_sql}
              AND EXISTS (
                  SELECT 1
                  FROM task_results result
                  JOIN instagram_username_aliases alias
                    ON alias.account_id=result.account_id
                  JOIN json_each(result.sources_json) source
                    ON source.value=candidate.mode
                  WHERE result.target_id=candidate.target_id
                    AND alias.username_norm=candidate.username_norm
                    {primary_result_alias_sql}
              )
            """,
            (now, now, target_id, mode, *(scoped_usernames or ()), *primary_result_parameters),
        )
        dedupe_additional_recorded_aliases()
        # Known rename aliases share one durable pending owner, even before its
        # first result is saved. Otherwise parallel children can each resume the
        # same identity claim under a different username and open it twice.
        # Keep the earliest pending row resumable if its read fails; only its
        # additional aliases become duplicates. The primary set already expands
        # each requested identity, so bounded appends do not scan the whole spool.
        connection.execute(
            f"""
            UPDATE task_mode_candidates AS candidate
            SET state='deduped', processed_at=COALESCE(processed_at, ?), updated_at=?
            WHERE candidate.target_id=? AND candidate.mode=? AND candidate.state='pending'
              {scope_sql}
              AND candidate.username_norm NOT IN (SELECT value FROM json_each(?))
              AND EXISTS (
                  SELECT 1 FROM instagram_username_aliases alias
                  WHERE alias.username_norm=candidate.username_norm
              )
            """,
            (now, now, target_id, mode, *(scoped_usernames or ()), *primary_result_parameters),
        )
        connection.execute(
            f"""
            UPDATE task_mode_candidates AS candidate
            SET state='deduped', processed_at=COALESCE(processed_at, ?), updated_at=?
            WHERE candidate.target_id=? AND candidate.mode=? AND candidate.state='pending'
              {scope_sql}
              AND EXISTS (
                  SELECT 1
                  FROM instagram_username_aliases alias
                  JOIN global_seen seen ON seen.account_id=alias.account_id
                  WHERE alias.username_norm=candidate.username_norm
                    AND NOT EXISTS (
                        SELECT 1
                        FROM workbench_identity_claims claim
                        WHERE claim.account_id=alias.account_id
                          AND claim.claimed_by_user_id=?
                          AND claim.source=candidate.mode
                          AND claim.source_target=candidate.target_id
                          AND NOT EXISTS (
                              SELECT 1 FROM task_results terminal_result
                              WHERE terminal_result.account_id=claim.account_id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM workbench_candidates terminal_candidate
                              WHERE terminal_candidate.account_id=claim.account_id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM workbench_collection_exclusions terminal_exclusion
                              WHERE terminal_exclusion.account_id=claim.account_id
                          )
                    )
              )
            """,
            (
                now,
                now,
                target_id,
                mode,
                *(scoped_usernames or ()),
                task["owner_user_id"],
            ),
        )

    def _new_discovery_write_session(self) -> DiscoveryWriteSession:
        """Return an unopened session owned by one production batch callback."""
        return DiscoveryWriteSession(self.database)

    def discover_task_mode_candidate(
        self, owner_user_id: str, task_id: str, target_id: str, mode: str,
        username: str,
        *, _session: DiscoveryWriteSession | None = None,
    ) -> dict[str, Any]:
        """Commit the parent's global reservation and one spool row together.

        An unfinished own claim remains resumable. Foreign/terminal identities
        become terminal duplicate spool rows, so discovery counters stay honest
        without sending them to a child. Any append failure rolls back the claim.
        """
        with (_session.write(self.database) if _session is not None else self.database.write()) as connection:
            self._owned_candidate_target(connection, owner_user_id, task_id, target_id, mode)
            claim = self.claim_workbench_identity(
                owner_user_id, username=username, source=mode, source_target=target_id,
                allow_owned_resume=True, _connection=connection,
            )
            result = self.append_task_mode_candidates(
                owner_user_id, task_id, target_id, mode, [username], _connection=connection,
            )
            return {**result, "duplicate": claim["duplicate"]}

    def append_task_mode_candidates(
        self,
        owner_user_id: str,
        task_id: str,
        target_id: str,
        mode: str,
        usernames: Iterable[str],
        *,
        max_total: int | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Durably append one bounded visible-list batch.

        The caller must never hand this method an unbounded relationship list.  A
        hard service-layer limit protects both production and future callers even if
        a renderer/worker regression bypasses the intended 100-name batching.
        """

        raw_usernames = list(usernames)
        if len(raw_usernames) > CANDIDATE_BATCH_MAX:
            raise ValidationError(
                f"Candidate batches may contain at most {CANDIDATE_BATCH_MAX} usernames"
            )
        if max_total is not None and (
            isinstance(max_total, bool)
            or not isinstance(max_total, int)
            or max_total < 1
        ):
            raise ValidationError("Candidate maximum total must be a positive integer")
        normalized: list[tuple[str, str]] = []
        seen_in_batch: set[str] = set()
        for username in raw_usernames:
            username_norm, username_display = normalize_instagram_username(username)
            if username_norm not in seen_in_batch:
                normalized.append((username_norm, username_display))
                seen_in_batch.add(username_norm)

        now = isoformat()
        added = 0
        inserted_username_norms: list[str] = []
        with (nullcontext(_connection) if _connection is not None else self.database.write()) as connection:
            target = self._owned_candidate_target(
                connection, owner_user_id, task_id, target_id, mode
            )
            task = self._owned_task(connection, owner_user_id, task_id)
            if (target["status"] == "completed" or task["status"] == "completed"
                    or target["current_stage"] == "deleted_archived"):
                existing = {
                    row[0] for row in connection.execute(
                        "SELECT username_norm FROM task_mode_candidates "
                        "WHERE target_id=? AND mode=? AND username_norm IN "
                        "(SELECT value FROM json_each(?))",
                        (target_id, mode, _json([name for name, _ in normalized])),
                    )
                }
                if any(name not in existing for name, _ in normalized):
                    raise ConflictError("A closed target cannot receive new candidates")
                return {
                    "added": 0,
                    **self._candidate_stats_from_connection(connection, target_id, mode),
                    "last_username": normalized[-1][1] if normalized else None,
                }
            counter = connection.execute(
                """
                SELECT total, next_discovery_order
                FROM task_mode_candidate_counters
                WHERE target_id=? AND mode=?
                """,
                (target_id, mode),
            ).fetchone()
            if counter is None:
                # initialize() normally guarantees the projection.  Keep this
                # repair path for a manually recovered/copy-restored database.
                latest_source = connection.execute(
                    """
                    SELECT discovery_order
                    FROM task_mode_candidates
                    WHERE target_id=? AND mode=?
                    ORDER BY discovery_order DESC
                    LIMIT 1
                    """,
                    (target_id, mode),
                ).fetchone()
                if latest_source is None:
                    current_total = 0
                    next_order = 1
                else:
                    aggregate = connection.execute(
                        """
                        SELECT
                            COUNT(*) AS total,
                            SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending,
                            SUM(CASE WHEN state='recorded' THEN 1 ELSE 0 END) AS recorded,
                            SUM(CASE WHEN state='deduped' THEN 1 ELSE 0 END) AS deduped
                        FROM task_mode_candidates
                        WHERE target_id=? AND mode=?
                        """,
                        (target_id, mode),
                    ).fetchone()
                    current_total = int(aggregate["total"] or 0)
                    next_order = int(latest_source["discovery_order"] or 0) + 1
                    connection.execute(
                        """
                        INSERT INTO task_mode_candidate_counters(
                            target_id, mode, total, pending, recorded, deduped,
                            next_discovery_order
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_id,
                            mode,
                            current_total,
                            int(aggregate["pending"] or 0),
                            int(aggregate["recorded"] or 0),
                            int(aggregate["deduped"] or 0),
                            next_order,
                        ),
                    )
            else:
                current_total = int(counter["total"] or 0)
                next_order = int(counter["next_discovery_order"] or 1)
            for username_norm, username_display in normalized:
                if max_total is not None and current_total >= max_total:
                    break
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO task_mode_candidates(
                        target_id, mode, username_norm, username_display,
                        discovery_order, discovered_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_id,
                        mode,
                        username_norm,
                        username_display,
                        next_order,
                        now,
                        now,
                    ),
                )
                if cursor.rowcount:
                    added += 1
                    inserted_username_norms.append(username_norm)
                    next_order += 1
                    current_total += 1

            # Appending is the hot visible-list path.  Reconcile only rows from
            # this bounded batch so its cost cannot grow with a large spool.
            # The explicit resume/drain entry point still performs a full
            # reconciliation to close any prior process-crash gap.
            if inserted_username_norms:
                self._reconcile_task_mode_candidates_in_connection(
                    connection,
                    task_id,
                    target_id,
                    mode,
                    now,
                    username_norms=inserted_username_norms,
                )
            stats = self._candidate_stats_from_connection(
                connection, target_id, mode
            )
        return {
            "added": added,
            **stats,
            "last_username": normalized[-1][1] if normalized else None,
        }

    def reconcile_task_mode_candidates(
        self,
        owner_user_id: str,
        task_id: str,
        target_id: str,
        mode: str,
        *,
        usernames: Iterable[str] | None = None,
    ) -> dict[str, int]:
        """Repair a full resume, or only an interrupted candidate's identities.

        A bounded failure repair includes every pending alias of each requested
        stable identity. Repairing only the latest username can lose the first
        recorded alias after a result commit, or inflate its saved counter.
        """
        normalized: tuple[str, ...] | None = None
        if usernames is not None:
            raw_usernames = list(usernames)
            if len(raw_usernames) > CANDIDATE_BATCH_MAX:
                raise ValidationError(
                    f"Candidate repairs may contain at most {CANDIDATE_BATCH_MAX} usernames"
                )
            normalized = tuple(dict.fromkeys(
                normalize_instagram_username(username)[0] for username in raw_usernames
            ))
        now = isoformat()
        with self.database.write() as connection:
            self._owned_candidate_target(
                connection, owner_user_id, task_id, target_id, mode
            )
            if normalized is None:
                self._reconcile_task_mode_candidates_in_connection(
                    connection, task_id, target_id, mode, now
                )
            elif normalized:
                primary_result_usernames = self._primary_task_mode_candidate_usernames(
                    connection, target_id, mode, username_norms=normalized
                )
                requested_values = ",".join("(?)" for _ in normalized)
                # Keep the small request on the outer side of these lookups.
                # A freely reordered join can instead scan every pending row
                # before checking whether it belongs to one requested identity.
                related = connection.execute(
                    f"""
                    WITH requested(username_norm) AS (VALUES {requested_values})
                    SELECT candidate.username_norm, candidate.discovery_order
                    FROM requested
                    CROSS JOIN task_mode_candidates candidate
                    WHERE candidate.target_id=? AND candidate.mode=?
                      AND candidate.state='pending'
                      AND candidate.username_norm=requested.username_norm
                    UNION
                    SELECT candidate.username_norm, candidate.discovery_order
                    FROM requested
                    CROSS JOIN instagram_username_aliases requested_alias
                    CROSS JOIN instagram_username_aliases alias
                    CROSS JOIN task_mode_candidates candidate
                    WHERE candidate.target_id=? AND candidate.mode=?
                      AND candidate.state='pending'
                      AND requested_alias.username_norm=requested.username_norm
                      AND alias.account_id=requested_alias.account_id
                      AND candidate.username_norm=alias.username_norm
                    ORDER BY discovery_order
                    """,
                    (*normalized, target_id, mode, target_id, mode),
                ).fetchall()
                # Alias history can exceed the request size. Keep SQL parameter
                # batches bounded and process earliest discoveries first so one
                # saved identity still produces exactly one recorded candidate.
                for offset in range(0, len(related), CANDIDATE_BATCH_MAX):
                    self._reconcile_task_mode_candidates_in_connection(
                        connection, task_id, target_id, mode, now,
                        username_norms=[
                            row["username_norm"]
                            for row in related[offset:offset + CANDIDATE_BATCH_MAX]
                        ],
                        primary_result_usernames=primary_result_usernames,
                    )
            return self._candidate_stats_from_connection(
                connection, target_id, mode
            )

    def task_mode_candidate_stats(
        self,
        owner_user_id: str,
        task_id: str,
        target_id: str,
        mode: str,
    ) -> dict[str, int]:
        with self.database.read() as connection:
            self._owned_candidate_target(
                connection, owner_user_id, task_id, target_id, mode
            )
            return self._candidate_stats_from_connection(
                connection, target_id, mode
            )

    def list_pending_task_mode_candidates(
        self,
        owner_user_id: str,
        task_id: str,
        target_id: str,
        mode: str,
        *,
        limit: int = CANDIDATE_BATCH_MAX,
        after_discovery_order: int | None = None,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= CANDIDATE_BATCH_MAX:
            raise ValidationError(
                f"Candidate page size must be between 1 and {CANDIDATE_BATCH_MAX}"
            )
        if after_discovery_order is not None and (isinstance(after_discovery_order, bool) or not isinstance(after_discovery_order, int) or after_discovery_order < 0):
            raise ValidationError("Candidate discovery cursor must be a non-negative integer")
        cursor_sql = " AND discovery_order>?" if after_discovery_order is not None else ""
        cursor_parameters = (after_discovery_order,) if after_discovery_order is not None else ()
        with self.database.read() as connection:
            self._owned_candidate_target(
                connection, owner_user_id, task_id, target_id, mode
            )
            rows = connection.execute(
                f"""
                SELECT * FROM task_mode_candidates
                WHERE target_id=? AND mode=? AND state='pending'{cursor_sql}
                ORDER BY discovery_order
                LIMIT ?
                """,
                (target_id, mode, *cursor_parameters, limit),
            ).fetchall()
        return [self._candidate_dict(row) for row in rows]

    def confirmed_task_mode_candidate_usernames(
        self, owner_user_id: str, task_id: str, target_id: str, mode: str,
        usernames: list[str],
    ) -> set[str]:
        """A committed source handoff confirms identity even before detail drain.

        Used only for restoring unconfirmed-source checkpoint obligations after
        a crash between candidate commit and the producer's progress clear.
        """
        if len(usernames) > 1000:
            raise ValidationError("Too many unconfirmed relationship identities")
        normalized = tuple(dict.fromkeys(normalize_instagram_username(value)[0] for value in usernames))
        result: set[str] = set()
        with self.database.read() as connection:
            self._owned_candidate_target(connection, owner_user_id, task_id, target_id, mode)
            for offset in range(0, len(normalized), 100):
                batch = normalized[offset:offset + 100]
                placeholders = ",".join("?" for _ in batch)
                result.update(row["username_norm"] for row in connection.execute(
                    f"SELECT username_norm FROM task_mode_candidates WHERE target_id=? AND mode=? "
                    f"AND username_norm IN ({placeholders})", (target_id, mode, *batch),
                ))
        return result

    def terminal_task_mode_candidate_usernames(
        self,
        owner_user_id: str,
        task_id: str,
        target_id: str,
        mode: str,
        usernames: Iterable[str],
    ) -> set[str]:
        """Read terminal rows for one bounded set of retained screening pages."""

        raw_usernames = list(usernames)
        if len(raw_usernames) > CANDIDATE_BATCH_MAX:
            raise ValidationError(
                f"Candidate state lookups may contain at most {CANDIDATE_BATCH_MAX} usernames"
            )
        normalized = tuple(dict.fromkeys(
            normalize_instagram_username(username)[0] for username in raw_usernames
        ))
        with self.database.read() as connection:
            self._owned_candidate_target(connection, owner_user_id, task_id, target_id, mode)
            if not normalized:
                return set()
            placeholders = ",".join("?" for _ in normalized)
            rows = connection.execute(
                f"""
                SELECT username_norm FROM task_mode_candidates
                WHERE target_id=? AND mode=? AND state IN ('recorded', 'deduped')
                  AND username_norm IN ({placeholders})
                """,
                (target_id, mode, *normalized),
            ).fetchall()
        return {row["username_norm"] for row in rows}

    def finish_task_mode_candidate(
        self,
        owner_user_id: str,
        task_id: str,
        target_id: str,
        mode: str,
        username: str,
        *,
        state: str,
    ) -> dict[str, Any]:
        if state not in CANDIDATE_TERMINAL_STATES:
            raise ValidationError("Candidate terminal state must be recorded or deduped")
        username_norm, _ = normalize_instagram_username(username)
        now = isoformat()
        with self.database.write() as connection:
            self._owned_candidate_target(
                connection, owner_user_id, task_id, target_id, mode
            )
            row = connection.execute(
                """
                SELECT * FROM task_mode_candidates
                WHERE target_id=? AND mode=? AND username_norm=?
                """,
                (target_id, mode, username_norm),
            ).fetchone()
            if row is None:
                raise NotFoundError("Candidate not found")
            # A terminal row is immutable.  This is important if another selected
            # window wins the global-dedupe race while the current profile is open.
            if row["state"] == "pending":
                connection.execute(
                    """
                    UPDATE task_mode_candidates
                    SET state=?, processed_at=?, updated_at=?
                    WHERE target_id=? AND mode=? AND username_norm=? AND state='pending'
                    """,
                    (state, now, now, target_id, mode, username_norm),
                )
                row = connection.execute(
                    """
                    SELECT * FROM task_mode_candidates
                    WHERE target_id=? AND mode=? AND username_norm=?
                    """,
                    (target_id, mode, username_norm),
                ).fetchone()
        return self._candidate_dict(row)

    # Results and shared deduplication ----------------------------------
    def record_result(
        self,
        owner_user_id: str,
        task_id: str,
        target_id: str,
        *,
        username: str,
        instagram_user_id: str | None,
        source_mode: str,
        visibility: str,
        profile: dict[str, Any],
        screening: dict[str, Any],
        qualified: bool | None,
        dedupe_claim_id: str | None = None,
    ) -> dict[str, Any]:
        username_norm, username_display = normalize_instagram_username(username)
        if source_mode not in TASK_MODES:
            raise ValidationError("Invalid result source mode")
        if visibility not in VISIBILITY_VALUES:
            raise ValidationError("Invalid visibility value")
        if instagram_user_id is not None:
            instagram_user_id = instagram_user_id.strip()
            if not instagram_user_id or len(instagram_user_id) > 100 or instagram_user_id.casefold().startswith(("fb:", "fbid:")):
                raise ValidationError("Invalid Instagram user id")
        _reject_sensitive_fields(profile, "profile")
        _reject_sensitive_fields(screening, "screening")
        profile, screening = normalize_person_category_fields(profile, screening)

        now = isoformat()
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            if source_mode not in _loads(task["modes_json"]):
                raise ValidationError("Result source mode is not enabled for this task")
            target = connection.execute(
                "SELECT status, current_stage FROM task_targets WHERE id=? AND task_id=?",
                (target_id, task_id),
            ).fetchone()
            if target is None:
                raise NotFoundError("Target not found")

            username_account = self._find_account(connection, None, username_norm)
            if (instagram_user_id and username_account is not None
                    and username_account["instagram_user_id"]
                    and username_account["instagram_user_id"] != instagram_user_id):
                raise ConflictError("Result username and stable Instagram id refer to different accounts")
            account = (
                self._find_account(connection, instagram_user_id, username_norm)
                if instagram_user_id else username_account
            )
            if (task["status"] == "completed" or target["status"] == "completed"
                    or target["current_stage"] in {"completed_archived", "deleted_archived"}):
                # Replayed delivery may acknowledge an existing result, but cannot
                # introduce identities, new modes, or progress into closed history.
                saved = connection.execute(
                    "SELECT r.*, a.instagram_user_id, a.current_username_display "
                    "FROM task_results r JOIN instagram_accounts a ON a.id=r.account_id "
                    "WHERE r.target_id=? AND r.account_id=?",
                    (target_id, account["id"] if account is not None else None),
                ).fetchone()
                if saved is None or source_mode not in _loads(saved["sources_json"]):
                    raise ConflictError("A closed target cannot receive new results")
                if dedupe_claim_id is not None and (
                    dedupe_claim_id != account["id"] or connection.execute(
                        "SELECT 1 FROM workbench_identity_claims "
                        "WHERE account_id=? AND claimed_by_user_id=?",
                        (dedupe_claim_id, owner_user_id),
                    ).fetchone() is None
                ):
                    raise ConflictError("Result does not match its workbench identity claim")
                response = self._result_dict(saved)
                response.update(was_globally_seen=True, deduped=True)
                return response
            was_globally_seen = False
            if account is None:
                account_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO instagram_accounts(
                        id, instagram_user_id, current_username_norm, current_username_display,
                        first_seen_at, last_seen_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (account_id, instagram_user_id, username_norm, username_display, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO instagram_username_aliases(account_id, username_norm, first_seen_at, last_seen_at)
                    VALUES(?, ?, ?, ?)
                    """,
                    (account_id, username_norm, now, now),
                )
            else:
                account_id = account["id"]
                was_globally_seen = connection.execute(
                    "SELECT 1 FROM global_seen WHERE account_id=?", (account_id,)
                ).fetchone() is not None
                if instagram_user_id and not account["instagram_user_id"]:
                    connection.execute(
                        "UPDATE instagram_accounts SET instagram_user_id=? WHERE id=?", (instagram_user_id, account_id)
                    )
                connection.execute(
                    "UPDATE instagram_accounts SET last_seen_at=? WHERE id=?", (now, account_id)
                )
                connection.execute(
                    """
                    INSERT INTO instagram_username_aliases(account_id, username_norm, first_seen_at, last_seen_at)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(username_norm) DO UPDATE SET last_seen_at=excluded.last_seen_at
                    """,
                    (account_id, username_norm, now, now),
                )

            canonical_result = connection.execute(
                "SELECT * FROM task_results WHERE account_id=? ORDER BY created_at, id LIMIT 1",
                (account_id,),
            ).fetchone()
            if canonical_result is not None:
                # Durable results are authoritative even if an older restore lost
                # their optional global_seen membership. A claim is not permission
                # to insert the same account under a second target.
                was_globally_seen = True

            if dedupe_claim_id is not None:
                owned_claim = connection.execute(
                    """
                    SELECT * FROM workbench_identity_claims
                    WHERE account_id=? AND claimed_by_user_id=?
                    """,
                    (dedupe_claim_id, owner_user_id),
                ).fetchone()
                if account_id != dedupe_claim_id or owned_claim is None:
                    raise ConflictError("Result does not match its workbench identity claim")
                if canonical_result is None:
                    if (
                        owned_claim["source"] not in {source_mode, "collection"}
                        or owned_claim["source_target"] not in {None, target_id}
                    ):
                        raise ConflictError("Result source does not match its workbench identity claim")
                    # Legacy unbound claims may be committed once; bind them to
                    # their first actual target inside the same transaction.
                    if owned_claim["source_target"] is None:
                        connection.execute(
                            "UPDATE workbench_identity_claims SET source=?, source_target=? WHERE account_id=?",
                            (source_mode, target_id, account_id),
                        )
                    was_globally_seen = False

            remember_identity_owner(connection, account_id, owner_user_id, now)

            # Persist target-level progress independently from the task-wide status.
            # This lets the desktop distinguish a healthy target from a sibling
            # window's network error and remains accurate even before the next
            # periodic checkpoint is written.
            connection.execute(
                """
                UPDATE task_targets
                SET current_stage='screening_accounts', last_success_at=?,
                    last_error=NULL, updated_at=?
                WHERE id=? AND task_id=?
                """,
                (now, now, target_id, task_id),
            )

            existing_result = connection.execute(
                "SELECT * FROM task_results WHERE target_id=? AND account_id=?", (target_id, account_id)
            ).fetchone()

            if existing_result is not None and connection.execute(
                """
                SELECT 1 FROM workbench_candidates WHERE account_id=?
                UNION ALL
                SELECT 1 FROM workbench_collection_exclusions WHERE account_id=?
                LIMIT 1
                """,
                (account_id, account_id),
            ).fetchone() is not None:
                # A delayed worker or the legacy result endpoint must never replace
                # the original collected profile after it has entered review.
                response = self._result_dict(connection.execute(
                    "SELECT r.*, a.instagram_user_id, a.current_username_display "
                    "FROM task_results r JOIN instagram_accounts a ON a.id=r.account_id WHERE r.id=?",
                    (existing_result["id"],),
                ).fetchone())
                response.update(was_globally_seen=True, deduped=True)
                return response

            # The shared dedupe database is authoritative across every collection
            # mode, source target, historical task and application login. The first
            # canonical result remains visible; a later occurrence is recorded only
            # as dedupe metadata and is never inserted into another result list.
            if was_globally_seen and existing_result is None:
                global_row = connection.execute(
                    "SELECT sources_json, first_seen_at FROM global_seen WHERE account_id=?", (account_id,)
                ).fetchone()
                global_sources = set(_loads(global_row["sources_json"])) if global_row else set()
                global_sources.add(source_mode)
                connection.execute(
                    "UPDATE global_seen SET sources_json=?, last_seen_at=? WHERE account_id=?",
                    (_json(sorted(global_sources)), now, account_id),
                )
                canonical = connection.execute(
                    """
                    SELECT result.*
                    FROM task_results result
                    JOIN tasks canonical_task ON canonical_task.id=result.task_id
                    WHERE result.account_id=? AND canonical_task.owner_user_id=?
                    ORDER BY result.created_at, result.id
                    LIMIT 1
                    """,
                    (account_id, owner_user_id),
                ).fetchone()
                incoming_rank = _person_recognition_rank(profile, screening)
                backfilled = False
                if (
                    canonical is not None
                    and incoming_rank > _stored_person_recognition_rank(canonical)
                    and connection.execute(
                        "SELECT 1 FROM workbench_candidates WHERE account_id=? "
                        "UNION ALL SELECT 1 FROM workbench_collection_exclusions WHERE account_id=? LIMIT 1",
                        (account_id, account_id),
                    ).fetchone() is None
                ):
                    canonical_profile = _loads(canonical["profile_json"])
                    canonical_screening = _loads(canonical["screening_json"])
                    if not isinstance(canonical_profile, dict):
                        canonical_profile = {}
                    if not isinstance(canonical_screening, dict):
                        canonical_screening = {}
                    canonical_profile["person_category"] = profile.get(
                        "person_category",
                        "unknown",
                    )
                    canonical_screening["person_recognition"] = screening.get(
                        "person_recognition",
                        {
                            "enabled": True,
                            "checked": False,
                            "category": "unknown",
                            "reason": "classifier_failed",
                        },
                    )
                    connection.execute(
                        """
                        UPDATE task_results
                        SET profile_json=?, screening_json=?
                        WHERE id=?
                        """,
                        (
                            _json(canonical_profile),
                            _json(canonical_screening),
                            canonical["id"],
                        ),
                    )
                    backfilled = True
                _event(
                    connection,
                    owner_user_id,
                    "task",
                    task_id,
                    "task.result_deduped",
                    {"target_id": target_id, "account_id": account_id, "username": username_norm},
                )
                return {
                    "username": username_display,
                    "account_id": account_id,
                    "sources": sorted(global_sources),
                    "was_globally_seen": True,
                    "deduped": True,
                    "person_recognition_backfilled": backfilled,
                }

            global_row = connection.execute(
                "SELECT sources_json FROM global_seen WHERE account_id=?", (account_id,)
            ).fetchone()
            global_sources = set(_loads(global_row["sources_json"])) if global_row else set()
            global_sources.add(source_mode)
            connection.execute(
                """
                INSERT INTO global_seen(account_id, sources_json, first_seen_at, last_seen_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    sources_json=excluded.sources_json,
                    last_seen_at=excluded.last_seen_at
                """,
                (account_id, _json(sorted(global_sources)), now, now),
            )

            sources = set(_loads(existing_result["sources_json"])) if existing_result else set()
            sources.add(source_mode)
            result_id = existing_result["id"] if existing_result else str(uuid.uuid4())
            created_at = existing_result["created_at"] if existing_result else now
            connection.execute(
                """
                INSERT INTO task_results(
                    id, task_id, target_id, account_id, sources_json, visibility,
                    profile_json, screening_json, qualified, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id, account_id) DO UPDATE SET
                    sources_json=excluded.sources_json,
                    visibility=excluded.visibility,
                    profile_json=excluded.profile_json,
                    screening_json=excluded.screening_json,
                    qualified=excluded.qualified,
                    updated_at=excluded.updated_at
                """,
                (
                    result_id,
                    task_id,
                    target_id,
                    account_id,
                    _json(sorted(sources)),
                    visibility,
                    _json(profile),
                    _json(screening),
                    None if qualified is None else int(qualified),
                    created_at,
                    now,
                ),
            )
            _event(
                connection,
                owner_user_id,
                "task",
                task_id,
                "task.result_recorded",
                {"target_id": target_id, "account_id": account_id, "globally_seen": was_globally_seen},
            )
            result = connection.execute(
                """
                SELECT r.*, a.instagram_user_id, a.current_username_display
                FROM task_results r JOIN instagram_accounts a ON a.id=r.account_id
                WHERE r.id=?
                """,
                (result_id,),
            ).fetchone()
        response = self._result_dict(result)
        response["was_globally_seen"] = was_globally_seen
        response["deduped"] = False
        return response

    def list_results(self, owner_user_id: str, task_id: str) -> list[dict[str, Any]]:
        with self.database.read() as connection:
            self._owned_task(connection, owner_user_id, task_id)
            rows = connection.execute(
                """
                SELECT r.*, a.instagram_user_id, a.current_username_display
                FROM task_results r JOIN instagram_accounts a ON a.id=r.account_id
                WHERE r.task_id=? AND a.current_username_norm NOT GLOB 'fb:*' ORDER BY r.updated_at DESC
                """,
                (task_id,),
            ).fetchall()
        return [self._result_dict(row) for row in rows]

    def result_usernames_for_target_mode(
        self,
        owner_user_id: str,
        task_id: str,
        target_id: str,
        mode: str,
    ) -> set[str]:
        """Return durable per-mode screening progress for safe task recovery."""
        if mode not in TASK_MODES:
            raise ValidationError("Invalid result source mode")
        with self.database.read() as connection:
            self._owned_task(connection, owner_user_id, task_id)
            if connection.execute(
                "SELECT 1 FROM task_targets WHERE id=? AND task_id=?",
                (target_id, task_id),
            ).fetchone() is None:
                raise NotFoundError("Target not found")
            rows = connection.execute(
                """
                SELECT a.current_username_norm, r.sources_json
                FROM task_results r
                JOIN instagram_accounts a ON a.id=r.account_id
                WHERE r.task_id=? AND r.target_id=?
                """,
                (task_id, target_id),
            ).fetchall()
        return {
            row["current_username_norm"]
            for row in rows
            if mode in _loads(row["sources_json"])
        }

    def check_global_dedupe(
        self,
        username: str,
        *,
        owner_user_id: str | None = None,
    ) -> dict[str, Any]:
        username_norm, username_display = normalize_instagram_username(username)
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT a.id, g.sources_json, COALESCE(g.first_seen_at,a.first_seen_at) AS first_seen_at, COALESCE(g.last_seen_at,a.last_seen_at) AS last_seen_at
                FROM instagram_username_aliases alias
                JOIN instagram_accounts a ON a.id=alias.account_id
                LEFT JOIN global_seen g ON g.account_id=a.id
                WHERE alias.username_norm=?
                """,
                (username_norm,),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """SELECT a.id, g.sources_json,
                              COALESCE(g.first_seen_at,a.first_seen_at) AS first_seen_at,
                              COALESCE(g.last_seen_at,a.last_seen_at) AS last_seen_at
                       FROM instagram_accounts a LEFT JOIN global_seen g ON g.account_id=a.id
                       WHERE a.current_username_norm=?""", (username_norm,),
                ).fetchone()
            seen = bool(row and (row["sources_json"] is not None
                        or identity_has_durable_history(connection, row["id"])))
        return {
            "username": username_display,
            "seen": seen,
            "sources": _loads(row["sources_json"]) if row and row["sources_json"] else [],
            "first_seen_at": row["first_seen_at"] if row else None,
            "last_seen_at": row["last_seen_at"] if row else None,
            "person_recognition_needed": False,
        }

    def should_skip_relationship_hover(
        self,
        owner_user_id: str,
        username: str,
        *,
        source: str,
        source_target: str,
    ) -> bool:
        """Skip a hover only for an identity with terminal or foreign evidence.

        A claim is globally reserved before its hover exclusion/result is saved.
        If the app stops in that gap, its own source must recheck the card on
        recovery; an ordinary global-seen lookup would incorrectly treat the
        unfinished reservation as a completed duplicate and bypass the hover.
        Other owners/sources still see the reservation as occupied.
        """

        username_norm, _ = normalize_instagram_username(username)
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT account.id FROM instagram_username_aliases alias
                   JOIN instagram_accounts account ON account.id=alias.account_id
                   WHERE alias.username_norm=?""",
                (username_norm,),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT id FROM instagram_accounts WHERE current_username_norm=?",
                    (username_norm,),
                ).fetchone()
            if row is None:
                return False
            account_id = row["id"]
            own_claim = connection.execute(
                """SELECT 1 FROM workbench_identity_claims
                   WHERE account_id=? AND claimed_by_user_id=?
                     AND source=? AND source_target=?""",
                (account_id, owner_user_id, source, source_target),
            ).fetchone() is not None
            if own_claim:
                finished = connection.execute(
                    """SELECT 1 FROM workbench_candidates WHERE account_id=?
                       UNION ALL SELECT 1 FROM task_results WHERE account_id=?
                       UNION ALL SELECT 1 FROM task_result_duplicate_archive WHERE account_id=?
                       LIMIT 1""",
                    (account_id,) * 3,
                ).fetchone() is not None
                if not finished:
                    finished = _has_historical_terminal_exclusion(connection, account_id)
                # Current hover exclusion and task result use separate durable
                # writes. If only the exclusion committed, its original source
                # must revisit the card to finish the missing result.
                if not finished:
                    return False
            return connection.execute(
                "SELECT 1 FROM global_seen WHERE account_id=?",
                (account_id,),
            ).fetchone() is not None or identity_has_durable_history(connection, account_id)

    # New-generation screening workbench -------------------------------
    @staticmethod
    def _workbench_candidate_dict(row: sqlite3.Row) -> dict[str, Any]:
        profile = _loads(row["profile_json"])
        screening = _loads(row["screening_json"])
        review_cache = _loads(row["review_cache_json"])
        status = str(row["status"])
        reviewed_at = row["reviewed_at"]
        username = row["current_username_display"]
        assert_username_platform(None, username)
        return {
            "id": row["id"],
            "account_id": row["account_id"],
            "claim_account_id": row["account_id"],
            "instagram_user_id": row["instagram_user_id"],
            "platform": "instagram",
            "username": username,
            "handle": f"@{username}",
            "visibility": row["visibility"],
            "status": status,
            "review_status": status,
            "review_stage": int(_row_value(row, "review_stage", 1)),
            "review_transferred_at": _row_value(row, "review_transferred_at"),
            "profile": profile if isinstance(profile, dict) else {},
            "screening": screening if isinstance(screening, dict) else {},
            # Pending rows expose protected review evidence. Approved rows may
            # keep a short-lived avatar preview for the action workspace; that
            # terminal preview is disposable through safe cache cleanup. Post
            # previews and screenshots are always discarded at decision.
            "review_cache": (
                review_cache
                if status in {"pending", "approved"} and isinstance(review_cache, dict)
                else {}
            ),
            "source_mode": row["source_mode"],
            "source_target": row["source_target"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "reviewed_at": reviewed_at,
            "approved_at": reviewed_at if status == "approved" else None,
            "rejected_at": reviewed_at if status == "rejected" else None,
        }

    @staticmethod
    def _avatar_only_review_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        """Keep snapshot payloads avatar-first without deleting legacy DB evidence."""

        profile = candidate.get("profile")
        if isinstance(profile, dict):
            profile.pop("recent_posts", None)
        review_cache = candidate.get("review_cache")
        avatar_preview = (
            review_cache.get("avatar_preview")
            if isinstance(review_cache, dict)
            else None
        )
        candidate["review_cache"] = (
            {"avatar_preview": avatar_preview}
            if isinstance(avatar_preview, str) and avatar_preview
            else {}
        )
        return candidate

    @staticmethod
    def _workbench_exclusion_dict(row: sqlite3.Row) -> dict[str, Any]:
        profile = _loads(row["profile_snapshot_json"])
        username = row["username_display"]
        assert_username_platform(None, username)
        return {
            "id": row["id"],
            "account_id": row["account_id"],
            "platform": "instagram",
            "username": username,
            "handle": f"@{username}",
            "reason_code": row["reason_code"],
            "reason": row["reason"],
            "location_country": row["location_country"],
            "profile": profile if isinstance(profile, dict) else {},
            "excluded_at": row["excluded_at"],
        }

    @staticmethod
    def _workbench_rejection_dict(row: sqlite3.Row) -> dict[str, Any]:
        profile = _loads(row["profile_snapshot_json"])
        screening = _loads(row["screening_snapshot_json"])
        username = row["current_username_display"]
        assert_username_platform(None, username)
        return {
            "id": row["id"],
            "candidate_id": row["candidate_id"],
            "account_id": row["account_id"],
            "platform": "instagram",
            "username": username,
            "handle": f"@{username}",
            "visibility": row["visibility"],
            "decision": row["decision"],
            "profile": profile if isinstance(profile, dict) else {},
            "screening": screening if isinstance(screening, dict) else {},
            "rejected_at": row["decided_at"],
            "reviewed_at": row["decided_at"],
        }

    def claim_workbench_identity(
        self,
        owner_user_id: str,
        *,
        username: str,
        instagram_user_id: str | None = None,
        source: str = "collection",
        source_target: str | None = None,
        allow_owned_resume: bool = False,
        _connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Atomically reserve one globally unseen identity before opening it.

        ``BEGIN IMMEDIATE`` in :meth:`Database.write` serializes competing browser
        windows.  Exactly one caller receives ``claimed``; every later occurrence
        returns ``duplicate`` and must skip location/profile reads.
        """

        username_norm, username_display = normalize_instagram_username(username)
        source = str(source).strip()
        if not source or len(source) > 100:
            raise ValidationError("Invalid workbench claim source")
        if source_target is not None:
            source_target = str(source_target).strip() or None
            if source_target is not None and len(source_target) > 150:
                raise ValidationError("Invalid workbench claim source target")
        if instagram_user_id is not None:
            instagram_user_id = str(instagram_user_id).strip()
            if not instagram_user_id or len(instagram_user_id) > 100 or instagram_user_id.casefold().startswith(("fb:", "fbid:")):
                raise ValidationError("Invalid Instagram user id")

        now = isoformat()
        with (nullcontext(_connection) if _connection is not None else self.database.write()) as connection:
            account_by_username = connection.execute(
                """
                SELECT account.*
                FROM instagram_username_aliases alias
                JOIN instagram_accounts account ON account.id=alias.account_id
                WHERE alias.username_norm=?
                """,
                (username_norm,),
            ).fetchone()
            if account_by_username is None:
                account_by_username = connection.execute(
                    "SELECT * FROM instagram_accounts WHERE current_username_norm=?",
                    (username_norm,),
                ).fetchone()
            account_by_id = None
            if instagram_user_id:
                account_by_id = connection.execute(
                    "SELECT * FROM instagram_accounts WHERE instagram_user_id=?",
                    (instagram_user_id,),
                ).fetchone()
            for matched_account in (account_by_username, account_by_id):
                if matched_account is not None:
                    assert_username_platform(None, matched_account["current_username_norm"])
            account = account_by_username or account_by_id
            globally_seen = bool(
                account
                and connection.execute(
                    "SELECT 1 FROM global_seen WHERE account_id=?", (account["id"],)
                ).fetchone()
            )
            existing_claim = (
                connection.execute(
                    "SELECT * FROM workbench_identity_claims WHERE account_id=?",
                    (account["id"],),
                ).fetchone()
                if account
                else None
            )
            already_claimed = existing_claim is not None

            if account and not globally_seen and (
                already_claimed or identity_has_durable_history(connection, account["id"])
            ):
                remember_registered_identity(
                    connection, account["id"], source,
                    existing_claim["claimed_by_user_id"] if existing_claim is not None else None,
                    now,
                )
                globally_seen = True
            if account_by_username is not None:
                # Restore the indexed alias without replacing anyone else's alias.
                connection.execute(
                    "INSERT OR IGNORE INTO instagram_username_aliases VALUES(?, ?, ?, ?)",
                    (account["id"], username_norm, account["first_seen_at"], now),
                )

            resumable_owned_claim = False
            if allow_owned_resume and account and existing_claim is not None:
                terminal = bool(
                    connection.execute(
                        """
                        SELECT 1 FROM workbench_candidates WHERE account_id=?
                        UNION ALL
                        SELECT 1 FROM task_results WHERE account_id=?
                        UNION ALL
                        SELECT 1 FROM task_result_duplicate_archive WHERE account_id=?
                        LIMIT 1
                        """,
                        (account["id"], account["id"], account["id"]),
                    ).fetchone()
                )
                if not terminal:
                    terminal = _has_historical_terminal_exclusion(connection, account["id"])
                resumable_owned_claim = bool(
                    not terminal
                    and existing_claim["claimed_by_user_id"] == owner_user_id
                    and existing_claim["source"] == source
                    and existing_claim["source_target"] == source_target
                )

            if resumable_owned_claim:
                remember_identity_owner(connection, account["id"], owner_user_id, now)
                total = _global_seen_total(connection)
                revision = int(
                    connection.execute(
                        "SELECT revision FROM workbench_state_revision WHERE singleton_id=1"
                    ).fetchone()[0]
                )
                return {
                    "outcome": "claimed",
                    "claimed": True,
                    "duplicate": False,
                    "resumed": True,
                    "claim_id": account["id"],
                    "account_id": account["id"],
                    "username": username_display,
                    "handle": f"@{username_display}",
                    "should_read_location": True,
                    "should_collect_profile": True,
                    "should_create_history": False,
                    "global_dedupe_count": total,
                    "snapshot_seq": revision,
                }

            if globally_seen or already_claimed:
                # A repeat may be the first observation carrying a stable ID.
                # Keep that evidence even though collection is skipped, otherwise
                # a later rename can be incorrectly claimed as a new identity.
                if (account_by_username is not None and instagram_user_id
                    and not account_by_username["instagram_user_id"] and account_by_id is None):
                    connection.execute("UPDATE instagram_accounts SET instagram_user_id=? WHERE id=?",
                                       (instagram_user_id, account_by_username["id"]))
                # A new username observed for an already-known stable IG id is an
                # alias, not a new person.  Recording the alias improves later
                # first-gate dedupe without creating history for this occurrence.
                if account_by_username is None and account_by_id is not None:
                    try:
                        connection.execute(
                            """
                            INSERT INTO instagram_username_aliases(
                                account_id, username_norm, first_seen_at, last_seen_at
                            ) VALUES(?, ?, ?, ?)
                            """,
                            (account["id"], username_norm, now, now),
                        )
                    except sqlite3.IntegrityError:
                        pass
                if globally_seen:
                    seen = connection.execute(
                        "SELECT sources_json FROM global_seen WHERE account_id=?",
                        (account["id"],),
                    ).fetchone()
                    sources = set(_loads(seen["sources_json"])) if seen else set()
                    sources.add(source)
                    connection.execute(
                        "UPDATE global_seen SET sources_json=?, last_seen_at=? WHERE account_id=?",
                        (_json(sorted(sources)), now, account["id"]),
                    )
                total = _global_seen_total(connection)
                return {
                    "outcome": "duplicate",
                    "claimed": False,
                    "duplicate": True,
                    "claim_id": None,
                    "account_id": account["id"],
                    "username": username_display,
                    "handle": f"@{username_display}",
                    "should_read_location": False,
                    "should_collect_profile": False,
                    "should_create_history": False,
                    "global_dedupe_count": total,
                }

            if account is None:
                account_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO instagram_accounts(
                        id, instagram_user_id, current_username_norm,
                        current_username_display, first_seen_at, last_seen_at
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        instagram_user_id,
                        username_norm,
                        username_display,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO instagram_username_aliases(
                        account_id, username_norm, first_seen_at, last_seen_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (account_id, username_norm, now, now),
                )
            else:
                account_id = account["id"]
                if instagram_user_id and not account["instagram_user_id"]:
                    connection.execute(
                        "UPDATE instagram_accounts SET instagram_user_id=? WHERE id=?",
                        (instagram_user_id, account_id),
                    )
                connection.execute(
                    "UPDATE instagram_accounts SET last_seen_at=? WHERE id=?",
                    (now, account_id),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO instagram_username_aliases(
                        account_id, username_norm, first_seen_at, last_seen_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (account_id, username_norm, now, now),
                )

            connection.execute(
                """
                INSERT INTO global_seen(account_id, sources_json, first_seen_at, last_seen_at)
                VALUES(?, ?, ?, ?)
                """,
                (account_id, _json([source]), now, now),
            )
            connection.execute(
                """
                INSERT INTO workbench_identity_claims(
                    account_id, claimed_by_user_id, source, source_target, claimed_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (account_id, owner_user_id, source, source_target, now),
            )
            remember_identity_owner(connection, account_id, owner_user_id, now)
            revision = _bump_workbench_revision(connection, now)
            total = _global_seen_total(connection)
        return {
            "outcome": "claimed",
            "claimed": True,
            "duplicate": False,
            "claim_id": account_id,
            "account_id": account_id,
            "username": username_display,
            "handle": f"@{username_display}",
            "should_read_location": True,
            "should_collect_profile": True,
            "should_create_history": False,
            "global_dedupe_count": total,
            "snapshot_seq": revision,
        }

    def confirm_workbench_identity(
        self,
        owner_user_id: str,
        *,
        claim_id: str,
        username: str,
        instagram_user_id: str,
    ) -> dict[str, Any]:
        """Bind a stable Instagram id after the username-first claim.

        Relationship dialogs expose a username before the target profile is opened,
        so the first gate cannot know the stable id.  Once the already-visible
        profile supplies it, this transaction either attaches it to the claimed
        account or conservatively reports a duplicate/conflict.  It never moves an
        alias, overwrites a different stable id, or merges immutable business rows.
        """

        username_norm, username_display = normalize_instagram_username(username)
        instagram_user_id = str(instagram_user_id).strip()
        if not instagram_user_id or len(instagram_user_id) > 100 or instagram_user_id.casefold().startswith(("fb:", "fbid:")):
            raise ValidationError("Invalid Instagram user id")
        now = isoformat()
        with self.database.write() as connection:
            claim = connection.execute(
                """
                SELECT claim.*, account.instagram_user_id,
                       account.current_username_display
                FROM workbench_identity_claims claim
                JOIN instagram_accounts account ON account.id=claim.account_id
                WHERE claim.account_id=? AND claim.claimed_by_user_id=?
                """,
                (claim_id, owner_user_id),
            ).fetchone()
            if claim is None:
                raise NotFoundError("Workbench identity claim not found")
            if connection.execute(
                """
                SELECT 1 FROM instagram_username_aliases
                WHERE account_id=? AND username_norm=?
                """,
                (claim_id, username_norm),
            ).fetchone() is None:
                raise ValidationError("Confirmed username does not match its claim")

            existing_id = claim["instagram_user_id"]
            stable_account = connection.execute(
                "SELECT * FROM instagram_accounts WHERE instagram_user_id=?",
                (instagram_user_id,),
            ).fetchone()
            assert_username_platform(None, claim["current_username_display"])
            if stable_account is not None:
                assert_username_platform(None, stable_account["current_username_norm"])
            conflict_reason: str | None = None
            canonical_account_id = claim_id
            if existing_id and existing_id != instagram_user_id:
                conflict_reason = "claim_has_different_stable_id"
                canonical_account_id = (
                    stable_account["id"] if stable_account is not None else claim_id
                )
            elif stable_account is not None and stable_account["id"] != claim_id:
                conflict_reason = "stable_id_belongs_to_another_account"
                canonical_account_id = stable_account["id"]

            if conflict_reason is not None:
                # A username-first claim can discover, from the already-visible
                # profile, that it is merely a renamed alias of an existing stable
                # Instagram id.  In that exact case the temporary account has no
                # business history yet, so keeping it would permanently inflate the
                # global dedupe count.  Move only the newly observed alias/source to
                # the canonical identity and remove the empty placeholder.  Claims
                # that already carry a different stable id are deliberately left
                # untouched because they are not safe to merge automatically.
                collapsed_placeholder = bool(
                    existing_id is None
                    and stable_account is not None
                    and stable_account["id"] != claim_id
                    and connection.execute(
                        """
                        SELECT NOT EXISTS(
                            SELECT 1 FROM workbench_candidates WHERE account_id=?
                        ) AND NOT EXISTS(
                            SELECT 1 FROM workbench_collection_exclusions WHERE account_id=?
                        ) AND NOT EXISTS(
                            SELECT 1 FROM task_results WHERE account_id=?
                        ) AND NOT EXISTS(
                            SELECT 1 FROM task_result_duplicate_archive WHERE account_id=?
                        )
                        """,
                        (claim_id, claim_id, claim_id, claim_id),
                    ).fetchone()[0]
                )
                if collapsed_placeholder:
                    merge_identity_owners(connection, claim_id, canonical_account_id)
                    placeholder_seen = connection.execute(
                        "SELECT sources_json FROM global_seen WHERE account_id=?",
                        (claim_id,),
                    ).fetchone()
                    canonical_seen = connection.execute(
                        "SELECT sources_json FROM global_seen WHERE account_id=?",
                        (canonical_account_id,),
                    ).fetchone()
                    sources = set(
                        _loads(placeholder_seen["sources_json"])
                        if placeholder_seen is not None
                        else []
                    )
                    sources.update(
                        _loads(canonical_seen["sources_json"])
                        if canonical_seen is not None
                        else []
                    )
                    if canonical_seen is None:
                        connection.execute(
                            """
                            INSERT INTO global_seen(
                                account_id, sources_json, first_seen_at, last_seen_at
                            ) VALUES(?, ?, ?, ?)
                            """,
                            (canonical_account_id, _json(sorted(sources)), now, now),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE global_seen
                            SET sources_json=?, last_seen_at=?
                            WHERE account_id=?
                            """,
                            (_json(sorted(sources)), now, canonical_account_id),
                        )
                    connection.execute(
                        "DELETE FROM workbench_identity_claims WHERE account_id=?",
                        (claim_id,),
                    )
                    connection.execute(
                        "DELETE FROM global_seen WHERE account_id=?", (claim_id,)
                    )
                    connection.execute(
                        "DELETE FROM instagram_username_aliases WHERE account_id=?",
                        (claim_id,),
                    )
                    connection.execute(
                        "DELETE FROM instagram_accounts WHERE id=?", (claim_id,)
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO instagram_username_aliases(
                            account_id, username_norm, first_seen_at, last_seen_at
                        ) VALUES(?, ?, ?, ?)
                        """,
                        (canonical_account_id, username_norm, now, now),
                    )
                    revision = _bump_workbench_revision(connection, now)
                else:
                    revision = int(
                        connection.execute(
                            "SELECT revision FROM workbench_state_revision WHERE singleton_id=1"
                        ).fetchone()[0]
                    )
                _event(
                    connection,
                    owner_user_id,
                    "workbench_identity",
                    claim_id,
                    "identity.stable_id_conflict",
                    {
                        "username": username_norm,
                        "instagram_user_id": instagram_user_id,
                        "canonical_account_id": canonical_account_id,
                        "reason": conflict_reason,
                        "action": (
                            "collapse_empty_alias_claim"
                            if collapsed_placeholder
                            else "skip_without_merge"
                        ),
                    },
                )
                return {
                    "outcome": "duplicate",
                    "confirmed": False,
                    "duplicate": True,
                    "claim_id": claim_id,
                    "account_id": canonical_account_id,
                    "username": username_display,
                    "instagram_user_id": instagram_user_id,
                    "reason": conflict_reason,
                    "placeholder_removed": collapsed_placeholder,
                    "should_continue": False,
                    "snapshot_seq": revision,
                }

            changed = existing_id is None
            if changed:
                connection.execute(
                    """
                    UPDATE instagram_accounts
                    SET instagram_user_id=?, last_seen_at=?
                    WHERE id=? AND instagram_user_id IS NULL
                    """,
                    (instagram_user_id, now, claim_id),
                )
                revision = _bump_workbench_revision(connection, now)
                _event(
                    connection,
                    owner_user_id,
                    "workbench_identity",
                    claim_id,
                    "identity.stable_id_bound",
                    {
                        "username": username_norm,
                        "instagram_user_id": instagram_user_id,
                    },
                )
            else:
                revision = int(
                    connection.execute(
                        "SELECT revision FROM workbench_state_revision WHERE singleton_id=1"
                    ).fetchone()[0]
                )
            return {
                "outcome": "confirmed",
                "confirmed": True,
                "duplicate": False,
                "claim_id": claim_id,
                "account_id": claim_id,
                "username": username_display,
                "instagram_user_id": instagram_user_id,
                "stable_id_bound": changed,
                "should_continue": True,
                "snapshot_seq": revision,
            }

    def create_workbench_candidate(
        self,
        owner_user_id: str,
        *,
        claim_id: str,
        username: str | None = None,
        visibility: str,
        profile: dict[str, Any],
        screening: dict[str, Any],
        review_cache: dict[str, Any],
        source_mode: str | None = None,
        source_target: str | None = None,
    ) -> dict[str, Any]:
        if visibility not in {"public", "private"}:
            raise ValidationError("A review candidate must be public or private")
        if visibility == "private":
            # Keep an old worker's routing evidence, but never recreate the retired
            # secondary lane. This changes neither a human decision nor identity.
            screening = _normalize_private_review_screening(screening)
        public_requires_exclusion = _review_requires_exclusion(visibility, profile, screening)
        if public_requires_exclusion:
            # Public secondary review is retired. Keep this guard at the durable API
            # boundary as well as in ExecutionManager so an older renderer/worker
            # cannot recreate the lane after startup migrated its historical rows.
            raise ValidationError(
                "Public accounts with failed/unconfirmed deterministic conditions "
                "(except unknown location or confirmed zero-post activity) must be "
                "collection-excluded"
            )
        stable_id = profile.get("instagram_user_id") if isinstance(profile, dict) else None
        if stable_id is not None:
            confirmation = self.confirm_workbench_identity(
                owner_user_id,
                claim_id=claim_id,
                username=username or str(profile.get("username") or ""),
                instagram_user_id=str(stable_id),
            )
            if confirmation["duplicate"]:
                raise ConflictError(
                    "Candidate stable id belongs to another global identity",
                    details=confirmation,
                )
        requested_username_norm = (
            normalize_instagram_username(username)[0] if username is not None else None
        )
        profile, screening = normalize_person_category_fields(profile, screening)
        profile_json = _bounded_workbench_json(
            profile, label="profile", max_bytes=WORKBENCH_PROFILE_MAX_BYTES
        )
        screening_json = _bounded_workbench_json(
            screening, label="screening", max_bytes=WORKBENCH_SCREENING_MAX_BYTES
        )
        # Manual review is avatar-first for both visibility lanes. Never persist
        # post screenshots at this boundary, including payloads from an older or
        # custom worker; the avatar thumbnail remains useful for review.
        review_cache = {
            "avatar_preview": review_cache.get("avatar_preview")
        } if isinstance(review_cache.get("avatar_preview"), str) else {}
        # Review screenshots are intentionally short-lived and erased atomically
        # after approve/reject.  Their size must never become a collection or
        # business-storage quota: degrade the optional cache before insertion,
        # while durable profile/screening objects keep their strict validation.
        review_cache_json = _disposable_review_cache_json(review_cache)
        review_cache_bytes = max(0, len(review_cache_json.encode("utf-8")) - 2)
        if source_mode is not None:
            source_mode = str(source_mode).strip() or None
            if source_mode is not None and len(source_mode) > 50:
                raise ValidationError("Invalid candidate source mode")
        if source_target is not None:
            source_target = str(source_target).strip() or None
            if source_target is not None and len(source_target) > 150:
                raise ValidationError("Invalid candidate source target")

        now = isoformat()
        with self.database.write() as connection:
            claim = connection.execute(
                """
                SELECT claim.*, account.current_username_display,
                       account.instagram_user_id
                FROM workbench_identity_claims claim
                JOIN instagram_accounts account ON account.id=claim.account_id
                WHERE claim.account_id=? AND claim.claimed_by_user_id=?
                """,
                (claim_id, owner_user_id),
            ).fetchone()
            if claim is None:
                raise NotFoundError("Workbench identity claim not found")
            if requested_username_norm is not None:
                matching_alias = connection.execute(
                    """
                    SELECT 1 FROM instagram_username_aliases
                    WHERE account_id=? AND username_norm=?
                    """,
                    (claim_id, requested_username_norm),
                ).fetchone()
                if matching_alias is None:
                    raise ValidationError("Candidate username does not match its claim")
            if connection.execute(
                "SELECT 1 FROM workbench_collection_exclusions WHERE account_id=?",
                (claim_id,),
            ).fetchone():
                raise ConflictError("Excluded identity cannot enter review")
            existing = connection.execute(
                """
                SELECT candidate.*, account.instagram_user_id,
                       account.current_username_display
                FROM workbench_candidates candidate
                JOIN instagram_accounts account ON account.id=candidate.account_id
                WHERE candidate.account_id=?
                """,
                (claim_id,),
            ).fetchone()
            if existing is not None:
                if existing["owner_user_id"] != owner_user_id:
                    raise NotFoundError("Workbench identity claim not found")
                if existing["visibility"] != visibility:
                    raise ConflictError("Candidate visibility is already fixed")
                return self._workbench_candidate_dict(existing)

            cache_usage = connection.execute(
                """
                SELECT pending_bytes
                FROM workbench_cache_usage
                WHERE owner_user_id=?
                """,
                (owner_user_id,),
            ).fetchone()
            current_cache_bytes = (
                int(cache_usage["pending_bytes"] or 0)
                if cache_usage is not None
                else 0
            )
            if current_cache_bytes + review_cache_bytes > WORKBENCH_PENDING_CACHE_BUDGET_BYTES:
                # Review backlog is never allowed to stop collection. Inline
                # screenshots are a convenience cache, so shed them for the incoming
                # row once the bounded allowance is full. Keep lightweight metadata
                # when it fits; durable profile/screening fields are unaffected.
                review_cache_json = _disposable_review_cache_json(
                    review_cache,
                    shed_previews=True,
                )
                review_cache_bytes = max(
                    0, len(review_cache_json.encode("utf-8")) - 2
                )
                if (
                    current_cache_bytes + review_cache_bytes
                    > WORKBENCH_PENDING_CACHE_BUDGET_BYTES
                ):
                    # Remote URLs and other optional metadata may be large even
                    # after inline images are removed.  Drop the cache only; the
                    # pending candidate must still be created.
                    review_cache_json = "{}"

            candidate_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO workbench_candidates(
                    id, owner_user_id, account_id, visibility, status,
                    profile_json, screening_json, review_cache_json, source_mode,
                    source_target, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    owner_user_id,
                    claim_id,
                    visibility,
                    profile_json,
                    screening_json,
                    review_cache_json,
                    source_mode,
                    source_target,
                    now,
                    now,
                ),
            )
            _bump_workbench_revision(connection, now)
            row = connection.execute(
                """
                SELECT candidate.*, account.instagram_user_id,
                       account.current_username_display
                FROM workbench_candidates candidate
                JOIN instagram_accounts account ON account.id=candidate.account_id
                WHERE candidate.id=?
                """,
                (candidate_id,),
            ).fetchone()
        return self._workbench_candidate_dict(row)

    def record_workbench_exclusion(
        self,
        owner_user_id: str,
        *,
        claim_id: str,
        username: str | None = None,
        reason_code: str,
        reason: str,
        location_country: str | None,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        stable_id = profile.get("instagram_user_id") if isinstance(profile, dict) else None
        if stable_id is not None:
            confirmation = self.confirm_workbench_identity(
                owner_user_id,
                claim_id=claim_id,
                username=username or str(profile.get("username") or ""),
                instagram_user_id=str(stable_id),
            )
            if confirmation["duplicate"]:
                raise ConflictError(
                    "Excluded profile stable id belongs to another global identity",
                    details=confirmation,
                )
        requested_username_norm = (
            normalize_instagram_username(username)[0] if username is not None else None
        )
        reason_code = str(reason_code).strip().casefold()
        if reason_code == "non_us":
            reason_code = "non_us_location"
        reason = str(reason).strip()
        if not reason_code or len(reason_code) > 100:
            raise ValidationError("Invalid exclusion reason code")
        if not reason or len(reason) > 500:
            raise ValidationError("Invalid exclusion reason")
        if location_country is not None:
            location_country = str(location_country).strip() or None
            if location_country is not None and len(location_country) > 100:
                raise ValidationError("Invalid exclusion country")
        profile_json = _bounded_workbench_json(
            profile, label="profile", max_bytes=WORKBENCH_PROFILE_MAX_BYTES
        )
        now = isoformat()
        with self.database.write() as connection:
            claim = connection.execute(
                """
                SELECT claim.*, account.current_username_display
                FROM workbench_identity_claims claim
                JOIN instagram_accounts account ON account.id=claim.account_id
                WHERE claim.account_id=? AND claim.claimed_by_user_id=?
                """,
                (claim_id, owner_user_id),
            ).fetchone()
            if claim is None:
                raise NotFoundError("Workbench identity claim not found")
            remember_registered_identity(
                connection, claim_id, "collection_excluded", owner_user_id, now
            )
            if requested_username_norm is not None:
                matching_alias = connection.execute(
                    """
                    SELECT 1 FROM instagram_username_aliases
                    WHERE account_id=? AND username_norm=?
                    """,
                    (claim_id, requested_username_norm),
                ).fetchone()
                if matching_alias is None:
                    raise ValidationError("Exclusion username does not match its claim")
            if connection.execute(
                "SELECT 1 FROM workbench_candidates WHERE account_id=?",
                (claim_id,),
            ).fetchone():
                raise ConflictError("Review candidate cannot be collection-excluded")
            existing = connection.execute(
                "SELECT * FROM workbench_collection_exclusions WHERE account_id=?",
                (claim_id,),
            ).fetchone()
            if existing is not None:
                if existing["owner_user_id"] != owner_user_id:
                    raise NotFoundError("Workbench identity claim not found")
                result = self._workbench_exclusion_dict(existing)
                result["outcome"] = (
                    "excluded_non_us"
                    if existing["reason_code"] == "non_us_location"
                    else "excluded"
                )
                return result

            exclusion_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO workbench_collection_exclusions(
                    id, account_id, owner_user_id, username_display, reason_code,
                    reason, location_country, profile_snapshot_json, excluded_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exclusion_id,
                    claim_id,
                    owner_user_id,
                    claim["current_username_display"],
                    reason_code,
                    reason,
                    location_country,
                    profile_json,
                    now,
                ),
            )
            revision = _bump_workbench_revision(connection, now)
            row = connection.execute(
                "SELECT * FROM workbench_collection_exclusions WHERE id=?",
                (exclusion_id,),
            ).fetchone()
        result = self._workbench_exclusion_dict(row)
        result["outcome"] = (
            "excluded_non_us" if reason_code == "non_us_location" else "excluded"
        )
        result["continue_collection"] = False
        result["snapshot_seq"] = revision
        return result

    def decide_workbench_candidate(
        self,
        owner_user_id: str,
        *,
        candidate_id: str,
        decision: str,
        platform: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValidationError("Invalid review decision")
        now = isoformat()
        validate_platform(platform)
        with self.database.write() as connection:
            assert_candidate_platforms(connection, owner_user_id, [candidate_id], platform)
            row = connection.execute(
                """
                SELECT candidate.*, account.instagram_user_id,
                       account.current_username_display
                FROM workbench_candidates candidate
                JOIN instagram_accounts account ON account.id=candidate.account_id
                WHERE candidate.id=? AND candidate.owner_user_id=?
                """,
                (candidate_id, owner_user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("Review candidate not found")
            if row["status"] != "pending":
                if row["status"] != decision:
                    raise ConflictError("Review decision is already final")
                revision = int(
                    connection.execute(
                        "SELECT revision FROM workbench_state_revision WHERE singleton_id=1"
                    ).fetchone()[0]
                )
                candidate = self._workbench_candidate_dict(row)
                if decision == "rejected":
                    immutable = connection.execute(
                        """
                        SELECT profile_snapshot_json, screening_snapshot_json
                        FROM workbench_review_decisions
                        WHERE candidate_id=? AND owner_user_id=? AND decision='rejected'
                        """,
                        (candidate_id, owner_user_id),
                    ).fetchone()
                    if immutable is not None:
                        profile = _loads(immutable["profile_snapshot_json"])
                        screening = _loads(immutable["screening_snapshot_json"])
                        candidate["profile"] = profile if isinstance(profile, dict) else {}
                        candidate["screening"] = (
                            screening if isinstance(screening, dict) else {}
                        )
                destination = (
                    f"approved_{row['visibility']}"
                    if decision == "approved"
                    else "manual_rejections"
                )
                return {
                    "candidate_id": candidate_id,
                    "decision": decision,
                    "visibility": row["visibility"],
                    "destination": destination,
                    "bucket": (
                        f"approved_{row['visibility']}"
                        if decision == "approved"
                        else "manual_rejections"
                    ),
                    "reviewed_at": row["reviewed_at"],
                    "snapshot_seq": revision,
                    "candidate": candidate,
                }

            decision_id = str(uuid.uuid4())
            current_review_cache = _loads(row["review_cache_json"])
            retained_approved_cache: dict[str, Any] = {}
            if decision == "approved" and isinstance(current_review_cache, dict):
                avatar_preview = current_review_cache.get("avatar_preview")
                if isinstance(avatar_preview, str) and avatar_preview.startswith("data:image/"):
                    retained_approved_cache["avatar_preview"] = avatar_preview
            retained_review_cache_json = json.dumps(
                retained_approved_cache,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            decision_profile_json = row["profile_json"] if decision == "rejected" else "{}"
            decision_screening_json = row["screening_json"] if decision == "rejected" else "{}"
            connection.execute(
                """
                INSERT INTO workbench_review_decisions(
                    id, candidate_id, owner_user_id, decision, visibility,
                    profile_snapshot_json, screening_snapshot_json, decided_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    candidate_id,
                    owner_user_id,
                    decision,
                    row["visibility"],
                    decision_profile_json,
                    decision_screening_json,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE workbench_candidates
                SET status=?,
                    profile_json=CASE WHEN ?='rejected' THEN '{}' ELSE profile_json END,
                    screening_json=CASE WHEN ?='rejected' THEN '{}' ELSE screening_json END,
                    review_cache_json=?, reviewed_at=?, updated_at=?
                WHERE id=? AND status='pending'
                """,
                (
                    decision,
                    decision,
                    decision,
                    retained_review_cache_json,
                    now,
                    now,
                    candidate_id,
                ),
            )
            revision = _bump_workbench_revision(connection, now)
            row = connection.execute(
                """
                SELECT candidate.*, account.instagram_user_id,
                       account.current_username_display
                FROM workbench_candidates candidate
                JOIN instagram_accounts account ON account.id=candidate.account_id
                WHERE candidate.id=?
                """,
                (candidate_id,),
            ).fetchone()
        candidate = self._workbench_candidate_dict(row)
        if decision == "rejected":
            rejected_profile = _loads(decision_profile_json)
            rejected_screening = _loads(decision_screening_json)
            candidate["profile"] = (
                rejected_profile if isinstance(rejected_profile, dict) else {}
            )
            candidate["screening"] = (
                rejected_screening if isinstance(rejected_screening, dict) else {}
            )
        destination = (
            f"approved_{row['visibility']}"
            if decision == "approved"
            else "manual_rejections"
        )
        return {
            "candidate_id": candidate_id,
            "decision": decision,
            "visibility": row["visibility"],
            "destination": destination,
            "bucket": (
                f"approved_{row['visibility']}"
                if decision == "approved"
                else "manual_rejections"
            ),
            "reviewed_at": now,
            "snapshot_seq": revision,
            "candidate": candidate,
        }

    def query_review_queue(self, owner_user_id: str, **options: Any) -> dict[str, Any]:
        from .review_layers import query_review_queue
        return query_review_queue(self, owner_user_id, **options)

    def move_review_stage(self, owner_user_id: str, **options: Any) -> dict[str, Any]:
        from .review_layers import move_review_stage
        return move_review_stage(self, owner_user_id, **options)

    def guard_platform_command(self, owner_user_id: str, command: str,
                               payload: dict[str, Any], platform: str | None) -> None:
        """Reject crossed immutable IDs before any command side effect.

        Mutating candidate services repeat the check in their write transaction.
        Task settings and identity namespaces cannot change platform in place;
        this owner-scoped read fence is also safe before async manager controls.
        """
        validate_platform(platform)
        with self.database.read() as connection:
            connection.execute("BEGIN")
            task_id = payload.get("task_id")
            if isinstance(task_id, str):
                task = self._owned_task(connection, owner_user_id, task_id)
                if collection_platform(_loads(task["settings_json"])) != (platform or "instagram"):
                    raise ConflictError("所选任务不属于当前平台，请刷新后重新选择",
                                        details={"reason": "platform_scope_mismatch", "platform": platform})
            if command in {"review_decision", "review_stage_move", "approved_candidate_dismiss"}:
                ids = payload.get("candidate_ids") or [payload.get("candidate_id")]
                if isinstance(ids, list) and all(isinstance(item, str) for item in ids):
                    assert_candidate_platforms(connection, owner_user_id, ids, platform)
            if command.startswith("split_"):
                ids = payload.get("candidate_ids") or [payload.get("candidate_id")]
                if isinstance(ids, list):
                    for candidate_id in ids:
                        if not isinstance(candidate_id, str):
                            continue
                        rows = connection.execute(
                            "SELECT username_norm FROM split_candidates WHERE owner_user_id=? AND id=? "
                            "UNION ALL SELECT username_norm FROM split_candidate_history WHERE owner_user_id=? AND id=? "
                            "UNION ALL SELECT username_norm FROM task_target_recovery_controls WHERE owner_user_id=? AND candidate_id=?",
                            (owner_user_id, candidate_id, owner_user_id, candidate_id, owner_user_id, candidate_id),
                        )
                        for row in rows:
                            assert_username_platform(platform, row["username_norm"])
                for item in payload.get("candidates", []) if isinstance(payload.get("candidates"), list) else []:
                    if isinstance(item, dict) and isinstance(item.get("username"), str):
                        assert_username_platform(platform, item["username"])
            if command in {"dedupe_claim", "create_candidate", "record_exclusion"}:
                username = payload.get("username")
                if isinstance(username, str):
                    assert_username_platform(platform, username)
                claim_id = payload.get("claim_id") or payload.get("claim_account_id")
                if isinstance(claim_id, str):
                    row = connection.execute(
                        "SELECT account.current_username_norm FROM workbench_identity_claims claim "
                        "JOIN instagram_accounts account ON account.id=claim.account_id "
                        "WHERE claim.account_id=? AND claim.claimed_by_user_id=?", (claim_id, owner_user_id),
                    ).fetchone()
                    if row:
                        assert_username_platform(platform, row["current_username_norm"])

    def get_workbench_dedupe_stats(self, owner_user_id: str, *, platform: str | None = None) -> dict[str, int]:
        candidate_scope = account_platform_sql(platform, 'workbench_candidates.account_id')
        exclusion_scope = username_platform_sql(platform, 'username_display')
        with self.database.read() as connection:
            total = global_seen_platform_total(connection, platform)
            claimed = int(
                connection.execute("SELECT COUNT(*) FROM workbench_identity_claims WHERE 1=1 " + account_platform_sql(platform, "workbench_identity_claims.account_id")).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT status, COUNT(*) AS count
                FROM workbench_candidates
                WHERE owner_user_id=? {candidate_scope}
                GROUP BY status
                """,
                (owner_user_id,),
            ).fetchall()
            status_counts = {row["status"]: int(row["count"]) for row in rows}
            excluded = int(
                connection.execute(
                    "SELECT COUNT(*) FROM workbench_collection_exclusions WHERE owner_user_id=?" + exclusion_scope,
                    (owner_user_id,),
                ).fetchone()[0]
            )
        return {
            "total": total,
            "claimed": claimed,
            "pending_review": status_counts.get("pending", 0),
            "approved": status_counts.get("approved", 0),
            "rejected": status_counts.get("rejected", 0),
            "collection_excluded": excluded,
        }

    def dismiss_approved_candidate(
        self, owner_user_id: str, candidate_id: str, *, mark_used: bool = False, platform: str | None = None
    ) -> dict[str, Any]:
        """Soft-remove one approved account while retaining every durable ledger.

        Pending/paused actions for any known username alias become visible
        failures.  A running click cannot be killed safely, so it receives a
        deferred cancellation fence; a later confirmed success still wins.
        """

        if mark_used is not False:
            raise ValidationError("Instagram 审核不支持手动标记已使用")
        now = isoformat()
        validate_platform(platform)
        with self.database.write() as connection:
            assert_candidate_platforms(connection, owner_user_id, [candidate_id], platform)
            candidate = connection.execute(
                """
                SELECT candidate.*, account.current_username_display
                FROM workbench_candidates candidate
                JOIN instagram_accounts account ON account.id=candidate.account_id
                WHERE candidate.id=? AND candidate.owner_user_id=?
                """,
                (candidate_id, owner_user_id),
            ).fetchone()
            if candidate is None:
                raise NotFoundError("Approved candidate not found")
            if candidate["status"] != "approved":
                raise ConflictError(
                    "Only an approved candidate can be removed from the queue",
                    details={"status": candidate["status"]},
                )
            existing = connection.execute(
                """
                SELECT * FROM workbench_candidate_dismissals
                WHERE candidate_id=? AND owner_user_id=?
                """,
                (candidate_id, owner_user_id),
            ).fetchone()
            if existing is not None:
                revision = int(
                    connection.execute(
                        "SELECT revision FROM workbench_state_revision WHERE singleton_id=1"
                    ).fetchone()[0]
                )
                return {
                    "candidate_id": candidate_id,
                    "status": "dismissed",
                    "dismissed_at": existing["dismissed_at"],
                    "global_dedupe_retained": True,
                    "affected_targets": [],
                    "snapshot_seq": revision,
                }

            target_rows = connection.execute(
                """
                SELECT target.id, target.status, target.control_after_attempt,
                       campaign.id AS campaign_id
                FROM action_targets target
                JOIN action_campaigns campaign ON campaign.id=target.campaign_id
                WHERE campaign.owner_user_id=?
                  AND target.status IN ('pending', 'paused', 'running')
                  AND EXISTS(
                      SELECT 1 FROM instagram_username_aliases alias
                      WHERE alias.account_id=?
                        AND alias.username_norm=target.username_norm
                  )
                ORDER BY campaign.created_at, target.queue_order
                """,
                (owner_user_id, candidate["account_id"]),
            ).fetchall()
            affected_targets: list[dict[str, Any]] = []
            for target in target_rows:
                if target["status"] == "running":
                    connection.execute(
                        """
                        UPDATE action_targets
                        SET control_after_attempt='cancel', updated_at=?
                        WHERE id=? AND status='running'
                        """,
                        (now, target["id"]),
                    )
                    affected_targets.append(
                        {
                            "campaign_id": target["campaign_id"],
                            "target_id": target["id"],
                            "from": "running",
                            "to": "running",
                            "deferred": True,
                            "deferred_action": "cancel",
                        }
                    )
                else:
                    connection.execute(
                        """
                        UPDATE action_targets
                        SET status='failed', control_after_attempt=NULL,
                            last_error=?, updated_at=?
                        WHERE id=? AND status IN ('pending', 'paused')
                        """,
                        (
                            "Approved account was removed before the action completed",
                            now,
                            target["id"],
                        ),
                    )
                    affected_targets.append(
                        {
                            "campaign_id": target["campaign_id"],
                            "target_id": target["id"],
                            "from": target["status"],
                            "to": "failed",
                            "deferred": False,
                        }
                    )

            dismissal_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO workbench_candidate_dismissals(
                    id, candidate_id, owner_user_id, dismissed_at
                ) VALUES(?, ?, ?, ?)
                """,
                (dismissal_id, candidate_id, owner_user_id, now),
            )
            revision = _bump_workbench_revision(connection, now)
            _event(
                connection,
                owner_user_id,
                "workbench_candidate",
                candidate_id,
                "candidate.dismissed",
                {
                    "account_id": candidate["account_id"],
                    "affected_targets": affected_targets,
                    "global_dedupe_retained": True,
                },
            )
        return {
            "candidate_id": candidate_id,
            "username": candidate["current_username_display"],
            "visibility": candidate["visibility"],
            "status": "dismissed",
            "dismissed_at": now,
            "global_dedupe_retained": True,
            "affected_targets": affected_targets,
            "snapshot_seq": revision,
        }

    def get_workbench_revision(self) -> int:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT revision FROM workbench_state_revision WHERE singleton_id=1"
            ).fetchone()
        return int(row["revision"])

    def advance_workbench_revision(self) -> int:
        now = isoformat()
        with self.database.write() as connection:
            return _bump_workbench_revision(connection, now)

    def clear_workbench_cache(self, owner_user_id: str) -> dict[str, Any]:
        """Clear terminal review previews without touching live review evidence."""

        now = isoformat()
        with self.database.write() as connection:
            row = connection.execute(
                """
                SELECT terminal_entries AS entries, terminal_bytes AS bytes
                FROM workbench_cache_usage
                WHERE owner_user_id=?
                """,
                (owner_user_id,),
            ).fetchone()
            cleared_entries = int(row["entries"] or 0) if row is not None else 0
            cleared_bytes = int(row["bytes"] or 0) if row is not None else 0
            connection.execute(
                """
                UPDATE workbench_candidates
                SET review_cache_json='{}'
                WHERE owner_user_id=? AND status!='pending' AND review_cache_json!='{}'
                """,
                (owner_user_id,),
            )
            # ``workbench_state_revision.last_cleanup_at`` is the global
            # automatic-maintenance watermark.  An owner-scoped manual cache
            # clear must never advance it: doing so would let repeated clicks
            # postpone diagnostic/session/spool maintenance for every user.
            revision = _bump_workbench_revision(connection, now)
        return {
            "cleared_entries": cleared_entries,
            "cleared_bytes": cleared_bytes,
            "last_cleanup_at": now,
            "business_records_retained": True,
            "retained": {
                "global_dedupe": True,
                "review_history": True,
                "collection_exclusions": True,
                "action_success_history": True,
                "live_task_checkpoints": True,
                "pending_review_previews": True,
            },
            "snapshot_seq": revision,
        }

    def get_storage_status(self) -> dict[str, Any]:
        """Read filesystem telemetry without a business snapshot or write lock."""
        database_path = self.database.path
        result: dict[str, Any] = {
            "disk_probe_available": False, "file_probe_available": True,
            "low_space_warning": None,
        }
        for key, path in (
            ("database_bytes", database_path),
            ("wal_bytes", database_path.with_name(f"{database_path.name}-wal")),
        ):
            try:
                # A WAL can disappear at a checkpoint between exists() and stat().
                # Probe once; absent is zero, unreadable is explicitly unknown.
                result[key] = path.stat().st_size
            except FileNotFoundError:
                result[key] = 0
            except OSError:
                result["file_probe_available"] = False
        try:
            total, used, free = shutil.disk_usage(database_path.parent)
        except OSError:
            return result
        threshold = max(1024**3, int(total * .05))
        result.update(disk_probe_available=True, disk_total_bytes=total,
                      disk_used_bytes=used, disk_free_bytes=free,
                      low_space_threshold_bytes=threshold,
                      low_space_warning=bool(total and free < threshold))
        return result

    def get_workbench_snapshot(
        self,
        owner_user_id: str,
        *,
        limit: int = 500,
        history_limit: int = 500,
        maintain: bool = True,
        platform: str | None = None,
    ) -> dict[str, Any]:
        """Read all workbench buckets from one explicit SQLite snapshot."""

        validate_platform(platform)
        candidate_scope = account_platform_sql(platform, 'candidate.account_id')
        account_scope = username_platform_sql(platform, 'account.current_username_norm')
        exclusion_scope = username_platform_sql(platform, 'username_display')
        action_scope = username_platform_sql(platform, 'success.username_norm')
        exclusion_page_hint = 'INDEXED BY idx_exclusions_platform_page' if platform is not None else ''
        limit = max(1, min(int(limit), 5000))
        history_limit = max(1, min(int(history_limit), 5000))
        # HTTP snapshots opt out; the app lifecycle owns periodic maintenance.
        # Keep synchronous maintenance for standalone service callers.
        if maintain:
            self.database.maintain_transient_data()
        with self.database.read() as connection:
            # Database connections use autocommit.  BEGIN is therefore required
            # to prevent a decision racing between the bucket and count queries.
            connection.execute("BEGIN")
            revision_row = connection.execute(
                """
                SELECT revision, updated_at, last_cleanup_at
                FROM workbench_state_revision WHERE singleton_id=1
                """
            ).fetchone()
            global_total = global_seen_platform_total(connection, platform)
            from .workbench_aggregates import snapshot_totals
            totals = snapshot_totals(connection, owner_user_id)
            claimed_total = totals.get("claimed", 0)
            total_collected = totals.get("total_collected", 0)
            total_public = totals.get("total_public", 0)
            total_private = totals.get("total_private", 0)
            total_split = totals.get("total_split", 0)
            grouped = {
                (status, visibility): totals.get(f"candidate:{status}:{visibility}", 0)
                for status in ("pending", "approved", "rejected")
                for visibility in ("public", "private")
            }
            pending_private_total = grouped.get(("pending", "private"), 0)
            # Compatibility fields remain for older clients; there is one private
            # review queue, counted from all durable rows rather than the page cap.
            pending_private_secondary = 0
            pending_private_primary = pending_private_total
            dismissed_total = totals.get("approved_dismissed", 0)
            greet_successes = totals.get("success:greet", 0)
            follow_successes = totals.get("success:follow", 0)
            action_success_total = greet_successes + follow_successes
            exclusion_total = totals.get("collection_excluded", 0)
            cache_usage = connection.execute(
                """
                SELECT pending_entries, pending_bytes,
                       terminal_entries, terminal_bytes
                FROM workbench_cache_usage
                WHERE owner_user_id=?
                """,
                (owner_user_id,),
            ).fetchone()
            cache_bytes = (
                int(cache_usage["terminal_bytes"] or 0)
                if cache_usage is not None
                else 0
            )
            pending_preview_bytes = (
                int(cache_usage["pending_bytes"] or 0)
                if cache_usage is not None
                else 0
            )
            pending_cache_entries = (
                int(cache_usage["pending_entries"] or 0)
                if cache_usage is not None
                else 0
            )
            event_log_row = connection.execute(
                """
                SELECT COALESCE(SUM(entries), 0) AS entries,
                       COALESCE(SUM(bytes), 0) AS bytes
                FROM event_log_usage WHERE owner_user_id=?
                """,
                (owner_user_id,),
            ).fetchone()

            def candidate_rows(status: str, visibility: str) -> list[sqlite3.Row]:
                if status == "approved":
                    # The exact eligibility projection skips dismissed/completed
                    # history before LIMIT, so even an exhausted queue is bounded.
                    return connection.execute(
                        """SELECT candidate.*, account.instagram_user_id,
                                  account.current_username_display
                           FROM workbench_actionable_candidates eligible
                                INDEXED BY idx_workbench_actionable_page
                           CROSS JOIN workbench_candidates candidate
                             ON candidate.id=eligible.candidate_id
                           CROSS JOIN instagram_accounts account
                             ON account.id=candidate.account_id
                           WHERE eligible.owner_user_id=? AND eligible.visibility=?
                           ORDER BY eligible.reviewed_at DESC, eligible.candidate_id DESC
                           LIMIT ?""", (owner_user_id, visibility, limit),
                    ).fetchall()
                return connection.execute(
                    f"""SELECT candidate.*, account.instagram_user_id,
                               account.current_username_display
                        FROM workbench_candidates candidate
                             INDEXED BY idx_candidates_platform_created
                        JOIN instagram_accounts account ON account.id=candidate.account_id
                        WHERE candidate.owner_user_id=? {candidate_scope}
                          AND candidate.status=? AND candidate.visibility=?
                        ORDER BY candidate.created_at ASC, candidate.id ASC
                        LIMIT ?""", (owner_user_id, status, visibility, limit),
                ).fetchall()

            approved_public_count = totals.get("approved:public", 0)
            approved_private_count = totals.get("approved:private", 0)

            pending_public = [
                self._avatar_only_review_candidate(
                    self._workbench_candidate_dict(row)
                )
                for row in candidate_rows("pending", "public")
            ]
            pending_private = [
                self._workbench_candidate_dict(row)
                for row in candidate_rows("pending", "private")
            ]
            approved_public = [
                self._workbench_candidate_dict(row)
                for row in candidate_rows("approved", "public")
            ]
            approved_private = [
                self._workbench_candidate_dict(row)
                for row in candidate_rows("approved", "private")
            ]
            rejection_rows = connection.execute(
                f"""
                SELECT decision.*, candidate.account_id,
                       account.current_username_display
                FROM workbench_review_decisions decision
                JOIN workbench_candidates candidate ON candidate.id=decision.candidate_id
                JOIN instagram_accounts account ON account.id=candidate.account_id
                WHERE decision.owner_user_id=? AND decision.decision='rejected' {account_scope}
                ORDER BY decision.decided_at DESC, decision.id DESC
                LIMIT ?
                """,
                (owner_user_id, history_limit),
            ).fetchall()
            approval_rows = connection.execute(
                f"""
                SELECT candidate.*, account.instagram_user_id,
                       account.current_username_display,
                       decision.decided_at,
                       dismissal.dismissed_at
                FROM workbench_review_decisions decision
                JOIN workbench_candidates candidate
                  ON candidate.id=decision.candidate_id
                JOIN instagram_accounts account ON account.id=candidate.account_id
                LEFT JOIN workbench_candidate_dismissals dismissal
                  ON dismissal.candidate_id=candidate.id
                WHERE decision.owner_user_id=? AND decision.decision='approved' {account_scope}
                ORDER BY decision.decided_at DESC, decision.id DESC
                LIMIT ?
                """,
                (owner_user_id, history_limit),
            ).fetchall()
            # Successful action identity and timing come straight from the
            # immutable business ledger.  Join only its exact attempt id to retain
            # the greeting text; never reconstruct success from bounded campaign
            # detail lists, which may be truncated in a large snapshot.
            action_success_history_rows = connection.execute(
                f"""
                SELECT success.*,
                       campaign.profile_id,
                       campaign.execution_type,
                       campaign.message AS campaign_message,
                       attempt.details_json AS attempt_details_json
                FROM action_success_ledger success
                LEFT JOIN action_campaigns campaign
                  ON campaign.id=success.campaign_id
                 AND campaign.owner_user_id=success.owner_user_id
                LEFT JOIN action_attempts attempt
                  ON attempt.id=success.attempt_id
                 AND attempt.campaign_id=success.campaign_id
                WHERE success.owner_user_id=? {action_scope}
                ORDER BY success.completed_at DESC,
                         success.operation ASC,
                         success.username_norm ASC
                LIMIT ?
                """,
                (owner_user_id, history_limit),
            ).fetchall()
            exclusion_rows = connection.execute(
                f"""
                SELECT * FROM workbench_collection_exclusions {exclusion_page_hint}
                WHERE owner_user_id=? {exclusion_scope}
                ORDER BY excluded_at DESC, id DESC
                LIMIT ?
                """,
                (owner_user_id, history_limit),
            ).fetchall()

        manual_rejections = [
            self._workbench_rejection_dict(row) for row in rejection_rows
        ]
        approval_history: list[dict[str, Any]] = []
        for row in approval_rows:
            item = self._workbench_candidate_dict(row)
            item["decided_at"] = row["decided_at"]
            item["dismissed_at"] = row["dismissed_at"]
            item["actionable"] = row["dismissed_at"] is None
            approval_history.append(item)
        action_success_history: list[dict[str, Any]] = []
        for row in action_success_history_rows:
            try:
                attempt_details = (
                    _loads(row["attempt_details_json"])
                    if isinstance(row["attempt_details_json"], str)
                    else {}
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                attempt_details = {}
            if not isinstance(attempt_details, dict):
                attempt_details = {}
            greeting_message = attempt_details.get("greeting_message")
            if not isinstance(greeting_message, str) or not greeting_message.strip():
                # Confirmed attempts created before message libraries were added did
                # not persist an attempt-level greeting field.  Those campaigns had
                # exactly one legacy message, so this is still the actual text.
                legacy_message = row["campaign_message"]
                greeting_message = (
                    str(legacy_message)
                    if row["operation"] == "greet"
                    and legacy_message is not None
                    and str(legacy_message).strip()
                    else None
                )
            history_attempt_details = (
                {"greeting_message": greeting_message}
                if greeting_message is not None
                else {}
            )
            action_success_history.append({
                # This deliberately does not reuse campaign_id: one campaign can
                # produce many immutable successes and the renderer merges action
                # histories by row id.
                "id": (
                    f"success-ledger:{row['operation']}:{row['username_norm']}"
                ),
                "ledger_backed": True,
                "ledger_campaign_id": row["campaign_id"],
                "operation": row["operation"],
                "execution_type": row["execution_type"] or "campaign",
                "profile_id": row["profile_id"] or "",
                "message": greeting_message,
                "messages": [greeting_message] if greeting_message is not None else [],
                "status": "completed",
                "targets": [
                    {
                        "id": row["target_id"],
                        "username": row["username_display"],
                        "status": "confirmed",
                        "confirmed_at": row["completed_at"],
                        "updated_at": row["completed_at"],
                    }
                ],
                "attempts": [
                    {
                        "id": row["attempt_id"],
                        "target_id": row["target_id"],
                        "username": row["username_display"],
                        "status": "confirmed",
                        "confirmed_at": row["completed_at"],
                        "finished_at": row["completed_at"],
                        "message": greeting_message,
                        "details": history_attempt_details,
                    }
                ],
                "created_at": row["completed_at"],
                "updated_at": row["completed_at"],
            })
        collection_exclusions = [
            self._workbench_exclusion_dict(row) for row in exclusion_rows
        ]
        counts = {
            "total_collected": total_collected,
            "total_public": total_public,
            "total_private": total_private,
            "total_split": total_split,
            "pending_public": grouped.get(("pending", "public"), 0),
            "pending_private": pending_private_total,
            "pending_private_primary": pending_private_primary,
            "pending_private_secondary": pending_private_secondary,
            "approved_public": approved_public_count,
            "approved_private": approved_private_count,
            "approved_dismissed": dismissed_total,
            "rejected": grouped.get(("rejected", "public"), 0)
            + grouped.get(("rejected", "private"), 0),
            "collection_excluded": exclusion_total,
        }
        counts["pending_review"] = counts["pending_public"] + counts["pending_private"]
        counts["approved"] = counts["approved_public"] + counts["approved_private"]
        counts["reviewed"] = counts["approved"] + counts["rejected"]
        storage_status = self.get_storage_status()
        return {
            "platform": platform,
            "version": __version__,
            "source_revision": __source_revision__,
            "snapshot_seq": int(revision_row["revision"]),
            "generated_at": isoformat(),
            "storage_updated_at": revision_row["updated_at"],
            "storage": {
                "backend": "sqlite",
                "durable": True,
                **storage_status,
                "cache_bytes": cache_bytes,
                "temporary_bytes": 0,
                "pending_preview_bytes": pending_preview_bytes,
                "pending_cache_entries": pending_cache_entries,
                "pending_cache_budget_bytes": WORKBENCH_PENDING_CACHE_BUDGET_BYTES,
                "event_log_entries": int(event_log_row["entries"]),
                "event_log_bytes": int(event_log_row["bytes"]),
                "retained_history": {
                    "approved": grouped.get(("approved", "public"), 0)
                    + grouped.get(("approved", "private"), 0),
                    "approved_dismissed": dismissed_total,
                    "action_successes": action_success_total,
                    "manual_rejections": counts["rejected"],
                    "collection_exclusions": exclusion_total,
                    "global_dedupe": global_total,
                },
                "last_maintenance_at": revision_row["last_cleanup_at"],
                "last_cleanup_at": revision_row["last_cleanup_at"],
                "wal_autocheckpoint_pages": 1000,
                "inline_binary_allowed": False,
            },
            "global_dedupe_count": global_total,
            "dedupe": {
                "total": global_total,
                "claimed": claimed_total,
                "pending_review": counts["pending_review"],
                "approved": counts["approved"],
                "rejected": counts["rejected"],
                "collection_excluded": exclusion_total,
                "action_successes": action_success_total,
                "greet_successes": greet_successes,
                "follow_successes": follow_successes,
            },
            "counts": counts,
            "pending_public_accounts": pending_public,
            "pending_private_accounts": pending_private,
            "unresolved_privacy_accounts": [],
            "approved_public_accounts": approved_public,
            "approved_private_accounts": approved_private,
            "approval_history": approval_history,
            "action_success_history": action_success_history,
            "manual_rejection_history": manual_rejections,
            "collection_exclusion_history": collection_exclusions,
            "has_more": {
                "pending_public_accounts": counts["pending_public"] > len(pending_public),
                "pending_private_accounts": counts["pending_private"] > len(pending_private),
                "approved_public_accounts": counts["approved_public"] > len(approved_public),
                "approved_private_accounts": counts["approved_private"] > len(approved_private),
                "approval_history": (
                    grouped.get(("approved", "public"), 0)
                    + grouped.get(("approved", "private"), 0)
                    > len(approval_history)
                ),
                "action_success_history": (
                    action_success_total > len(action_success_history)
                ),
                "manual_rejection_history": counts["rejected"] > len(manual_rejections),
                "collection_exclusion_history": exclusion_total > len(collection_exclusions),
            },
        }

    # Runtime execution and browser leases -----------------------------
    def has_unfinished_window_targets(self, owner_user_id: str, task_id: str, profile_id: str) -> bool:
        """Keep a window open for its durable unfinished assignments.

        An unassigned pending task target may still be picked by an eligible
        window. Failure-inbox rows without a window and the waiting split pool
        are not assignments. Once a live task returns a failed target to the
        waiting split pool, its recovery control records the transfer even if a
        different task subsequently claims and rebinds the split queue row.
        This reads only; mode candidates remain untouched.
        """
        with self.database.read() as connection:
            return connection.execute("""SELECT 1 FROM task_targets target
                JOIN tasks task ON task.id=target.task_id
                WHERE task.id=? AND task.owner_user_id=?
                  AND target.status!='completed'
                  AND COALESCE(target.current_stage,'')!='deleted_archived'
                  AND NOT (
                    target.current_window_id IS NULL
                    AND target.status IN ('recoverable', 'failed', 'stopped')
                    AND COALESCE(json_extract(task.settings_json, '$.live_queue_enabled'), 0)=1
                    AND EXISTS(
                      SELECT 1 FROM task_target_recovery_controls transferred
                      WHERE transferred.target_id=target.id
                        AND transferred.owner_user_id=task.owner_user_id
                        AND transferred.state='requeued'
                    )
                  )
                  AND (
                    target.current_window_id=?
                    OR (COALESCE(target.current_window_id,'')=''
                        AND COALESCE((
                          SELECT failure.source_window_id
                          FROM task_target_recovery_controls failure
                          WHERE failure.target_id=target.id
                            AND failure.owner_user_id=task.owner_user_id
                            AND failure.state='pending'
                            AND failure.source_window_id IS NOT NULL
                        ), target.preferred_window_id, '')=?)
                    OR (COALESCE(target.current_window_id,'')=''
                        AND COALESCE(target.preferred_window_id,'')=''
                        AND target.status='pending'
                        AND (json_array_length(CASE WHEN json_valid(target.allowed_window_ids_json)
                                 THEN target.allowed_window_ids_json ELSE '[]' END)=0
                             OR EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(target.allowed_window_ids_json)
                                 THEN target.allowed_window_ids_json ELSE '[]' END) WHERE value=?)))
                  ) LIMIT 1""", (task_id, owner_user_id, profile_id, profile_id, profile_id)).fetchone() is not None

    def has_returned_window_target(self, owner_user_id: str, task_id: str, profile_id: str) -> bool:
        """Whether an idle window's failed source was explicitly handed to the split queue.

        The recovery control remains keyed to the old target after another task
        claims the candidate and rewrites its source_target_id. A pending failure
        that the user has not returned must keep its window available for repair.
        """
        with self.database.read() as connection:
            return connection.execute("""
                SELECT 1 FROM task_targets target
                JOIN tasks task ON task.id=target.task_id
                JOIN task_target_recovery_controls transfer
                  ON transfer.target_id=target.id
                 AND transfer.owner_user_id=task.owner_user_id
                WHERE task.id=? AND task.owner_user_id=?
                  AND transfer.source_window_id=?
                  AND target.current_window_id IS NULL
                  AND target.status IN ('recoverable', 'failed', 'stopped')
                  AND transfer.state='requeued'
                  AND COALESCE(json_extract(task.settings_json, '$.live_queue_enabled'), 0)=1
                LIMIT 1
            """, (task_id, owner_user_id, profile_id)).fetchone() is not None

    def has_claimable_split_candidate_for_window(
        self, owner_user_id: str, task_id: str, profile_id: str,
        *, include_temporarily_blocked: bool = False,
    ) -> bool:
        """Read-only window-specific check for restarting an exited live worker.

        Mirror claim_next_split_candidate's eligibility guards: the subsequent
        worker still performs the authoritative transaction and may lose a race.
        """
        with self.database.read() as connection:
            return connection.execute("""
                SELECT 1 FROM split_candidates candidate
                JOIN tasks task ON task.id=? AND task.owner_user_id=?
                JOIN task_windows selected
                  ON selected.task_id=task.id AND selected.profile_id=?
                WHERE candidate.owner_user_id=task.owner_user_id
                  AND candidate.username_norm NOT GLOB 'fb:*'
                  AND COALESCE(json_extract(task.settings_json, '$.platform'), 'instagram')='instagram'
                  AND task.status IN ('running', 'paused', 'waiting_network')
                  AND (?=1 OR task.status='running')
                  AND COALESCE(json_extract(task.settings_json, '$.live_queue_enabled'), 0)=1
                  AND candidate.candidate_kind='manual'
                  AND candidate.queue_state='queued'
                  AND candidate.queued_target_id IS NULL
                  AND (?=1 OR candidate.dispatch_locked=0)
                  AND COALESCE(candidate.manual_category_override, '')!='completed'
                  AND COALESCE(candidate.source_status, '')!='dismissed_by_user'
                  AND (?=1 OR NOT EXISTS(
                    SELECT 1 FROM collection_dispatch_locks gate
                    WHERE gate.owner_user_id=candidate.owner_user_id AND gate.locked=1
                  ))
                  AND (
                    candidate.source_target_id IS NULL
                    OR candidate.source_task_id=task.id
                    OR NOT EXISTS (
                      SELECT 1 FROM task_targets source
                      JOIN task_windows source_window ON source_window.task_id=source.task_id
                      WHERE source.id=candidate.source_target_id
                        AND NOT EXISTS (
                          SELECT 1 FROM task_list_dismissals dismissal
                          WHERE dismissal.task_id=source.task_id
                            AND dismissal.owner_user_id=candidate.owner_user_id
                        )
                    )
                  )
                  AND (
                    NOT EXISTS (
                      SELECT 1 FROM split_candidate_window_affinity affinity
                      WHERE affinity.candidate_id=candidate.id
                    )
                    OR EXISTS (
                      SELECT 1 FROM split_candidate_window_affinity affinity
                      WHERE affinity.candidate_id=candidate.id AND affinity.profile_id=?
                    )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM task_targets existing
                    WHERE existing.task_id=task.id
                      AND existing.username_norm=candidate.username_norm
                      AND (existing.status IN ('running', 'waiting_network', 'completed')
                           OR existing.current_stage='deleted_archived')
                  )
                LIMIT 1
            """, (task_id, owner_user_id, profile_id,
                    int(include_temporarily_blocked), int(include_temporarily_blocked),
                    int(include_temporarily_blocked), profile_id)).fetchone() is not None

    def recover_interrupted_operations(self) -> None:
        """Pause interrupted collection work without inventing an active network wait.

        A live network wait is not a failure while Core is running.  If Core itself
        exits, however, its claimed target has been interrupted and must enter the
        manual failure inbox.  Its resume cursor, durable candidates and original
        wait kind/reason are retained so a login/challenge can never be mistaken for
        an automatically retryable transport outage after restart.
        """
        now = isoformat()
        with self.database.write() as connection:
            connection.execute(
                "UPDATE task_results SET updated_at=updated_at WHERE 0"  # ensure schema is writable before recovery
            )
            waiting_checkpoints = connection.execute(
                """
                SELECT checkpoint.id, checkpoint.cursor_json, checkpoint.counters_json
                FROM task_checkpoints checkpoint
                JOIN task_targets target ON target.id=checkpoint.target_id
                WHERE checkpoint.stage='waiting_network'
                """
            ).fetchall()
            for checkpoint in waiting_checkpoints:
                cursor = _loads(checkpoint["cursor_json"])
                counters = _loads(checkpoint["counters_json"])
                if not isinstance(cursor, dict):
                    cursor = {}
                if not isinstance(counters, dict):
                    counters = {}
                resume_stage = cursor.get("resume_stage", "mode_started")
                resume_cursor = cursor.get("resume_cursor", {})
                if resume_stage == "waiting_network":
                    resume_stage = "mode_started"
                if not isinstance(resume_cursor, dict):
                    resume_cursor = {}
                # A restart is not a new retry budget. Preserve the original
                # deadline/classification so an explicit resume cannot bypass a
                # cooldown or turn a login/challenge wait into a transport retry.
                wait_metadata_fields = {
                    "original_reason", "retry_count", "retry_delay_seconds",
                    "next_retry_at", "recovery_target", "candidate_username",
                    "source_discovery_complete", "no_progress_retry_kind",
                    "list_retry_count", "surface_retry_count",
                }
                wait_metadata = {
                    key: counters[key] for key in wait_metadata_fields if key in counters
                }
                previous_counters = counters.get("previous_counters")
                if not isinstance(previous_counters, dict):
                    previous_counters = {
                        key: value
                        for key, value in counters.items()
                        if key not in {
                            "reason",
                            "message",
                            "wait_kind",
                        } | wait_metadata_fields
                    }
                original_reason = str(counters.get("reason") or "network_unavailable")
                wait_kind = str(counters.get("wait_kind") or "network")
                connection.execute(
                    """
                    UPDATE task_checkpoints
                    SET stage='interrupted_recoverable', cursor_json=?,
                        counters_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        _json(
                            {
                                "resume_stage": resume_stage,
                                "resume_cursor": resume_cursor,
                            }
                        ),
                        _json(
                            {
                                **wait_metadata,
                                "reason": original_reason,
                                "wait_kind": wait_kind,
                                "interruption_reason": "application_interrupted",
                                "message": "应用已中断，检查点可继续",
                                "previous_counters": previous_counters,
                            }
                        ),
                        now,
                        checkpoint["id"],
                    ),
                )

            # There is no live waiter while the Core is starting.  A stale target
            # projection must therefore be archived together with its checkpoint,
            # even when an older bug had already changed the target back to PENDING
            # or RUNNING before the process exited.
            connection.execute(
                """
                UPDATE task_targets
                SET current_stage='interrupted_recoverable',
                    last_error='应用已中断，检查点可继续',
                    updated_at=?
                WHERE current_stage='waiting_network'
                """,
                (now,),
            )

            # The outage itself was not a failure, but losing Core interrupts a
            # claimed target. Archive it for deliberate user handling while keeping
            # its original window as the preferred resume profile.
            connection.execute(
                """
                UPDATE task_targets
                SET status='recoverable',
                    preferred_window_id=COALESCE(preferred_window_id, current_window_id),
                    current_window_id=NULL,
                    current_stage='interrupted_recoverable',
                    last_error='应用已中断，检查点可继续',
                    updated_at=?
                WHERE status='waiting_network'
                """,
                (now,),
            )

            # A profile that was actively reading Instagram is genuinely uncertain;
            # keep it recoverable and archive it through the existing failure trigger.
            connection.execute(
                """
                UPDATE task_targets
                SET status='recoverable',
                    preferred_window_id=COALESCE(preferred_window_id, current_window_id),
                    current_window_id=NULL,
                    last_error='应用在采集中断，可从检查点继续',
                    updated_at=?
                WHERE status='running'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status='paused', finished_at=NULL,
                    last_error='应用已中断；请从原检查点继续',
                    updated_at=?, version=version+1
                WHERE status IN ('running', 'waiting_network')
                   OR (
                       status='recoverable'
                       AND EXISTS(
                           SELECT 1 FROM task_targets target
                           WHERE target.task_id=tasks.id
                             AND target.status='recoverable'
                             AND target.current_stage='interrupted_recoverable'
                       )
                   )
                   OR (
                       status='completed'
                       AND EXISTS(
                           SELECT 1 FROM task_targets target
                           WHERE target.task_id=tasks.id
                             AND target.status!='completed'
                       )
                   )
                """,
                (now,),
            )
            running_attempts = connection.execute(
                "SELECT id, target_id, details_json FROM action_attempts WHERE status='running'"
            ).fetchall()
            for attempt in running_attempts:
                details = _loads(attempt["details_json"])
                if not isinstance(details, dict):
                    details = {}
                details["reason"] = "application_stopped_during_action"
                connection.execute(
                    "UPDATE action_attempts SET status='unknown', finished_at=?, details_json=? WHERE id=?",
                    (now, _json(details), attempt["id"]),
                )
                connection.execute(
                    "UPDATE action_targets SET status='unknown', control_after_attempt=NULL, last_error=?, updated_at=? WHERE id=?",
                    ("Outcome unknown after application restart; automatic retry disabled", now, attempt["target_id"]),
                )
            connection.execute(
                "UPDATE action_campaigns SET status='paused', last_error=?, updated_at=?, version=version+1 "
                "WHERE status IN ('queued', 'running')",
                ("Application restarted; task is paused and requires explicit resume", now),
            )
            connection.execute("DELETE FROM action_dispatch_claims")
            # Posting submission/cleanup may have an unknown external outcome.
            # Its durable manager, not generic startup, owns releasing that fence.
            connection.execute("""DELETE FROM browser_operation_leases WHERE operation_type!='posting'
                AND NOT (operation_type='studio' AND NOT EXISTS (
                    SELECT 1 FROM studio_jobs own_job WHERE own_job.id=browser_operation_leases.entity_id
                    AND own_job.owner_user_id=browser_operation_leases.owner_user_id
                    AND own_job.profile_id=browser_operation_leases.profile_id AND own_job.kind='nurture'))
                AND NOT EXISTS (SELECT 1 FROM studio_jobs job
                    WHERE job.profile_id=browser_operation_leases.profile_id AND job.kind='nurture'
                    AND job.status='completed' AND json_extract(job.result_json,'$.window_hold')=1)""")

    def get_task_execution_spec(self, owner_user_id: str, task_id: str) -> dict[str, Any]:
        return self.get_task(owner_user_id, task_id)

    def get_checkpoint(self, owner_user_id: str, task_id: str, target_id: str, mode: str) -> dict[str, Any] | None:
        with self.database.read() as connection:
            self._owned_task(connection, owner_user_id, task_id)
            row = connection.execute(
                "SELECT * FROM task_checkpoints WHERE task_id=? AND target_id=? AND mode=?",
                (task_id, target_id, mode),
            ).fetchone()
        return self._checkpoint_dict(row) if row else None

    def reconcile_task_network_status(
        self, owner_user_id: str, task_id: str, status: str, *, error: str | None = None,
    ) -> None:
        """Update active recovery state without reviving a concurrent pause/stop.

        The read-only fast path avoids queuing unchanged status behind a writer.
        The write path rechecks the state inside its transaction: a pause or final
        cleanup may have committed while this background call waited for SQLite.
        """
        if status not in {"running", "waiting_network"}:
            raise ValidationError("Invalid task network status")

        def needed(task) -> bool:
            return task["status"] in {"queued", "running", "waiting_network"} and (
                task["status"] != status or task["last_error"] != error
            )

        with self.database.read() as connection:
            if not needed(self._owned_task(connection, owner_user_id, task_id)):
                return
        now = isoformat()
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            if not needed(task):
                return
            connection.execute(
                "UPDATE tasks SET status=?, updated_at=?, started_at=?, finished_at=NULL, "
                "last_error=?, version=version+1 WHERE id=?",
                (status, now, task["started_at"] or (now if status == "running" else None), error, task_id),
            )
            _event(connection, owner_user_id, "task", task_id, "task.runtime_status",
                   {"from": task["status"], "to": status, "error": error})

    def set_task_runtime_status(
        self,
        owner_user_id: str,
        task_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        if status not in TASK_STATES:
            raise ValidationError("Invalid task runtime status")
        if status in {"stopped", "completed", "failed", "recoverable"}:
            return self.finalize_task_runtime_status(
                owner_user_id,
                task_id,
                status,
                error=error,
            )
        now = isoformat()
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            started_at = task["started_at"] or (now if status == "running" else None)
            finished_at = now if status in {"stopped", "completed", "failed"} else None
            connection.execute(
                """
                UPDATE tasks SET status=?, updated_at=?, started_at=?, finished_at=?, last_error=?, version=version+1
                WHERE id=?
                """,
                (status, now, started_at, finished_at, error, task_id),
            )
            _event(
                connection,
                owner_user_id,
                "task",
                task_id,
                "task.runtime_status",
                {"from": task["status"], "to": status, "error": error},
            )
        return self.get_task(owner_user_id, task_id)

    def finalize_task_runtime_status(
        self,
        owner_user_id: str,
        task_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Atomically converge active targets and publish a terminal task state.

        ExecutionManager can call this once after all workers have unwound.  Failed,
        stopped and recoverable tasks convert any residual RUNNING/WAITING_NETWORK
        target to RECOVERABLE in the same SQLite transaction.  COMPLETED is stricter:
        it is rejected unless every target is already completed, preventing a durable
        contradiction that the renderer cannot safely resume.
        """

        if status not in {"stopped", "completed", "failed", "recoverable"}:
            raise ValidationError("Invalid terminal task runtime status")
        now = isoformat()
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            if status == "completed":
                if connection.execute(
                    "SELECT 1 FROM task_targets target JOIN task_mode_candidates candidate "
                    "ON candidate.target_id=target.id "
                    "WHERE target.task_id=? AND candidate.state='pending' LIMIT 1",
                    (task_id,),
                ).fetchone() is not None:
                    raise ConflictError("A task cannot complete while candidates are pending")
                unfinished = connection.execute(
                    """
                    SELECT id, status FROM task_targets
                    WHERE task_id=? AND status!='completed'
                    ORDER BY queue_order
                    """,
                    (task_id,),
                ).fetchall()
                if unfinished:
                    raise ConflictError(
                        "A task cannot complete while targets are unfinished",
                        details={
                            "task_id": task_id,
                            "unfinished_targets": len(unfinished),
                            "active_targets": sum(
                                1
                                for row in unfinished
                                if row["status"] in {"running", "waiting_network"}
                            ),
                        },
                    )
                converged_targets = 0
            else:
                cursor = connection.execute(
                    """
                    UPDATE task_targets
                    SET status='recoverable',
                        preferred_window_id=COALESCE(
                            preferred_window_id, current_window_id
                        ),
                        last_error=COALESCE(?, last_error),
                        updated_at=?
                    WHERE task_id=?
                      AND status IN ('running', 'waiting_network')
                    """,
                    (error, now, task_id),
                )
                converged_targets = cursor.rowcount
            started_at = task["started_at"]
            finished_at = now if status in {"stopped", "completed", "failed"} else None
            connection.execute(
                """
                UPDATE tasks
                SET status=?, updated_at=?, started_at=?, finished_at=?,
                    last_error=?, version=version+1
                WHERE id=?
                """,
                (status, now, started_at, finished_at, error, task_id),
            )
            _event(
                connection,
                owner_user_id,
                "task",
                task_id,
                "task.runtime_finalized",
                {
                    "from": task["status"],
                    "to": status,
                    "error": error,
                    "converged_targets": converged_targets,
                },
            )
        return self.get_task(owner_user_id, task_id)

    def capture_target_source_profile(
        self, owner_user_id: str, task_id: str, target_id: str, profile: dict[str, Any]
    ) -> bool:
        """Persist only exact-source header counts while this generation is live."""
        from .split_completion_details import count
        if not isinstance(profile, dict) or not isinstance(profile.get('username'), str):
            return False
        try:
            username, _ = normalize_instagram_username(profile['username'])
        except ValidationError:
            return False
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            target = connection.execute('SELECT username_norm,status,current_stage FROM task_targets WHERE id=? AND task_id=?',
                (target_id, task_id)).fetchone()
            if (target is None or task['status'] == 'completed' or target['status'] == 'completed'
                    or target['current_stage'] in {'completed_archived', 'deleted_archived'}
                    or target['username_norm'] != username):
                return False
            snapshot = {'username': username, **{field: count(profile.get(field))
                        for field in ('followers', 'following', 'posts')}}
            connection.execute('UPDATE task_targets SET source_profile_json=? WHERE id=? AND task_id=?',
                (_json(snapshot), target_id, task_id))
        return True

    def set_target_runtime_status(
        self,
        owner_user_id: str,
        task_id: str,
        target_id: str,
        status: str,
        *,
        window_id: str | None = None,
        source_recheck_mode: str | None = None,
        automatic_completion_token: str | None = None,
    ) -> None:
        if status not in {"pending", "running", "waiting_network", "completed", "recoverable", "failed", "stopped"}:
            raise ValidationError("Invalid target runtime status")
        now = isoformat()
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            target = connection.execute(
                "SELECT * FROM task_targets WHERE id=? AND task_id=?",
                (target_id, task_id),
            ).fetchone()
            if target is None:
                raise NotFoundError("Target not found")
            if status == "completed" and connection.execute(
                "SELECT 1 FROM task_mode_candidates "
                "WHERE target_id=? AND state='pending' LIMIT 1",
                (target_id,),
            ).fetchone() is not None:
                raise ConflictError("A target cannot complete while candidates are pending")
            if status == "completed":
                for checkpoint in connection.execute(
                    "SELECT cursor_json FROM task_checkpoints WHERE task_id=? AND target_id=? AND mode IN ('followers','following')",
                    (task_id, target_id),
                ):
                    if _pending_relation_cursor_names(checkpoint["cursor_json"]):
                        raise ConflictError("A target cannot complete while observed list identities remain unconfirmed")
            already_owned = (
                target["status"] in {"running", "waiting_network"}
                and bool(window_id) and target["current_window_id"] == window_id
            )
            if (
                target["status"] in {"running", "waiting_network"}
                and window_id
                and target["current_window_id"] != window_id
            ):
                raise ConflictError(
                    "该目标已由另一个窗口执行，不能改写窗口归属",
                    details={"target_id": target_id, "reason": "target_owned_by_other_window"},
                )
            if status in {"running", "waiting_network"} and not already_owned:
                if target["status"] in {"running", "waiting_network"}:
                    raise ConflictError(
                        "该目标已由另一个窗口执行，不能改写窗口归属",
                        details={"target_id": target_id, "reason": "target_owned_by_other_window"},
                    )
                if connection.execute(
                    """
                    SELECT 1 FROM split_candidates
                    WHERE owner_user_id=? AND candidate_kind='manual'
                      AND source_target_id=? AND queue_state='queued'
                      AND queued_target_id IS NULL LIMIT 1
                    """,
                    (owner_user_id, target_id),
                ).fetchone():
                    raise ConflictError(
                        "该目标已返回等待列表，必须先领取排队记录",
                        details={"target_id": target_id, "reason": "split_candidate_waiting"},
                    )
            if status in {"running", "waiting_network"} and not already_owned:
                self._guard_collection_dispatch(connection, owner_user_id, target["username_norm"])
            try:
                allowed_window_ids = _loads(
                    _row_value(target, "allowed_window_ids_json", "[]") or "[]"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                allowed_window_ids = []
            if (
                window_id
                and isinstance(allowed_window_ids, list)
                and allowed_window_ids
                and window_id not in allowed_window_ids
            ):
                raise ConflictError(
                    "The selected window is not allowed for this split target",
                    details={
                        "task_id": task_id,
                        "target_id": target_id,
                        "profile_id": window_id,
                    },
                )
            if task["status"] == "completed" and status != "completed":
                raise ConflictError(
                    "A completed task cannot contain an unfinished target",
                    details={"task_id": task_id, "target_id": target_id, "status": status},
                )
            current_status = str(target["status"])
            failure_statuses = {"failed", "recoverable", "stopped"}
            stale_reversal = (
                current_status == "completed" and status != "completed"
            ) or (
                current_status in failure_statuses and status == "completed"
            )
            if not stale_reversal and status == "completed":
                stale_reversal = connection.execute(
                    """
                    SELECT 1 FROM task_target_recovery_controls
                    WHERE target_id=? AND owner_user_id=? AND state='pending'
                    """,
                    (target_id, owner_user_id),
                ).fetchone() is not None
            if not stale_reversal and status in failure_statuses:
                explicit_recheck = connection.execute(
                    "SELECT 1 FROM task_source_rechecks WHERE target_id=? AND state IN ('prepared','active','resuming')", (target_id,)
                ).fetchone()
                stale_reversal = not explicit_recheck and connection.execute(
                    """
                    SELECT 1 FROM split_candidate_history
                    WHERE owner_user_id=? AND source_target_id=?
                    """,
                    (owner_user_id, target_id),
                ).fetchone() is not None
            if stale_reversal:
                raise ConflictError(
                    "Stale target status cannot reverse failure and completion",
                    details={
                        "task_id": task_id,
                        "target_id": target_id,
                        "from_status": current_status,
                        "to_status": status,
                    },
                )
            cursor = connection.execute(
                """
                UPDATE task_targets
                SET status=?,
                    preferred_window_id=CASE
                        WHEN ? IN ('failed', 'recoverable', 'stopped')
                        THEN COALESCE(preferred_window_id, current_window_id, ?)
                        ELSE preferred_window_id
                    END,
                    current_window_id=CASE
                        WHEN ? IN ('failed', 'recoverable', 'stopped') THEN NULL
                        ELSE ?
                    END,
                    last_error=CASE
                        WHEN ? IN ('pending', 'running', 'completed') THEN NULL
                        ELSE last_error
                    END,
                    updated_at=?
                WHERE id=? AND task_id=?
                """,
                (status, status, window_id, status, window_id, status, now, target_id, task_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Target status changed concurrently")
            if status in failure_statuses and window_id:
                # The failure trigger fires during UPDATE above. A source may
                # have been preferred on A and later claimed by B; the true
                # failing lease is B, even though preferred_window_id stays A.
                connection.execute(
                    "UPDATE task_target_recovery_controls SET source_window_id=? "
                    "WHERE target_id=? AND owner_user_id=? AND state='pending'",
                    (window_id, target_id, owner_user_id),
                )
            if source_recheck_mode is not None:
                connection.execute(
                    "UPDATE task_source_rechecks SET state='completed',completed_at=? "
                    "WHERE target_id=? AND mode=? AND state='active'", (now, target_id, source_recheck_mode),
                )
            if status == "completed":
                if automatic_completion_token and window_id:
                    if not connection.execute(
                        "SELECT 1 FROM browser_operation_leases WHERE profile_id=? AND lease_token=? "
                        "AND owner_user_id=? AND operation_type='collection' AND entity_id=?",
                        (window_id, automatic_completion_token, owner_user_id, task_id),
                    ).fetchone():
                        raise ConflictError("Completion cleanup lease changed")
                    connection.execute(
                        "INSERT INTO task_automatic_completions(target_id,task_id,profile_id,lease_token,state,updated_at) "
                        "VALUES(?,?,?,?,'cleanup_pending',?) ON CONFLICT(target_id) DO UPDATE SET "
                        "profile_id=excluded.profile_id,lease_token=excluded.lease_token,state='cleanup_pending',updated_at=excluded.updated_at",
                        (target_id, task_id, window_id, automatic_completion_token, now),
                    )
                connection.execute(
                    "UPDATE task_source_rechecks SET state='completed',completed_at=? "
                    "WHERE target_id=? AND state IN ('active','resuming')", (now, target_id),
                )
                if current_status != 'completed':
                    from .split_completion_details import save_completion_details
                    save_completion_details(connection, owner_user_id, target_id)
                # The status update above lets the completion trigger preserve the
                # source window in permanent history first. Archive the execution
                # card in the SAME transaction. Keep its source window for history
                # and delayed command ownership checks; runtime leases, not this
                # historical association, decide whether that window is occupied.
                connection.execute(
                    """
                    UPDATE task_targets
                    SET current_stage='completed_archived'
                    WHERE id=? AND task_id=? AND status='completed'
                    """,
                    (target_id, task_id),
                )
                if target["current_stage"] != "completed_archived":
                    _event(
                        connection, owner_user_id, "task_target", target_id,
                        "task_target.completed_card_archived",
                        {"task_id": task_id, "history_preserved": True, "automatic": True},
                    )

    def claim_parent_reel_decision(
        self, owner_user_id: str, task_id: str, target_id: str, profile_id: str,
        reel_key: str, like_selected: bool, *, lease_token: str,
    ) -> bool:
        """Fence and persist a Reel decision before an optional like attempt.

        A decision is never retried after an ambiguous click or process restart.
        This grants no new lease and never modifies account/candidate counters.
        """
        if type(like_selected) is not bool or not isinstance(reel_key, str) or not re.fullmatch(r"/reel/[A-Za-z0-9_-]{1,64}", reel_key):
            raise ValidationError("Invalid parent Reel decision")
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            target = connection.execute("SELECT status,current_window_id FROM task_targets WHERE id=? AND task_id=?",
                                        (target_id, task_id)).fetchone()
            if target is None:
                raise NotFoundError("Task target not found")
            if (collection_platform(_loads(task["settings_json"])) != "instagram"
                    or task["status"] != "running" or target["status"] != "running"
                    or target["current_window_id"] != profile_id):
                return False
            if not connection.execute(
                "SELECT 1 FROM browser_operation_leases WHERE profile_id=? AND lease_token=? "
                "AND owner_user_id=? AND operation_type='collection' AND entity_id=?",
                (profile_id, lease_token, owner_user_id, task_id),
            ).fetchone():
                return False
            modes = _loads(task["modes_json"])
            if not modes or modes[-1] != "followers":
                return False
            for mode in modes:
                checkpoint = connection.execute("SELECT cursor_json FROM task_checkpoints WHERE target_id=? AND mode=?",
                                                (target_id, mode)).fetchone()
                cursor = _loads(checkpoint["cursor_json"]) if checkpoint else {}
                for _ in range(6):
                    if not isinstance(cursor, dict) or cursor.get("pending_relation_usernames"):
                        return False
                    if isinstance(cursor.get("resume_cursor"), dict):
                        cursor = cursor["resume_cursor"]
                    else:
                        break
                if not (cursor.get("candidate_spool_complete") is True and cursor.get("candidate_spool_natural_end") is True):
                    return False
            if not connection.execute("SELECT 1 FROM task_mode_candidates WHERE target_id=? AND state='pending' LIMIT 1",
                                      (target_id,)).fetchone():
                return False
            claimed = connection.execute(
                "INSERT OR IGNORE INTO task_parent_reel_decisions(owner_user_id,task_id,target_id,reel_key,"
                "like_selected,profile_id,lease_token,decided_at) VALUES(?,?,?,?,?,?,?,?)",
                (owner_user_id,task_id,target_id,reel_key,int(like_selected),profile_id,lease_token,isoformat()),
            )
            return claimed.rowcount == 1

    def acquire_browser_lease(self, owner_user_id: str, profile_id: str, *,
                              operation_type: str, entity_id: str, ttl_seconds: int = 90) -> str:
        # Lock order is surface -> SQLite. Desktop I/O never holds SQLite's write
        # lock, and a competing surface cannot appear between validation and hide.
        with self.database.browser_surface_lock:
            token = self._acquire_browser_lease_record(owner_user_id, profile_id,
                operation_type=operation_type, entity_id=entity_id, ttl_seconds=ttl_seconds)
            try:
                before_operation = getattr(self, 'before_browser_operation', None)
                if before_operation:
                    before_operation(profile_id)
            except BaseException:
                self.release_browser_lease(profile_id, token)
                raise
            return token

    def _acquire_browser_lease_record(
        self,
        owner_user_id: str,
        profile_id: str,
        *,
        operation_type: str,
        entity_id: str,
        ttl_seconds: int = 90,
    ) -> str:
        if operation_type not in {"collection", "action", "monitor", "studio", "account"}:
            raise ValidationError("Invalid browser lease operation")
        if not 15 <= ttl_seconds <= 600:
            raise ValidationError("Browser lease TTL must be between 15 and 600 seconds")
        now_dt = utc_now()
        now = isoformat(now_dt)
        expires = isoformat(now_dt + timedelta(seconds=ttl_seconds))
        lease_token = str(uuid.uuid4())
        with self.database.write() as connection:
            if profile_id.startswith('native:'):
                native = connection.execute('SELECT owner_user_id FROM native_browser_profiles WHERE id=?',(profile_id,)).fetchone()
                if native is None or native['owner_user_id'] != owner_user_id:
                    raise NotFoundError('内置窗口不存在')
            # A missing lease never authorizes adopting an unresolved durable
            # hold. Posting preflight uses this exact read-only fence as well.
            assert_no_durable_window_hold(connection, owner_user_id, profile_id)
            if operation_type != 'account':
                from .account_platforms import platform_for_profile
                browser_platform = platform_for_profile(connection, profile_id, owner_user_id=owner_user_id)
                if operation_type == 'collection':
                    task = connection.execute('SELECT settings_json FROM tasks WHERE id=? AND owner_user_id=?',
                        (entity_id, owner_user_id)).fetchone()
                    if task is not None:
                        stored_task_settings(task['settings_json'])
                allowed_platforms = {'instagram'}
                if browser_platform not in allowed_platforms:
                    raise ValidationError('该窗口平台与当前任务不匹配，请选择对应平台的空闲窗口')
            # Clear only rows that are unambiguously expired/orphaned here. Active
            # manager reconciliation is performed by the profiles endpoint, which has
            # access to the in-memory task registry.
            _reconcile_browser_leases(
                connection,
                now=now_dt,
                # A task can persist FAILED/RECOVERABLE slightly before its async
                # coordinator has disconnected every worker and released the lease.
                # acquire() has no in-memory manager registry, so terminal status is
                # not sufficient evidence that the row is stale. Keep it fenced until
                # explicit release or TTL expiry instead of allowing duplicate use.
                remove_terminal_without_manager=False,
                protected_tokens=self.database.live_browser_lease_tokens,
            )
            try:
                connection.execute(
                    """
                    INSERT INTO browser_operation_leases(
                        profile_id, owner_user_id, operation_type, entity_id, lease_token,
                        acquired_at, heartbeat_at, expires_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (profile_id, owner_user_id, operation_type, entity_id, lease_token, now, now, expires),
                )
            except sqlite3.IntegrityError as exc:
                holder = connection.execute(
                    "SELECT operation_type, entity_id, owner_user_id FROM browser_operation_leases WHERE profile_id=?",
                    (profile_id,),
                ).fetchone()
                retired_holder = holder and (holder['operation_type'] == 'posting' or (
                    holder['operation_type'] == 'studio' and holder['entity_id'] in legacy_studio_lease_entities(connection)))
                raise ConflictError(
                    "BitBrowser window is already in use",
                    details={
                        "profile_id": profile_id,
                        "operation_type": 'account' if retired_holder else holder["operation_type"] if holder else "unknown",
                        "entity_id": holder["entity_id"] if holder and not retired_holder and holder['owner_user_id'] == owner_user_id else None,
                    },
                ) from exc
            self.database.live_browser_lease_tokens.add(lease_token)
        return lease_token

    def renew_browser_lease(self, profile_id: str, lease_token: str, *, ttl_seconds: int = 90) -> None:
        now_dt = utc_now()
        now = isoformat(now_dt)
        expires = isoformat(now_dt + timedelta(seconds=ttl_seconds))
        with self.database.write() as connection:
            cursor = connection.execute(
                """
                UPDATE browser_operation_leases SET heartbeat_at=?, expires_at=?
                WHERE profile_id=? AND lease_token=?
                """,
                (now, expires, profile_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Browser operation lease was lost", details={"profile_id": profile_id})

    def release_browser_lease(
        self, profile_id: str, lease_token: str, *, completed_cleanup: bool = False,
    ) -> None:
        """Release the exact generation; successful cleanup also hides its cards.

        Release and dismissal commit together. A missing/replaced lease is never
        evidence of successful cleanup, including after startup recovery.
        """
        with self.database.write() as connection:
            lease = connection.execute(
                "SELECT owner_user_id,entity_id,operation_type FROM browser_operation_leases "
                "WHERE profile_id=? AND lease_token=?", (profile_id, lease_token),
            ).fetchone()
            connection.execute(
                "DELETE FROM browser_operation_leases WHERE profile_id=? AND lease_token=?",
                (profile_id, lease_token),
            )
            if completed_cleanup and lease and lease["operation_type"] == "collection":
                rows = connection.execute(
                    "SELECT completion.target_id FROM task_automatic_completions completion "
                    "JOIN task_targets target ON target.id=completion.target_id "
                    "WHERE completion.profile_id=? AND completion.lease_token=? AND completion.task_id=? "
                    "AND completion.state='cleanup_pending' AND target.status='completed' "
                    "AND NOT EXISTS(SELECT 1 FROM task_mode_candidates candidate "
                    "WHERE candidate.target_id=target.id AND candidate.state='pending')",
                    (profile_id, lease_token, lease["entity_id"]),
                ).fetchall()
                now = isoformat()
                for row in rows:
                    connection.execute(
                        "INSERT INTO task_target_list_dismissals(target_id,task_id,owner_user_id,dismissed_at) "
                        "VALUES(?,?,?,?) ON CONFLICT(target_id) DO NOTHING",
                        (row["target_id"], lease["entity_id"], lease["owner_user_id"], now),
                    )
                    connection.execute(
                        "UPDATE task_automatic_completions SET state='dismissed',updated_at=? WHERE target_id=?",
                        (now, row["target_id"]),
                    )
                    _event(connection, lease["owner_user_id"], "task_target", row["target_id"],
                           "task_target.dismissed_from_collection_list",
                           {"task_id": lease["entity_id"], "history_retained": True, "automatic": True})
        self.database.live_browser_lease_tokens.discard(lease_token)

    def release_browser_leases_for_entity(
        self,
        owner_user_id: str,
        entity_id: str,
        *,
        operation_type: str,
    ) -> int:
        """Release stale leases only for the owned task/campaign being closed."""
        if operation_type not in {"collection", "action", "monitor", "studio", "posting", "account"}:
            raise ValidationError("Invalid browser lease operation")
        with self.database.write() as connection:
            tokens = [row[0] for row in connection.execute(
                "SELECT lease_token FROM browser_operation_leases WHERE owner_user_id=? AND entity_id=? AND operation_type=?",
                (owner_user_id, entity_id, operation_type))]
            cursor = connection.execute(
                """
                DELETE FROM browser_operation_leases
                WHERE owner_user_id=? AND entity_id=? AND operation_type=?
                """,
                (owner_user_id, entity_id, operation_type),
            )
            self.database.live_browser_lease_tokens.difference_update(tokens)
        return int(cursor.rowcount)

    def list_browser_lease_states(
        self,
        requesting_user_id: str,
        *,
        active_collection_entity_ids: Iterable[str] | None = None,
        active_action_entity_ids: Iterable[str] | None = None,
        active_monitor_entity_ids: Iterable[str] | None = None,
        active_studio_entity_ids: Iterable[str] | None = None,
        active_posting_entity_ids: Iterable[str] | None = None,
        inactive_grace_seconds: int = 5,
    ) -> list[dict[str, Any]]:
        """Read occupancy, retaining conservative locks when cleanup is busy.

        Reconciliation is optional here. Command admission still acquires and
        verifies leases transactionally; no cached or expired lease is silently
        treated as free while its cleanup has been deferred.
        """
        now_dt = utc_now()
        with self.database.try_write() as connection:
            if connection is not None:
                _reconcile_browser_leases(
                    connection,
                    now=now_dt,
                    active_collection_entity_ids=(
                        set(active_collection_entity_ids)
                        if active_collection_entity_ids is not None
                        else None
                    ),
                    active_action_entity_ids=(
                        set(active_action_entity_ids)
                        if active_action_entity_ids is not None
                        else None
                    ),
                    active_monitor_entity_ids=(
                        set(active_monitor_entity_ids)
                        if active_monitor_entity_ids is not None
                        else None
                    ),
                    active_studio_entity_ids=set(active_studio_entity_ids) if active_studio_entity_ids is not None else None,
                    active_posting_entity_ids=set(active_posting_entity_ids) if active_posting_entity_ids is not None else None,
                    inactive_grace_seconds=inactive_grace_seconds,
                    protected_tokens=self.database.live_browser_lease_tokens,
                )
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT
                    lease.profile_id,
                    lease.owner_user_id,
                    lease.operation_type,
                    lease.entity_id,
                    lease.acquired_at,
                    lease.heartbeat_at,
                    lease.expires_at,
                    task.status AS task_status,
                    campaign.status AS campaign_status,
                    EXISTS(
                        SELECT 1 FROM task_targets target
                        WHERE target.task_id=lease.entity_id
                          AND target.current_window_id=lease.profile_id
                          AND target.status IN ('running', 'waiting_network')
                    ) AS has_running_target
                FROM browser_operation_leases lease
                LEFT JOIN tasks task
                  ON lease.operation_type='collection' AND task.id=lease.entity_id
                LEFT JOIN action_campaigns campaign
                  ON lease.operation_type='action' AND campaign.id=lease.entity_id
                ORDER BY lease.acquired_at
                """
            ).fetchall()
            cleanup_rows = connection.execute(
                "SELECT profile_id,id,owner_user_id,created_at FROM studio_jobs "
                "WHERE kind='nurture' AND status='completed' AND json_extract(result_json,'$.window_hold')=1 "
                "ORDER BY created_at,id"
            ).fetchall()
            retired_studio_ids = legacy_studio_lease_entities(connection)
            retired_holds = legacy_window_holds(connection)
        result: list[dict[str, Any]] = []
        for row in rows:
            owned = row["owner_user_id"] == requesting_user_id
            retired = row['operation_type'] == 'posting' or (
                row['operation_type'] == 'studio' and row['entity_id'] in retired_studio_ids)
            if retired:
                state = 'occupied'
            elif row["operation_type"] == "collection":
                state = (
                    "paused"
                    if row["task_status"] == "paused"
                    else "waiting_network"
                    if row["task_status"] == "waiting_network"
                    else "collecting"
                    if row["has_running_target"]
                    else "waiting"
                )
            else:
                state = "paused" if row["campaign_status"] == "paused" else "action"
            result.append(
                {
                    "profile_id": row["profile_id"],
                    "operation_type": 'account' if retired else row["operation_type"],
                    "state": state,
                    "owned_by_current_login": owned,
                    "entity_id": row["entity_id"] if owned and not retired else None,
                    "acquired_at": row["acquired_at"],
                    "heartbeat_at": row["heartbeat_at"],
                    "expires_at": row["expires_at"],
                    **({'can_reconcile_window_state': True} if retired and owned else {}),
                }
            )
        # These are display-only reservations, never replacement lease records.
        # Keep an actual successor lease authoritative and never disclose a
        # different login's job id through the synthetic cleanup entry.
        visible_profiles = {row['profile_id'] for row in result}
        for row in cleanup_rows:
            if row['profile_id'] in visible_profiles:
                continue
            owned = row['owner_user_id'] == requesting_user_id
            result.append({'profile_id':row['profile_id'],'operation_type':'studio',
                'state':'cleanup_pending','owned_by_current_login':owned,
                'entity_id':row['id'] if owned else None,'acquired_at':row['created_at'],
                'heartbeat_at':None,'expires_at':None})
            visible_profiles.add(row['profile_id'])
        for row in retired_holds:
            if row['profile_id'] in visible_profiles:
                continue
            result.append({'profile_id': row['profile_id'], 'operation_type': 'account',
                'state': 'occupied', 'owned_by_current_login': row['owner_user_id'] == requesting_user_id,
                'entity_id': None, 'acquired_at': row['created_at'],
                'heartbeat_at': None, 'expires_at': None,
                **({'can_reconcile_window_state': True} if row['owner_user_id'] == requesting_user_id else {})})
            visible_profiles.add(row['profile_id'])
        return result

    # Persistent action campaigns --------------------------------------
    def create_action_campaign(
        self,
        owner_user_id: str,
        *,
        operation: str,
        execution_type: str,
        profile_id: str,
        targets: list[str],
        target_sources: dict[str, str] | None = None,
        message: str | None,
        interval_min_seconds: int,
        interval_max_seconds: int,
        limit_count: int,
        messages: list[str] | None = None,
    ) -> dict[str, Any]:
        profile_id = str(profile_id).strip()
        if not profile_id or len(profile_id) > 128:
            raise ValidationError("A valid BitBrowser profile is required")
        if operation not in {"follow", "greet"} or execution_type not in {"manual", "campaign"}:
            raise ValidationError("Invalid action campaign type")
        normalized_messages: list[str] = []
        seen_messages: set[str] = set()
        if operation == "greet":
            raw_messages: list[Any] = list(messages or [])
            if message is not None:
                # Append the legacy field after the new library. New clients may
                # send both (`message=messages[0]`); de-duplication keeps it once.
                raw_messages.append(message)
            for raw_message in raw_messages:
                if not isinstance(raw_message, str):
                    raise ValidationError("Greeting messages must be text")
                cleaned_message = raw_message.strip()
                if not cleaned_message:
                    continue
                if len(cleaned_message) > 200:
                    raise ValidationError("Greeting message may not exceed 200 characters")
                if cleaned_message not in seen_messages:
                    normalized_messages.append(cleaned_message)
                    seen_messages.add(cleaned_message)
            if not normalized_messages:
                raise ValidationError("At least one greeting message is required")
        primary_message = normalized_messages[0] if normalized_messages else None
        if limit_count < 1:
            raise ValidationError("Action limit must be positive")
        if interval_min_seconds < 0 or interval_max_seconds < interval_min_seconds:
            raise ValidationError("Invalid action interval")
        normalized_sources: dict[str, str] = {}
        for raw_username, raw_source in (target_sources or {}).items():
            try:
                username_norm, _ = normalize_instagram_username(raw_username)
            except ValidationError:
                continue
            source = str(raw_source).strip()
            if source:
                normalized_sources[username_norm] = source[:150]
        normalized: list[tuple[str, str, str | None]] = []
        seen: set[str] = set()
        for raw_target in targets:
            username_norm, username_display = normalize_instagram_username(raw_target)
            if username_norm not in seen:
                normalized.append(
                    (username_norm, username_display, normalized_sources.get(username_norm))
                )
                seen.add(username_norm)
        if not normalized:
            raise ValidationError("At least one valid action target is required")
        campaign_id = str(uuid.uuid4())
        now = isoformat()
        with self.database.write() as connection:
            if connection.execute("SELECT 1 FROM app_users WHERE id=?", (owner_user_id,)).fetchone() is None:
                raise AuthenticationError("Application user no longer exists")
            # Only inspect ledger entries named by this request.  Both tables can
            # grow for the lifetime of an installation, whereas the matching sets
            # below are now bounded by the request.  The surrounding IMMEDIATE
            # transaction preserves the former atomic eligibility snapshot.
            successful: set[str] = set()
            dispatching: set[str] = set()
            requested_usernames = [item[0] for item in normalized]
            for batch_start in range(
                0, len(requested_usernames), ACTION_ELIGIBILITY_BATCH_MAX
            ):
                batch = requested_usernames[
                    batch_start : batch_start + ACTION_ELIGIBILITY_BATCH_MAX
                ]
                placeholders = ",".join("?" for _ in batch)
                matching_rows = connection.execute(
                    f"""
                    SELECT username_norm, 'successful' AS eligibility_state
                    FROM action_success_ledger
                    WHERE owner_user_id=? AND operation=?
                      AND username_norm IN ({placeholders})
                    UNION ALL
                    SELECT username_norm, 'dispatching' AS eligibility_state
                    FROM action_dispatch_claims
                    WHERE owner_user_id=? AND operation=?
                      AND username_norm IN ({placeholders})
                    """,
                    (
                        owner_user_id,
                        operation,
                        *batch,
                        owner_user_id,
                        operation,
                        *batch,
                    ),
                ).fetchall()
                for row in matching_rows:
                    if row["eligibility_state"] == "successful":
                        successful.add(row["username_norm"])
                    else:
                        dispatching.add(row["username_norm"])
            skipped_successful = [item[0] for item in normalized if item[0] in successful]
            skipped_running = [item[0] for item in normalized if item[0] in dispatching]
            eligible = [
                item
                for item in normalized
                if item[0] not in successful and item[0] not in dispatching
            ][:limit_count]
            if not eligible:
                raise ConflictError(
                    "Every selected account is already successful or currently executing",
                    details={
                        "already_successful": skipped_successful,
                        "currently_executing": skipped_running,
                    },
                )
            connection.execute(
                """
                INSERT INTO action_campaigns(
                    id, owner_user_id, operation, execution_type, profile_id, message, messages_json,
                    interval_min_seconds, interval_max_seconds, limit_count, status,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    campaign_id,
                    owner_user_id,
                    operation,
                    execution_type,
                    profile_id,
                    primary_message,
                    _json(normalized_messages),
                    interval_min_seconds,
                    interval_max_seconds,
                    limit_count,
                    now,
                    now,
                ),
            )
            for queue_order, (username_norm, username_display, source_target) in enumerate(eligible, start=1):
                reserve_split_identity(
                    connection, username_norm, username_display, now,
                    source="action_target", owner_user_id=owner_user_id,
                )
                connection.execute(
                    """
                    INSERT INTO action_targets(
                        id, campaign_id, username_norm, username_display, source_target,
                        queue_order, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()), campaign_id, username_norm, username_display,
                        source_target, queue_order, now,
                    ),
                )
            _event(
                connection,
                owner_user_id,
                "action_campaign",
                campaign_id,
                "campaign.created",
                {
                    "operation": operation,
                    "target_count": len(eligible),
                    "profile_id": profile_id,
                    "skipped_successful": skipped_successful,
                    "skipped_running": skipped_running,
                },
            )
        return self.get_action_campaign(owner_user_id, campaign_id)

    def fail_unstarted_action_campaign(
        self, owner_user_id: str, campaign_id: str, *, error: str
    ) -> dict[str, Any]:
        """Close a durable campaign whose browser worker never started."""
        now = isoformat()
        message = str(error).strip()[:500] or "Task did not start"
        with self.database.write() as connection:
            campaign = connection.execute(
                "SELECT * FROM action_campaigns WHERE id=? AND owner_user_id=?",
                (campaign_id, owner_user_id),
            ).fetchone()
            if campaign is None:
                raise NotFoundError("Action campaign not found")
            if campaign["status"] == "queued":
                connection.execute(
                    "UPDATE action_targets SET status='failed', control_after_attempt=NULL, last_error=?, updated_at=? WHERE campaign_id=? AND status IN ('pending', 'paused')",
                    (message, now, campaign_id),
                )
                connection.execute(
                    "UPDATE action_campaigns SET status='failed', last_error=?, finished_at=?, updated_at=?, version=version+1 WHERE id=?",
                    (message, now, now, campaign_id),
                )
                _event(connection, owner_user_id, "action_campaign", campaign_id, "campaign.start_failed", {"error": message})
        return self.get_action_campaign(owner_user_id, campaign_id)

    def get_action_campaign(self, owner_user_id: str, campaign_id: str) -> dict[str, Any]:
        with self.database.read() as connection:
            campaign = connection.execute(
                "SELECT * FROM action_campaigns WHERE id=? AND owner_user_id=?",
                (campaign_id, owner_user_id),
            ).fetchone()
            if campaign is None:
                raise NotFoundError("Action campaign not found")
            targets = connection.execute(
                "SELECT * FROM action_targets WHERE campaign_id=? ORDER BY queue_order", (campaign_id,)
            ).fetchall()
            attempts = connection.execute(
                "SELECT * FROM action_attempts WHERE campaign_id=? ORDER BY started_at", (campaign_id,)
            ).fetchall()
        return self._campaign_dict(campaign, targets, attempts)

    def list_action_campaigns(
        self,
        owner_user_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
        detail_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        with self.database.read() as connection:
            bounded_offset = max(0, int(offset))
            if limit is None and bounded_offset == 0:
                rows = connection.execute(
                    """
                    SELECT * FROM action_campaigns WHERE owner_user_id=?
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (owner_user_id,),
                ).fetchall()
            elif limit is None:
                rows = connection.execute(
                    """
                    SELECT * FROM action_campaigns WHERE owner_user_id=?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                    """,
                    (owner_user_id, bounded_offset),
                ).fetchall()
            else:
                bounded_limit = max(1, min(int(limit), 5001))
                rows = connection.execute(
                    """
                    SELECT * FROM action_campaigns
                    WHERE owner_user_id=?
                    ORDER BY CASE
                        WHEN status IN ('queued', 'running', 'paused', 'recoverable')
                        THEN 0 ELSE 1 END,
                        updated_at DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (owner_user_id, bounded_limit, bounded_offset),
                ).fetchall()
            if not rows:
                return []
            campaign_ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in campaign_ids)
            detail_filter = ""
            parameters: list[Any] = [*campaign_ids]
            if detail_limit is not None:
                detail_filter = "WHERE detail_rank<=?"
                detail_budget = max(1, min(int(detail_limit), 50001))
                # A global budget must not let one old campaign starve every
                # sibling, nor become a per-campaign multiplier.
                parameters.append(max(1, detail_budget // len(campaign_ids)))
            # Rank only small identity/sort fields. Reading full historical
            # payloads before applying detail_rank loads every old error/detail
            # into SQLite's window sorter even though only a few are returned.
            targets = connection.execute(
                f"""
                WITH ranked_targets AS (
                    SELECT id, campaign_id, queue_order,
                    ROW_NUMBER() OVER (
                        PARTITION BY campaign_id
                        ORDER BY CASE
                            WHEN status='running' THEN 0
                            WHEN status='unknown' THEN 1
                            WHEN status IN ('failed', 'stopped') THEN 2
                            WHEN status IN ('pending', 'paused') THEN 3
                            WHEN status='dismissed' THEN 4
                            ELSE 5 END,
                            queue_order, id
                    ) AS detail_rank
                    FROM action_targets
                    WHERE campaign_id IN ({placeholders})
                )
                SELECT target.*, ranked_targets.detail_rank
                FROM ranked_targets
                JOIN action_targets target ON target.id=ranked_targets.id
                {detail_filter}
                ORDER BY target.campaign_id, target.queue_order, target.id
                """,
                tuple(parameters),
            ).fetchall()
            target_totals: dict[str, int] = {}
            attempt_totals: dict[str, int] = {}
            if detail_limit is not None:
                target_totals = {
                    row["campaign_id"]: int(row["count"])
                    for row in connection.execute(
                        f"""
                        SELECT campaign_id, COUNT(*) AS count
                        FROM action_targets WHERE campaign_id IN ({placeholders})
                        GROUP BY campaign_id
                        """,
                        tuple(campaign_ids),
                    ).fetchall()
                }
                attempt_totals = {
                    row["campaign_id"]: int(row["count"])
                    for row in connection.execute(
                        f"""
                        SELECT campaign_id, COUNT(*) AS count
                        FROM action_attempts WHERE campaign_id IN ({placeholders})
                        GROUP BY campaign_id
                        """,
                        tuple(campaign_ids),
                    ).fetchall()
                }
            attempts = connection.execute(
                f"""
                WITH ranked_attempts AS (
                    SELECT id, campaign_id, started_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY campaign_id
                        ORDER BY CASE
                            WHEN status='running' THEN 0
                            WHEN status='unknown' THEN 1
                            WHEN status IN ('failed', 'stopped') THEN 2
                            ELSE 3 END,
                            started_at DESC, id DESC
                    ) AS detail_rank
                    FROM action_attempts
                    WHERE campaign_id IN ({placeholders})
                )
                SELECT attempt.*, ranked_attempts.detail_rank
                FROM ranked_attempts
                JOIN action_attempts attempt ON attempt.id=ranked_attempts.id
                {detail_filter}
                ORDER BY attempt.campaign_id, attempt.started_at, attempt.id
                """,
                tuple(parameters),
            ).fetchall()
        targets_by_campaign: dict[str, list[sqlite3.Row]] = {}
        for target in targets:
            targets_by_campaign.setdefault(target["campaign_id"], []).append(target)
        attempts_by_campaign: dict[str, list[sqlite3.Row]] = {}
        for attempt in attempts:
            attempts_by_campaign.setdefault(attempt["campaign_id"], []).append(attempt)
        result: list[dict[str, Any]] = []
        for row in rows:
            campaign = self._campaign_dict(
                row,
                targets_by_campaign.get(row["id"], ()),
                attempts_by_campaign.get(row["id"], ()),
            )
            campaign["targets_truncated"] = (
                target_totals.get(row["id"], 0)
                > len(targets_by_campaign.get(row["id"], ()))
            )
            campaign["attempts_truncated"] = (
                attempt_totals.get(row["id"], 0)
                > len(attempts_by_campaign.get(row["id"], ()))
            )
            result.append(campaign)
        return result

    def count_action_campaigns(self, owner_user_id: str) -> int:
        with self.database.read() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM action_campaigns WHERE owner_user_id=?",
                    (owner_user_id,),
                ).fetchone()[0]
            )

    def count_action_campaigns_by_operation(
        self, owner_user_id: str
    ) -> dict[str, int]:
        """Return exact per-operation counts for page-specific truncation flags."""

        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT operation, COUNT(*) AS count
                FROM action_campaigns
                WHERE owner_user_id=?
                GROUP BY operation
                """,
                (owner_user_id,),
            ).fetchall()
        return {str(row["operation"]): int(row["count"]) for row in rows}

    def find_active_campaign(
        self, owner_user_id: str, *, profile_id: str, operation: str
    ) -> dict[str, Any] | None:
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM action_campaigns
                WHERE owner_user_id=? AND profile_id=? AND operation=? AND status IN ('queued', 'running', 'paused')
                ORDER BY created_at DESC LIMIT 1
                """,
                (owner_user_id, profile_id, operation),
            ).fetchone()
        return self._campaign_dict(row, (), ()) if row else None

    def set_campaign_status(
        self,
        owner_user_id: str,
        campaign_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"queued", "running", "paused", "completed", "failed", "recoverable", "stopped"}:
            raise ValidationError("Invalid action campaign status")
        now = isoformat()
        with self.database.write() as connection:
            campaign = connection.execute(
                "SELECT * FROM action_campaigns WHERE id=? AND owner_user_id=?",
                (campaign_id, owner_user_id),
            ).fetchone()
            if campaign is None:
                raise NotFoundError("Action campaign not found")
            deferred_attempts = 0
            failed_pending_targets = 0
            deferred_running_targets = 0
            if status in {"failed", "stopped"}:
                terminal_message = (
                    str(error).strip()[:500]
                    if error and str(error).strip()
                    else "Action campaign stopped before this target was completed"
                )
                cursor = connection.execute(
                    """
                    UPDATE action_targets
                    SET control_after_attempt='cancel', last_error=?, updated_at=?
                    WHERE campaign_id=? AND status='running'
                    """,
                    (terminal_message, now, campaign_id),
                )
                deferred_running_targets = int(cursor.rowcount)
                deferred_attempts = deferred_running_targets
                cursor = connection.execute(
                    """
                    UPDATE action_targets
                    SET status='failed', control_after_attempt=NULL,
                        last_error=?, updated_at=?
                    WHERE campaign_id=? AND status IN ('pending', 'paused')
                    """,
                    (terminal_message, now, campaign_id),
                )
                failed_pending_targets = int(cursor.rowcount)
            started_at = campaign["started_at"] or (now if status == "running" else None)
            finished_at = now if status in {"completed", "failed", "stopped"} else None
            connection.execute(
                """
                UPDATE action_campaigns
                SET status=?, updated_at=?, started_at=?, finished_at=?, last_error=?, version=version+1
                WHERE id=?
                """,
                (status, now, started_at, finished_at, error, campaign_id),
            )
            _event(
                connection,
                owner_user_id,
                "action_campaign",
                campaign_id,
                "campaign.status",
                {
                    "from": campaign["status"],
                    "to": status,
                    "error": error,
                    "failed_pending_targets": failed_pending_targets,
                    "deferred_running_targets": deferred_running_targets,
                    "deferred_attempts": deferred_attempts,
                },
            )
        return self.get_action_campaign(owner_user_id, campaign_id)

    def dismiss_action_failure(
        self,
        owner_user_id: str,
        campaign_id: str,
        target_id: str,
    ) -> dict[str, Any]:
        """Dismiss one known failed item and return its account to approved waiting.

        The target row and attempts remain as audit data.  ``dismissed`` means only
        that the failure card is no longer active; it never erases global dedupe or
        a successful attempt.  An UNKNOWN outcome deliberately cannot use this
        path: a person must first say whether the browser action actually happened.
        """

        now = isoformat()
        with self.database.write() as connection:
            target = connection.execute(
                """
                SELECT target.*, campaign.owner_user_id
                FROM action_targets target
                JOIN action_campaigns campaign ON campaign.id=target.campaign_id
                WHERE target.id=? AND target.campaign_id=?
                  AND campaign.owner_user_id=?
                """,
                (target_id, campaign_id, owner_user_id),
            ).fetchone()
            if target is None:
                raise NotFoundError("Action failure target not found")
            if target["status"] == "dismissed":
                revision = int(
                    connection.execute(
                        "SELECT revision FROM workbench_state_revision WHERE singleton_id=1"
                    ).fetchone()[0]
                )
                return {
                    "campaign_id": campaign_id,
                    "target_id": target_id,
                    "username": target["username_display"],
                    "status": "dismissed",
                    "returned_to_approved": True,
                    "global_dedupe_retained": True,
                    "snapshot_seq": revision,
                }
            if target["status"] == "unknown":
                raise ConflictError(
                    "An unknown action outcome requires explicit confirmation",
                    details={
                        "status": "unknown",
                        "resolution_required": True,
                        "allowed_outcomes": ["not_completed", "completed"],
                    },
                )
            if target["status"] not in {"failed", "stopped"}:
                raise ConflictError(
                    "Only an unfinished failed action can be dismissed",
                    details={"status": target["status"]},
                )
            successful = connection.execute(
                """
                SELECT 1 FROM action_attempts
                WHERE target_id=? AND status IN ('confirmed', 'already_done')
                LIMIT 1
                """,
                (target_id,),
            ).fetchone()
            if successful is not None:
                raise ConflictError("A successful action history cannot be dismissed")
            connection.execute(
                """
                UPDATE action_targets
                SET status='dismissed', last_error=NULL, updated_at=?
                WHERE id=?
                """,
                (now, target_id),
            )
            approved = connection.execute(
                """
                SELECT candidate.id
                FROM instagram_username_aliases alias
                JOIN workbench_candidates candidate ON candidate.account_id=alias.account_id
                WHERE alias.username_norm=? AND candidate.owner_user_id=?
                  AND candidate.status='approved'
                LIMIT 1
                """,
                (target["username_norm"], owner_user_id),
            ).fetchone()
            revision = _bump_workbench_revision(connection, now)
            _event(
                connection,
                owner_user_id,
                "action_campaign",
                campaign_id,
                "campaign.failure_dismissed",
                {
                    "target_id": target_id,
                    "username": target["username_norm"],
                    "approved_candidate_id": approved["id"] if approved else None,
                    "global_dedupe_retained": True,
                },
            )
        return {
            "campaign_id": campaign_id,
            "target_id": target_id,
            "username": target["username_display"],
            "status": "dismissed",
            "returned_to_approved": approved is not None,
            "approved_candidate_id": approved["id"] if approved else None,
            "global_dedupe_retained": True,
            "snapshot_seq": revision,
        }

    def resolve_unknown_action(
        self,
        owner_user_id: str,
        campaign_id: str,
        target_id: str,
        *,
        outcome: str,
    ) -> dict[str, Any]:
        """Resolve an indeterminate browser action without risking a duplicate.

        ``not_completed`` retires only the failed action row and makes the approved
        candidate selectable again.  ``completed`` records a permanent success
        before the approved projection is recomputed.  Both decisions are atomic
        and remain visible through the attempt/event history.
        """

        if outcome not in {"not_completed", "completed"}:
            raise ValidationError("Invalid unknown action resolution")
        now = isoformat()
        with self.database.write() as connection:
            target = connection.execute(
                """
                SELECT target.*, campaign.owner_user_id,
                       campaign.operation, campaign.status AS campaign_status
                FROM action_targets target
                JOIN action_campaigns campaign ON campaign.id=target.campaign_id
                WHERE target.id=? AND target.campaign_id=?
                  AND campaign.owner_user_id=?
                """,
                (target_id, campaign_id, owner_user_id),
            ).fetchone()
            if target is None:
                raise NotFoundError("Unknown action target not found")

            ledger = connection.execute(
                """
                SELECT * FROM action_success_ledger
                WHERE owner_user_id=? AND operation=? AND username_norm=?
                """,
                (owner_user_id, target["operation"], target["username_norm"]),
            ).fetchone()
            if target["status"] in {"confirmed", "already_done"} and outcome == "completed":
                revision = int(
                    connection.execute(
                        "SELECT revision FROM workbench_state_revision WHERE singleton_id=1"
                    ).fetchone()[0]
                )
                return {
                    "campaign_id": campaign_id,
                    "target_id": target_id,
                    "username": target["username_display"],
                    "operation": target["operation"],
                    "outcome": outcome,
                    "status": target["status"],
                    "returned_to_approved": False,
                    "success_ledger_recorded": ledger is not None,
                    "global_dedupe_retained": True,
                    "snapshot_seq": revision,
                }
            if target["status"] == "dismissed" and outcome == "not_completed":
                revision = int(
                    connection.execute(
                        "SELECT revision FROM workbench_state_revision WHERE singleton_id=1"
                    ).fetchone()[0]
                )
                approved = connection.execute(
                    """
                    SELECT candidate.id
                    FROM instagram_username_aliases alias
                    JOIN workbench_candidates candidate ON candidate.account_id=alias.account_id
                    LEFT JOIN workbench_candidate_dismissals dismissal
                      ON dismissal.candidate_id=candidate.id
                    WHERE alias.username_norm=? AND candidate.owner_user_id=?
                      AND candidate.status='approved' AND dismissal.id IS NULL
                    LIMIT 1
                    """,
                    (target["username_norm"], owner_user_id),
                ).fetchone()
                return {
                    "campaign_id": campaign_id,
                    "target_id": target_id,
                    "username": target["username_display"],
                    "operation": target["operation"],
                    "outcome": outcome,
                    "status": "dismissed",
                    "returned_to_approved": approved is not None,
                    "approved_candidate_id": approved["id"] if approved else None,
                    "success_ledger_recorded": False,
                    "global_dedupe_retained": True,
                    "snapshot_seq": revision,
                }
            if target["status"] != "unknown":
                raise ConflictError(
                    "Only an unknown action outcome can be resolved",
                    details={"status": target["status"], "outcome": outcome},
                )

            attempt = connection.execute(
                """
                SELECT * FROM action_attempts
                WHERE target_id=? AND status='unknown'
                ORDER BY attempt_number DESC LIMIT 1
                """,
                (target_id,),
            ).fetchone()
            approved = None
            if outcome == "not_completed":
                connection.execute(
                    """
                    UPDATE action_targets
                    SET status='dismissed', control_after_attempt=NULL,
                        last_error=NULL, updated_at=?
                    WHERE id=? AND status='unknown'
                    """,
                    (now, target_id),
                )
                approved = connection.execute(
                    """
                    SELECT candidate.id
                    FROM instagram_username_aliases alias
                    JOIN workbench_candidates candidate ON candidate.account_id=alias.account_id
                    LEFT JOIN workbench_candidate_dismissals dismissal
                      ON dismissal.candidate_id=candidate.id
                    WHERE alias.username_norm=? AND candidate.owner_user_id=?
                      AND candidate.status='approved' AND dismissal.id IS NULL
                    LIMIT 1
                    """,
                    (target["username_norm"], owner_user_id),
                ).fetchone()
                event_type = "campaign.unknown_confirmed_not_completed"
                status = "dismissed"
                success_recorded = False
            else:
                if attempt is None:
                    attempt_id = str(uuid.uuid4())
                    attempt_number = int(
                        connection.execute(
                            "SELECT COUNT(*) + 1 FROM action_attempts WHERE target_id=?",
                            (target_id,),
                        ).fetchone()[0]
                    )
                    connection.execute(
                        """
                        INSERT INTO action_attempts(
                            id, campaign_id, target_id, attempt_number, status,
                            started_at, finished_at, details_json
                        ) VALUES(?, ?, ?, ?, 'confirmed', ?, ?, ?)
                        """,
                        (
                            attempt_id,
                            campaign_id,
                            target_id,
                            attempt_number,
                            now,
                            now,
                            _json({
                                "manual_resolution": "completed",
                                "previous_status": "unknown",
                            }),
                        ),
                    )
                else:
                    attempt_id = attempt["id"]
                    details = _loads(attempt["details_json"])
                    if not isinstance(details, dict):
                        details = {}
                    details.update(
                        {
                            "manual_resolution": "completed",
                            "previous_status": "unknown",
                            "resolved_at": now,
                        }
                    )
                    connection.execute(
                        """
                        UPDATE action_attempts
                        SET status='confirmed', finished_at=?, details_json=?
                        WHERE id=? AND status='unknown'
                        """,
                        (now, _json(details), attempt_id),
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO action_success_ledger(
                        owner_user_id, operation, username_norm, username_display,
                        campaign_id, target_id, attempt_id, completed_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner_user_id,
                        target["operation"],
                        target["username_norm"],
                        target["username_display"],
                        campaign_id,
                        target_id,
                        attempt_id,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE action_targets
                    SET status='confirmed', control_after_attempt=NULL,
                        last_error=NULL, updated_at=?
                    WHERE id=? AND status='unknown'
                    """,
                    (now, target_id),
                )
                event_type = "campaign.unknown_confirmed_completed"
                status = "confirmed"
                success_recorded = True
                if target['operation'] == 'follow':
                    from .private_follow_reviews import capture_private_follow_completions
                    capture_private_follow_completions(connection, owner_user_id, attempt_id, target['username_norm'])

            revision = _bump_workbench_revision(connection, now)
            _event(
                connection,
                owner_user_id,
                "action_campaign",
                campaign_id,
                event_type,
                {
                    "target_id": target_id,
                    "username": target["username_norm"],
                    "operation": target["operation"],
                    "outcome": outcome,
                    "approved_candidate_id": approved["id"] if approved else None,
                    "success_ledger_recorded": success_recorded,
                    "global_dedupe_retained": True,
                },
            )
        return {
            "campaign_id": campaign_id,
            "target_id": target_id,
            "username": target["username_display"],
            "operation": target["operation"],
            "outcome": outcome,
            "status": status,
            "returned_to_approved": approved is not None,
            "approved_candidate_id": approved["id"] if approved else None,
            "success_ledger_recorded": success_recorded,
            "global_dedupe_retained": True,
            "snapshot_seq": revision,
        }

    def control_action_target(
        self,
        owner_user_id: str,
        campaign_id: str,
        target_id: str,
        *,
        action: str,
    ) -> dict[str, Any]:
        """Control exactly one target without changing its sibling targets."""

        if action not in {"pause", "resume", "cancel"}:
            raise ValidationError("Invalid action target control")
        now = isoformat()
        with self.database.write() as connection:
            target = connection.execute(
                """
                SELECT target.*, campaign.owner_user_id, campaign.status AS campaign_status
                FROM action_targets target
                JOIN action_campaigns campaign ON campaign.id=target.campaign_id
                WHERE target.id=? AND target.campaign_id=?
                  AND campaign.owner_user_id=?
                """,
                (target_id, campaign_id, owner_user_id),
            ).fetchone()
            if target is None:
                raise NotFoundError("Action target not found")

            prior_status = target["status"]
            deferred = False
            deferred_action: str | None = None
            if prior_status == "running":
                if action == "resume":
                    raise ConflictError(
                        "A running target is already active",
                        details={"status": prior_status},
                    )
                deferred = True
                deferred_action = action
                connection.execute(
                    """
                    UPDATE action_targets
                    SET control_after_attempt=?, updated_at=?
                    WHERE id=? AND status='running'
                    """,
                    (action, now, target_id),
                )
                new_status = "running"
            elif action == "pause" and prior_status in {"pending", "paused"}:
                new_status = "paused"
                connection.execute(
                    """
                    UPDATE action_targets
                    SET status='paused', control_after_attempt=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (now, target_id),
                )
            elif action == "resume" and prior_status in {"paused", "pending"}:
                new_status = "pending"
                connection.execute(
                    """
                    UPDATE action_targets
                    SET status='pending', control_after_attempt=NULL,
                        last_error=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (now, target_id),
                )
            elif action == "cancel" and prior_status in {"pending", "paused"}:
                new_status = "failed"
                connection.execute(
                    """
                    UPDATE action_targets
                    SET status='failed', control_after_attempt=NULL,
                        last_error=?, updated_at=?
                    WHERE id=?
                    """,
                    ("任务在实际执行前已取消，可以直接删除并退回待执行", now, target_id),
                )
            else:
                raise ConflictError(
                    "This action target can no longer be controlled",
                    details={"status": prior_status, "action": action},
                )

            revision = _bump_workbench_revision(connection, now)
            _event(
                connection,
                owner_user_id,
                "action_campaign",
                campaign_id,
                "campaign.target_control",
                {
                    "target_id": target_id,
                    "action": action,
                    "from": prior_status,
                    "to": new_status,
                    "deferred": deferred,
                },
            )
        return {
            "campaign_id": campaign_id,
            "target_id": target_id,
            "action": action,
            "status": new_status,
            "deferred": deferred,
            "deferred_action": deferred_action,
            "success_confirmation_has_priority": deferred,
            "snapshot_seq": revision,
        }

    def interrupt_action_campaign(
        self,
        owner_user_id: str,
        campaign_id: str,
        *,
        error: str,
        final_status: str = "paused",
    ) -> dict[str, Any]:
        """Fence an interrupted action and make any in-flight outcome UNKNOWN.

        A recoverable process interruption remains paused so an operator can
        inspect UNKNOWN outcomes.  UNKNOWN always has priority over a requested
        terminal failure: the campaign stays paused and unstarted siblings remain
        pending until the operator resolves the uncertain action.  A failure with
        no in-flight/UNKNOWN action remains terminal and retires pending targets.
        """

        if final_status not in {"paused", "failed"}:
            raise ValidationError("Invalid interrupted action final status")
        now = isoformat()
        with self.database.write() as connection:
            campaign = connection.execute(
                "SELECT * FROM action_campaigns WHERE id=? AND owner_user_id=?",
                (campaign_id, owner_user_id),
            ).fetchone()
            if campaign is None:
                raise NotFoundError("Action campaign not found")
            running_attempts = connection.execute(
                "SELECT id, target_id, details_json FROM action_attempts WHERE campaign_id=? AND status='running'",
                (campaign_id,),
            ).fetchall()
            has_running_target = connection.execute(
                "SELECT 1 FROM action_targets WHERE campaign_id=? AND status='running' LIMIT 1",
                (campaign_id,),
            ).fetchone() is not None
            has_existing_unknown = connection.execute(
                "SELECT 1 FROM action_targets WHERE campaign_id=? AND status='unknown' LIMIT 1",
                (campaign_id,),
            ).fetchone() is not None
            dispatch_claim_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM action_dispatch_claims WHERE campaign_id=?",
                    (campaign_id,),
                ).fetchone()[0]
            )
            if (
                not running_attempts
                and not has_running_target
                and dispatch_claim_count == 0
                and (
                    campaign["status"] in {"completed", "failed", "stopped"}
                    or (
                        campaign["status"] == "paused"
                        and has_existing_unknown
                    )
                )
            ):
                targets = connection.execute(
                    "SELECT * FROM action_targets WHERE campaign_id=? ORDER BY queue_order",
                    (campaign_id,),
                ).fetchall()
                attempts = connection.execute(
                    "SELECT * FROM action_attempts WHERE campaign_id=? ORDER BY started_at",
                    (campaign_id,),
                ).fetchall()
                return self._campaign_dict(campaign, targets, attempts)
            for attempt in running_attempts:
                details = _loads(attempt["details_json"])
                if not isinstance(details, dict):
                    details = {}
                details.update({"reason": "execution_interrupted", "message": error})
                connection.execute(
                    "UPDATE action_attempts SET status='unknown', finished_at=?, details_json=? WHERE id=?",
                    (now, _json(details), attempt["id"]),
                )
                connection.execute(
                    "UPDATE action_targets SET status='unknown', control_after_attempt=NULL, last_error=?, updated_at=? WHERE id=?",
                    (error, now, attempt["target_id"]),
                )
            # Defensive convergence for a target whose running attempt row was
            # lost or never became visible.  Its external outcome is uncertain,
            # so it must never be converted to an ordinary retryable failure.
            orphan_running_cursor = connection.execute(
                """
                UPDATE action_targets
                SET status='unknown', control_after_attempt=NULL,
                    last_error=?, updated_at=?
                WHERE campaign_id=? AND status='running'
                """,
                (error, now, campaign_id),
            )
            has_unknown = connection.execute(
                """
                SELECT 1 FROM action_targets
                WHERE campaign_id=? AND status='unknown'
                LIMIT 1
                """,
                (campaign_id,),
            ).fetchone() is not None
            # A user's explicit stop remains the campaign-level terminal intent.
            # We still fence its in-flight row as UNKNOWN and release the dispatch
            # claim, but must not silently turn a stopped campaign back into a
            # resumable paused campaign.
            effective_final_status = (
                "stopped"
                if campaign["status"] == "stopped"
                else ("paused" if has_unknown else final_status)
            )
            failed_pending_targets = 0
            if effective_final_status == "failed":
                pending_cursor = connection.execute(
                    """
                    UPDATE action_targets
                    SET status='failed', control_after_attempt=NULL,
                        last_error=?, updated_at=?
                    WHERE campaign_id=? AND status IN ('pending', 'paused')
                    """,
                    (error, now, campaign_id),
                )
                failed_pending_targets = int(pending_cursor.rowcount)
            released_dispatch_claims = int(
                connection.execute(
                    "DELETE FROM action_dispatch_claims WHERE campaign_id=?",
                    (campaign_id,),
                ).rowcount
            )
            connection.execute(
                """
                UPDATE action_campaigns
                SET status=?, updated_at=?, finished_at=?, last_error=?, version=version+1
                WHERE id=?
                """,
                (
                    effective_final_status,
                    now,
                    (
                        campaign["finished_at"] or now
                        if effective_final_status == "stopped"
                        else (now if effective_final_status == "failed" else None)
                    ),
                    error,
                    campaign_id,
                ),
            )
            _event(
                connection,
                owner_user_id,
                "action_campaign",
                campaign_id,
                "campaign.interrupted",
                {
                    "unknown_attempts": len(running_attempts),
                    "orphan_running_targets": int(orphan_running_cursor.rowcount),
                    "failed_pending_targets": failed_pending_targets,
                    "released_dispatch_claims": released_dispatch_claims,
                    "requested_final_status": final_status,
                    "final_status": effective_final_status,
                    "error": error,
                },
            )
        return self.get_action_campaign(owner_user_id, campaign_id)

    def next_action_target(self, owner_user_id: str, campaign_id: str) -> dict[str, Any] | None:
        now = isoformat()
        with self.database.write() as connection:
            campaign = connection.execute(
                "SELECT operation FROM action_campaigns WHERE id=? AND owner_user_id=?",
                (campaign_id, owner_user_id),
            ).fetchone()
            if campaign is None:
                raise NotFoundError("Action campaign not found")
            # Reconcile rows queued before another campaign committed the same
            # success.  This permanent ledger, not a bounded UI history list, is
            # authoritative for automatic-action dedupe.
            connection.execute(
                """
                UPDATE action_targets
                SET status='already_done', last_error=NULL,
                    control_after_attempt=NULL, updated_at=?
                WHERE campaign_id=? AND status='pending'
                  AND EXISTS(
                      SELECT 1 FROM action_success_ledger success
                      WHERE success.owner_user_id=?
                        AND success.operation=?
                        AND success.username_norm=action_targets.username_norm
                  )
                """,
                (now, campaign_id, owner_user_id, campaign["operation"]),
            )
            row = connection.execute(
                "SELECT * FROM action_targets WHERE campaign_id=? AND status='pending' ORDER BY queue_order LIMIT 1",
                (campaign_id,),
            ).fetchone()
        return self._action_target_dict(row) if row else None

    def start_action_attempt(
        self,
        owner_user_id: str,
        campaign_id: str,
        target_id: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> str:
        initial_details = dict(details or {})
        _reject_sensitive_fields(initial_details, "action_details")
        initial_details.pop('private_follow_profile', None)
        now = isoformat()
        attempt_id = str(uuid.uuid4())
        with self.database.write() as connection:
            campaign = connection.execute(
                "SELECT operation FROM action_campaigns WHERE id=? AND owner_user_id=?",
                (campaign_id, owner_user_id),
            ).fetchone()
            if campaign is None:
                raise NotFoundError("Action campaign not found")
            target = connection.execute(
                "SELECT * FROM action_targets WHERE id=? AND campaign_id=?", (target_id, campaign_id)
            ).fetchone()
            if target is None:
                raise NotFoundError("Action target not found")
            if target["status"] != "pending":
                raise ConflictError(
                    "Action target is not pending; automatic retry is disabled",
                    details={"status": target["status"]},
                )
            if campaign['operation'] == 'follow':
                from .private_follow_reviews import private_candidate_snapshot
                snapshot = private_candidate_snapshot(connection, owner_user_id, target['username_norm'])
                if snapshot is not None:
                    initial_details['private_follow_profile'] = snapshot
            if connection.execute(
                """
                SELECT 1 FROM action_success_ledger
                WHERE owner_user_id=? AND operation=? AND username_norm=?
                """,
                (owner_user_id, campaign["operation"], target["username_norm"]),
            ).fetchone() is not None:
                raise ConflictError(
                    "Action target already has an immutable success record",
                    details={"status": "already_done"},
                )
            attempt_number = connection.execute(
                "SELECT COUNT(*) + 1 FROM action_attempts WHERE target_id=?", (target_id,)
            ).fetchone()[0]
            try:
                connection.execute(
                    """
                    INSERT INTO action_dispatch_claims(
                        owner_user_id, operation, username_norm, campaign_id,
                        target_id, attempt_id, claimed_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner_user_id,
                        campaign["operation"],
                        target["username_norm"],
                        campaign_id,
                        target_id,
                        attempt_id,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "The same account action is already executing",
                    details={"status": "dispatching"},
                ) from exc
            connection.execute(
                "UPDATE action_targets SET status='running', control_after_attempt=NULL, last_error=NULL, updated_at=? WHERE id=?",
                (now, target_id),
            )
            connection.execute(
                """
                INSERT INTO action_attempts(
                    id, campaign_id, target_id, attempt_number, status, started_at, details_json
                ) VALUES(?, ?, ?, ?, 'running', ?, ?)
                """,
                (attempt_id, campaign_id, target_id, attempt_number, now, _json(initial_details)),
            )
        return attempt_id

    def finish_action_attempt(
        self,
        owner_user_id: str,
        campaign_id: str,
        attempt_id: str,
        *,
        status: str,
        details: dict[str, Any],
    ) -> None:
        if status not in {"confirmed", "already_done", "failed", "unknown"}:
            raise ValidationError("Invalid action attempt status")
        _reject_sensitive_fields(details, "action_details")
        now = isoformat()
        with self.database.write() as connection:
            campaign = connection.execute(
                "SELECT operation FROM action_campaigns WHERE id=? AND owner_user_id=?",
                (campaign_id, owner_user_id),
            ).fetchone()
            if campaign is None:
                raise NotFoundError("Action campaign not found")
            attempt = connection.execute(
                "SELECT * FROM action_attempts WHERE id=? AND campaign_id=?", (attempt_id, campaign_id)
            ).fetchone()
            if attempt is None:
                raise NotFoundError("Action attempt not found")
            if attempt["status"] != "running":
                # Shutdown/heartbeat can atomically fence the attempt before the
                # worker unwinds and reports the same UNKNOWN result.  Treat that
                # exact convergence as idempotent while preserving the first fence
                # reason.  No other terminal result may overwrite UNKNOWN.
                if status == "unknown" and attempt["status"] == "unknown":
                    connection.execute(
                        "DELETE FROM action_dispatch_claims WHERE attempt_id=?",
                        (attempt_id,),
                    )
                    return
                raise ConflictError("Action attempt is already finalized")
            stored_details = _loads(attempt["details_json"])
            if not isinstance(stored_details, dict):
                stored_details = {}
            stored_details.update({key: value for key, value in details.items() if key != 'private_follow_profile'})
            target = connection.execute(
                "SELECT * FROM action_targets WHERE id=?",
                (attempt["target_id"],),
            ).fetchone()
            deferred_action = target["control_after_attempt"] if target is not None else None
            if deferred_action:
                stored_details["deferred_control"] = deferred_action
                stored_details["success_confirmation_has_priority"] = True
            error = stored_details.get("message") if status in {"failed", "unknown"} else None
            connection.execute(
                "UPDATE action_attempts SET status=?, finished_at=?, details_json=? WHERE id=?",
                (status, now, _json(stored_details), attempt_id),
            )
            connection.execute(
                "UPDATE action_targets SET status=?, control_after_attempt=NULL, last_error=?, updated_at=? WHERE id=?",
                (status, error, now, attempt["target_id"]),
            )
            if status in {"confirmed", "already_done"} and target is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO action_success_ledger(
                        owner_user_id, operation, username_norm, username_display,
                        campaign_id, target_id, attempt_id, completed_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner_user_id,
                        campaign["operation"],
                        target["username_norm"],
                        target["username_display"],
                        campaign_id,
                        attempt["target_id"],
                        attempt_id,
                        now,
                    ),
                )
            connection.execute(
                "DELETE FROM action_dispatch_claims WHERE attempt_id=?",
                (attempt_id,),
            )
            if status == 'confirmed' and campaign['operation'] == 'follow':
                from .private_follow_reviews import capture_private_follow_completions
                capture_private_follow_completions(connection, owner_user_id, attempt_id, target['username_norm'])
            _bump_workbench_revision(connection, now)

    def list_action_counter_resets(
        self, owner_user_id: str, *, operation: str
    ) -> list[dict[str, Any]]:
        if operation not in {"follow", "greet"}:
            raise ValidationError("Invalid action operation")
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT profile_id, operation, reset_at
                FROM action_counter_resets
                WHERE owner_user_id=? AND operation=?
                ORDER BY reset_at DESC
                """,
                (owner_user_id, operation),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_window_action_counts(
        self,
        owner_user_id: str,
        profile_ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        """Return exact ledger counts for a bounded set of visible windows.

        The caller supplies the current BitBrowser inventory, so the result size is
        bounded by real UI rows.  Each SQL aggregate still scans the authoritative
        success ledger and is independent of campaign/history pagination.
        """

        normalized: list[str] = []
        for raw_profile_id in profile_ids:
            profile_id = str(raw_profile_id).strip()
            if (
                profile_id
                and len(profile_id) <= 128
                and profile_id not in normalized
            ):
                normalized.append(profile_id)
        if not normalized:
            return []
        if len(normalized) > 5000:
            normalized = normalized[:5000]
        result = {
            profile_id: {
                "profile_id": profile_id,
                "greet_successes": 0,
                "follow_successes": 0,
                "total_successes": 0,
                "greet_current": 0,
                "follow_current": 0,
                "greet_reset_at": None,
                "follow_reset_at": None,
            }
            for profile_id in normalized
        }
        with self.database.read() as connection:
            for offset in range(0, len(normalized), 400):
                batch = normalized[offset : offset + 400]
                placeholders = ",".join("?" for _ in batch)
                count_rows = connection.execute(
                    f"""
                    SELECT campaign.profile_id, success.operation,
                           COUNT(*) AS permanent_count,
                           SUM(
                               CASE
                                   WHEN reset.reset_at IS NULL
                                     OR success.completed_at > reset.reset_at
                                   THEN 1 ELSE 0
                               END
                           ) AS current_count,
                           reset.reset_at
                    FROM action_success_ledger success
                    JOIN action_campaigns campaign
                      ON campaign.id=success.campaign_id
                    LEFT JOIN action_counter_resets reset
                      ON reset.owner_user_id=success.owner_user_id
                     AND reset.profile_id=campaign.profile_id
                     AND reset.operation=success.operation
                    WHERE success.owner_user_id=?
                      AND campaign.owner_user_id=?
                      AND campaign.profile_id IN ({placeholders})
                    GROUP BY campaign.profile_id, success.operation, reset.reset_at
                    """,
                    (owner_user_id, owner_user_id, *batch),
                ).fetchall()
                reset_rows = connection.execute(
                    f"""
                    SELECT profile_id, operation, reset_at
                    FROM action_counter_resets
                    WHERE owner_user_id=? AND profile_id IN ({placeholders})
                    """,
                    (owner_user_id, *batch),
                ).fetchall()
                for row in count_rows:
                    item = result[row["profile_id"]]
                    operation = row["operation"]
                    permanent = int(row["permanent_count"])
                    current = int(row["current_count"])
                    item[f"{operation}_successes"] = permanent
                    item[f"{operation}_current"] = current
                    item[f"{operation}_reset_at"] = row["reset_at"]
                    item["total_successes"] += permanent
                for row in reset_rows:
                    item = result[row["profile_id"]]
                    item[f"{row['operation']}_reset_at"] = row["reset_at"]
        return [result[profile_id] for profile_id in normalized]

    def reset_action_counter(
        self, owner_user_id: str, *, operation: str, profile_id: str
    ) -> dict[str, Any]:
        if operation not in {"follow", "greet"}:
            raise ValidationError("Invalid action operation")
        profile_id = profile_id.strip()
        if not profile_id or len(profile_id) > 128:
            raise ValidationError("Invalid BitBrowser profile id")
        now = isoformat()
        with self.database.write() as connection:
            if connection.execute(
                "SELECT 1 FROM app_users WHERE id=?", (owner_user_id,)
            ).fetchone() is None:
                raise AuthenticationError("Application user no longer exists")
            connection.execute(
                """
                INSERT INTO action_counter_resets(owner_user_id, operation, profile_id, reset_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(owner_user_id, operation, profile_id)
                DO UPDATE SET reset_at=excluded.reset_at
                """,
                (owner_user_id, operation, profile_id, now),
            )
            _event(
                connection,
                owner_user_id,
                "action_counter",
                profile_id,
                "counter.reset",
                {"operation": operation, "reset_at": now},
            )
        return {"operation": operation, "profile_id": profile_id, "reset_at": now}

    # Split-account recovery pool --------------------------------------
    @staticmethod
    def _split_claim_locked(connection: sqlite3.Connection, owner_user_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM collection_dispatch_locks WHERE owner_user_id=? AND locked=1",
            (owner_user_id,),
        ).fetchone() is not None

    def get_split_claim_locked(self, owner_user_id: str) -> bool:
        with self.database.read() as connection:
            return self._split_claim_locked(connection, owner_user_id)

    def collection_dispatch_state(self, owner_user_id: str) -> dict[str, Any]:
        with self.database.read() as connection:
            return {
                "locked": self._split_claim_locked(connection, owner_user_id),
                "usernames": {row[0] for row in connection.execute(
                    "SELECT username_norm FROM split_candidates WHERE owner_user_id=? AND dispatch_locked=1",
                    (owner_user_id,),
                )},
            }

    @staticmethod
    def _guard_locked_split_username(connection: sqlite3.Connection, owner_user_id: str, username_norm: str) -> None:
        if connection.execute(
            "SELECT 1 FROM split_candidates WHERE owner_user_id=? AND username_norm=? AND dispatch_locked=1",
            (owner_user_id, username_norm),
        ).fetchone() is not None:
            raise ConflictError("该分裂号已锁定，不能转入任务；请先解锁", details={"reason": "collection_dispatch_locked"})

    def _guard_collection_dispatch(self, connection: sqlite3.Connection, owner_user_id: str, username_norm: str) -> None:
        if self._split_claim_locked(connection, owner_user_id) or connection.execute(
            "SELECT 1 FROM split_candidates WHERE owner_user_id=? AND username_norm=? AND dispatch_locked=1",
            (owner_user_id, username_norm),
        ).fetchone() is not None:
            raise ConflictError(
                "分裂号领取已锁定；当前目标继续执行，新目标等待解锁",
                details={"reason": "collection_dispatch_locked"},
            )

    def set_split_claim_locked(self, owner_user_id: str, locked: bool) -> dict[str, Any]:
        if not isinstance(locked, bool):
            raise ValidationError("locked must be a boolean")
        with self.database.write() as connection:
            if connection.execute("SELECT 1 FROM app_users WHERE id=?", (owner_user_id,)).fetchone() is None:
                raise AuthenticationError("Application user no longer exists")
            connection.execute(
                "INSERT INTO collection_dispatch_locks(owner_user_id,locked,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(owner_user_id) DO UPDATE SET locked=excluded.locked,updated_at=excluded.updated_at",
                (owner_user_id, int(locked), isoformat()),
            )
            _event(connection, owner_user_id, "collection_dispatch", owner_user_id,
                   "collection_dispatch.lock_changed", {"locked": locked})
        return {"locked": locked}

    def set_split_candidate_locked(self, owner_user_id: str, candidate_id: str, locked: bool) -> dict[str, Any]:
        if not isinstance(locked, bool):
            raise ValidationError("locked must be a boolean")
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT * FROM split_candidates WHERE id=? AND owner_user_id=? AND candidate_kind='manual'",
                (candidate_id, owner_user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("Split candidate not found")
            if row["queue_state"] != "queued" or row["queued_target_id"] is not None:
                raise ConflictError("该分裂号已被领取或不在等待列表，不能更改领取锁")
            connection.execute("UPDATE split_candidates SET dispatch_locked=?,updated_at=? WHERE id=?", (int(locked), isoformat(), candidate_id))
            _event(connection, owner_user_id, "split_candidate", candidate_id,
                   "split_candidate.lock_changed", {"locked": locked})
            updated = connection.execute(
                "SELECT candidate.*, COALESCE((SELECT json_group_array(profile_id) FROM "
                "(SELECT profile_id FROM split_candidate_window_affinity WHERE candidate_id=? ORDER BY queue_order)), '[]') AS allowed_window_ids_json "
                "FROM split_candidates candidate WHERE id=?", (candidate_id, candidate_id),
            ).fetchone()
            return self._split_candidate_dict(updated)

    def upsert_manual_split_candidates(
        self,
        owner_user_id: str,
        candidates: list[dict[str, Any]],
        *,
        include_outcome: bool = False,
        allow_completed: bool = False,
        platform: str = "instagram",
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Persist new split generations and report every blocked duplicate.

        A live generation, immutable completion, or pending failure is authoritative.
        Finished usernames may start a new generation after explicit confirmation.
        Re-adding the same username does not rebind it; an explicit queued request may
        atomically promote that same live available generation, while other duplicates
        return their current disposition for resolution in the appropriate UI.
        ``include_outcome`` returns only submitted/affected generations together
        with accepted ids and duplicate details.  The default list return remains
        available for legacy internal callers.
        """
        validate_platform(platform)
        if not candidates:
            raise ValidationError("At least one split candidate is required")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in candidates:
            username_norm, username_display = normalize_instagram_username(str(raw.get("username") or ""))
            assert_username_platform(platform, username_norm)
            if username_norm in seen:
                continue
            seen.add(username_norm)
            source_target = str(raw.get("source_target") or "").strip()[:150] or None
            profile = raw.get("profile") if isinstance(raw.get("profile"), dict) else {}
            _reject_sensitive_fields(profile, "split_candidate_profile")
            raw_allowed_window_ids = raw.get("allowed_window_ids")
            allowed_window_ids = (
                None
                if raw_allowed_window_ids is None
                else _normalize_allowed_window_ids(raw_allowed_window_ids)
            )
            normalized.append(
                {
                    "username_norm": username_norm,
                    "username_display": username_display,
                    "source_target": source_target,
                    "profile_json": _json(profile),
                    "queued": bool(raw.get("queued")),
                    "allowed_window_ids": allowed_window_ids,
                }
            )
        now = isoformat()
        accepted_ids: list[str] = []
        duplicates: list[dict[str, Any]] = []
        with self.database.write() as connection:
            if connection.execute(
                "SELECT 1 FROM app_users WHERE id=?", (owner_user_id,)
            ).fetchone() is None:
                raise AuthenticationError("Application user no longer exists")
            live_rows: list[sqlite3.Row] = []
            history_rows: list[sqlite3.Row] = []
            failure_rows: list[sqlite3.Row] = []
            incoming_usernames = [item["username_norm"] for item in normalized]
            # Duplicate detection is scoped to the submitted usernames.  Lifetime
            # completion history must not be loaded whenever one new target is
            # pasted into the workbench.
            for offset in range(0, len(incoming_usernames), 400):
                username_batch = incoming_usernames[offset : offset + 400]
                placeholders = ",".join("?" for _ in username_batch)
                parameters = (owner_user_id, *username_batch)
                live_rows.extend(
                    connection.execute(
                        f"""
                        SELECT id, username_norm, queue_state,
                               manual_category_override
                        FROM split_candidates
                        WHERE owner_user_id=? AND candidate_kind='manual'
                          AND username_norm IN ({placeholders})
                        """,
                        parameters,
                    ).fetchall()
                )
                history_rows.extend(
                    connection.execute(
                        f"""
                        WITH ranked_history AS (
                            SELECT id, username_norm, manual_category_override,
                                   COUNT(*) OVER (
                                       PARTITION BY username_norm
                                   ) AS generation_count,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY username_norm
                                       ORDER BY completed_at DESC, id ASC
                                   ) AS history_rank
                            FROM split_candidate_history
                            WHERE owner_user_id=?
                              AND username_norm IN ({placeholders})
                        )
                        SELECT id, username_norm, manual_category_override,
                               generation_count
                        FROM ranked_history
                        WHERE history_rank=1
                        """,
                        parameters,
                    ).fetchall()
                )
                failure_rows.extend(
                    connection.execute(
                        f"""
                        WITH ranked_failures AS (
                            SELECT candidate_id AS id, username_norm,
                                   COUNT(*) OVER (
                                       PARTITION BY username_norm
                                   ) AS generation_count,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY username_norm
                                       ORDER BY updated_at DESC, target_id ASC
                                   ) AS failure_rank
                            FROM task_target_recovery_controls
                            WHERE owner_user_id=? AND state='pending'
                              AND username_norm IN ({placeholders})
                        )
                        SELECT id, username_norm, generation_count
                        FROM ranked_failures
                        WHERE failure_rank=1
                        """,
                        parameters,
                    ).fetchall()
                )
            live_by_username = {row["username_norm"]: row for row in live_rows}
            history_by_username: dict[str, list[sqlite3.Row]] = {}
            for row in history_rows:
                history_by_username.setdefault(row["username_norm"], []).append(row)
            failures_by_username: dict[str, list[sqlite3.Row]] = {}
            for row in failure_rows:
                failures_by_username.setdefault(row["username_norm"], []).append(row)

            for item in normalized:
                username_norm = item["username_norm"]
                reserve_split_identity(
                    connection, username_norm, item["username_display"], now,
                    owner_user_id=owner_user_id,
                )
                live = live_by_username.get(username_norm)
                histories = history_by_username.get(username_norm, [])
                failures = failures_by_username.get(username_norm, [])
                if was_split_executed(connection, owner_user_id, username_norm) and not allow_completed:
                    disposition = "failure" if failures else "running" if live is not None and live["queue_state"] == "claimed" else "completed" if histories else "history"
                    duplicates.append({
                        "username": item["username_display"],
                        "candidate_ids": list(dict.fromkeys([row["id"] for row in failures] + ([live["id"]] if live is not None else []) + [row["id"] for row in histories])),
                        "kind": "failure" if failures else "manual", "disposition": disposition,
                        "lifecycle_state": disposition, "real_lifecycle_state": disposition,
                        "generation_count": (1 if live is not None else 0) + sum(int(row["generation_count"]) for row in histories) + sum(int(row["generation_count"]) for row in failures),
                        "reason": "split_already_executed",
                        "message": "该账号已有分裂记录，请确认是否再次加入",
                    })
                    continue
                direct = connection.execute(
                    "SELECT t.id FROM task_targets t JOIN tasks task ON task.id=t.task_id "
                    "WHERE task.owner_user_id=? AND t.username_norm=? AND t.status!='completed' "
                    "AND COALESCE(t.current_stage,'')!='deleted_archived' LIMIT 1",
                    (owner_user_id, username_norm),
                ).fetchone()
                if direct is not None and live is None:
                    duplicates.append({"username": item["username_display"], "candidate_ids": [],
                        "kind": "manual", "disposition": "waiting", "lifecycle_state": "waiting",
                        "real_lifecycle_state": "waiting", "generation_count": 1,
                        "reason": "split_already_queued", "message": "该账号已在任务列表，不能重复加入"})
                    continue
                # The public-review "split account" action is an immediate queue
                # request. If the username was saved earlier as an available manual
                # candidate, promote that exact durable generation atomically rather
                # than reporting a duplicate that remains outside the live queue.
                if (
                    live is not None
                    and item["queued"]
                    and live["queue_state"] == "available"
                    and live["manual_category_override"] != "completed"
                    and not failures
                ):
                    connection.execute(
                        """
                        UPDATE split_candidates
                        SET queue_state='queued', queued_task_id=NULL,
                            queued_target_id=NULL,
                            queued_at=COALESCE(queued_at, ?), updated_at=?
                        WHERE id=? AND owner_user_id=?
                          AND queue_state='available'
                          AND COALESCE(manual_category_override, '')!='completed'
                        """,
                        (now, now, live["id"], owner_user_id),
                    )
                    if item["allowed_window_ids"] is not None:
                        _replace_split_candidate_window_affinity(
                            connection, live["id"], item["allowed_window_ids"]
                        )
                    accepted_ids.append(live["id"])
                    _event(
                        connection,
                        owner_user_id,
                        "split_candidate",
                        live["id"],
                        "split_candidate.queued",
                        {"source": "queued_upsert"},
                    )
                    continue
                # A manually ignored/completed live row is not an active task.
                # When the operator explicitly chooses to collect it again,
                # revive that durable row instead of reporting a duplicate and
                # leaving the waiting list empty.
                if (
                    live is not None
                    and not failures
                    and allow_completed
                    and live["manual_category_override"] == "completed"
                    and live["queue_state"] != "claimed"
                ):
                    connection.execute(
                        """
                        UPDATE split_candidates
                        SET username_display=?, queue_state='queued',
                            queued_task_id=NULL, queued_target_id=NULL,
                            queued_at=?, claimed_at=NULL,
                            manual_category_override=NULL,
                            manual_category_at=NULL, updated_at=?
                        WHERE id=? AND owner_user_id=?
                        """,
                        (
                            item["username_display"],
                            now,
                            now,
                            live["id"],
                            owner_user_id,
                        ),
                    )
                    if item["allowed_window_ids"] is not None:
                        _replace_split_candidate_window_affinity(
                            connection, live["id"], item["allowed_window_ids"]
                        )
                    accepted_ids.append(live["id"])
                    _event(
                        connection,
                        owner_user_id,
                        "split_candidate",
                        username_norm,
                        "split_candidate.requeued",
                        {"queue_state": "queued", "reason": "operator_override"},
                    )
                    record_split_admission(connection, owner_user_id, username_norm, now)
                    continue
                if live is not None or failures or (histories and not allow_completed):
                    candidate_ids = [row["id"] for row in failures]
                    if live is not None:
                        candidate_ids.append(live["id"])
                    candidate_ids.extend(row["id"] for row in histories)
                    if failures:
                        disposition = "failure"
                        kind = "failure"
                        lifecycle_state = None
                        real_lifecycle_state = None
                    elif live is not None:
                        real_lifecycle_state = (
                            "running" if live["queue_state"] == "claimed" else "waiting"
                        )
                        if (
                            real_lifecycle_state == "waiting"
                            and live["manual_category_override"] == "completed"
                        ):
                            disposition = "ignored"
                        else:
                            disposition = real_lifecycle_state
                        kind = "manual"
                        lifecycle_state = (
                            live["manual_category_override"] or real_lifecycle_state
                        )
                    else:
                        disposition = "completed"
                        kind = "manual"
                        real_lifecycle_state = "completed"
                        lifecycle_state = "completed"
                    duplicates.append(
                        {
                            "username": item["username_display"],
                            "candidate_ids": list(dict.fromkeys(candidate_ids)),
                            "kind": kind,
                            "disposition": disposition,
                            "lifecycle_state": lifecycle_state,
                            "real_lifecycle_state": real_lifecycle_state,
                            "generation_count": (
                                (1 if live is not None else 0)
                                + sum(
                                    int(row["generation_count"])
                                    for row in histories
                                )
                                + sum(
                                    int(row["generation_count"])
                                    for row in failures
                                )
                            ),
                        }
                    )
                    continue
                record_split_admission(connection, owner_user_id, username_norm, now)
                candidate_id = str(uuid.uuid4())
                incoming_state = "queued" if item["queued"] else "available"
                connection.execute(
                    """
                    INSERT INTO split_candidates(
                        id, owner_user_id, username_norm, username_display,
                        candidate_kind, source_target, profile_json, queue_state,
                        queued_at, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        owner_user_id,
                        item["username_norm"],
                        item["username_display"],
                        item["source_target"],
                        item["profile_json"],
                        incoming_state,
                        now if incoming_state == "queued" else None,
                        now,
                        now,
                    ),
                )
                _replace_split_candidate_window_affinity(
                    connection, candidate_id, item["allowed_window_ids"] or []
                )
                accepted_ids.append(candidate_id)
                _event(
                    connection,
                    owner_user_id,
                    "split_candidate",
                    item["username_norm"],
                    "split_candidate.saved",
                    {"queue_state": incoming_state},
                )
        if not include_outcome:
            return self.list_split_candidates(owner_user_id)
        affected_ids = list(
            dict.fromkeys(
                accepted_ids
                + [
                    candidate_id
                    for duplicate in duplicates
                    for candidate_id in duplicate["candidate_ids"]
                ]
            )
        )
        rows = self._list_split_candidates_by_ids(owner_user_id, affected_ids)
        return {
            "candidates": rows,
            "total": len(rows),
            "affected_count": len(rows),
            "accepted_ids": accepted_ids,
            "accepted_count": len(accepted_ids),
            "duplicates": duplicates,
            "duplicate_count": len(duplicates),
        }

    def mark_split_candidates_queued(
        self,
        owner_user_id: str,
        candidate_ids: list[str],
        *,
        include_outcome: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Queue exact live generations and describe non-queueable duplicates."""
        normalized_ids = list(
            dict.fromkeys(str(item).strip() for item in candidate_ids if str(item).strip())
        )
        if not normalized_ids:
            raise ValidationError("At least one split candidate is required")
        placeholders = ",".join("?" for _ in normalized_ids)
        now = isoformat()
        accepted_ids: list[str] = []
        duplicates: list[dict[str, Any]] = []
        with self.database.write() as connection:
            live_rows = connection.execute(
                f"""
                SELECT id, username_display, queue_state,
                       manual_category_override
                FROM split_candidates
                WHERE owner_user_id=? AND id IN ({placeholders})
                  AND candidate_kind='manual'
                """,
                (owner_user_id, *normalized_ids),
            ).fetchall()
            history_rows = connection.execute(
                f"""
                SELECT id, username_display
                FROM split_candidate_history
                WHERE owner_user_id=? AND id IN ({placeholders})
                """,
                (owner_user_id, *normalized_ids),
            ).fetchall()
            failure_rows = connection.execute(
                f"""
                SELECT candidate_id AS id, username_display
                FROM task_target_recovery_controls
                WHERE owner_user_id=? AND state='pending'
                  AND candidate_id IN ({placeholders})
                """,
                (owner_user_id, *normalized_ids),
            ).fetchall()
            live_by_id = {row["id"]: row for row in live_rows}
            history_by_id = {row["id"]: row for row in history_rows}
            failure_by_id = {row["id"]: row for row in failure_rows}
            found_ids = set(live_by_id) | set(history_by_id) | set(failure_by_id)
            if len(found_ids) != len(normalized_ids):
                raise NotFoundError("Split candidate generation not found")

            for candidate_id in normalized_ids:
                live = live_by_id.get(candidate_id)
                if live is None:
                    failure = failure_by_id.get(candidate_id)
                    if failure is not None:
                        duplicates.append(
                            {
                                "username": failure["username_display"],
                                "candidate_ids": [candidate_id],
                                "kind": "failure",
                                "disposition": "failure",
                                "lifecycle_state": None,
                                "real_lifecycle_state": None,
                            }
                        )
                    else:
                        history = history_by_id[candidate_id]
                        duplicates.append(
                            {
                                "username": history["username_display"],
                                "candidate_ids": [candidate_id],
                                "kind": "manual",
                                "disposition": "completed",
                                "lifecycle_state": "completed",
                                "real_lifecycle_state": "completed",
                            }
                        )
                    continue
                real_state = "running" if live["queue_state"] == "claimed" else "waiting"
                if live["manual_category_override"] == "completed":
                    disposition = "ignored"
                elif live["queue_state"] == "available":
                    connection.execute(
                        """
                        UPDATE split_candidates
                        SET queue_state='queued', queued_task_id=NULL,
                            queued_target_id=NULL,
                            queued_at=COALESCE(queued_at, ?), updated_at=?
                        WHERE id=? AND owner_user_id=?
                          AND queue_state='available'
                          AND COALESCE(manual_category_override, '')!='completed'
                        """,
                        (now, now, candidate_id, owner_user_id),
                    )
                    accepted_ids.append(candidate_id)
                    _event(
                        connection,
                        owner_user_id,
                        "split_candidate",
                        candidate_id,
                        "split_candidate.queued",
                        {},
                    )
                    continue
                else:
                    disposition = real_state
                duplicates.append(
                    {
                        "username": live["username_display"],
                        "candidate_ids": [candidate_id],
                        "kind": "manual",
                        "disposition": disposition,
                        "lifecycle_state": (
                            live["manual_category_override"] or real_state
                        ),
                        "real_lifecycle_state": real_state,
                    }
                )
        if not include_outcome:
            return self.list_split_candidates(owner_user_id)
        rows = self._list_split_candidates_by_ids(owner_user_id, normalized_ids)
        return {
            "candidates": rows,
            "total": len(rows),
            "affected_count": len(rows),
            "accepted_ids": accepted_ids,
            "accepted_count": len(accepted_ids),
            "duplicates": duplicates,
            "duplicate_count": len(duplicates),
        }

    def set_split_candidate_allowed_windows(
        self,
        owner_user_id: str,
        candidate_id: str,
        allowed_window_ids: Iterable[Any],
    ) -> dict[str, Any]:
        """Atomically set or clear one unclaimed split target's window pool.

        An empty pool restores automatic dispatch.  The same database write lock
        serializes this mutation with ``claim_next_split_candidate``: either the
        affinity wins before claim, or a stale edit receives a conflict after the
        target has started.  It can never be claimed using a half-written pool.
        """

        candidate_id = str(candidate_id).strip()
        if not candidate_id:
            raise ValidationError("Invalid split candidate id")
        normalized_windows = _normalize_allowed_window_ids(allowed_window_ids)
        now = isoformat()
        with self.database.write() as connection:
            candidate = connection.execute(
                """
                SELECT id, queue_state, queued_target_id
                FROM split_candidates
                WHERE id=? AND owner_user_id=? AND candidate_kind='manual'
                """,
                (candidate_id, owner_user_id),
            ).fetchone()
            if candidate is None:
                raise NotFoundError("Split candidate not found")
            if (
                candidate["queue_state"] not in {"available", "queued"}
                or candidate["queued_target_id"] is not None
            ):
                raise ConflictError(
                    "The split target has already started and its windows cannot be changed"
                )
            _replace_split_candidate_window_affinity(
                connection, candidate_id, normalized_windows
            )
            connection.execute(
                "UPDATE split_candidates SET updated_at=? WHERE id=?",
                (now, candidate_id),
            )
            _event(
                connection,
                owner_user_id,
                "split_candidate",
                candidate_id,
                "split_candidate.window_affinity_set",
                {
                    "assignment_mode": (
                        "specified" if normalized_windows else "automatic"
                    ),
                    "window_count": len(normalized_windows),
                },
            )
        rows = self._list_split_candidates_by_ids(owner_user_id, [candidate_id])
        if not rows:
            raise NotFoundError("Split candidate not found")
        return rows[0]

    def set_split_candidate_manual_category(
        self,
        owner_user_id: str,
        candidate_ids: list[str],
        category: str | None,
        *,
        affected_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Move stable split generations between display buckets only.

        Queue ownership, task/target status and dispatch pointers are never changed
        here.  ``completed`` on a real WAITING row is a durable ignore disposition,
        so claim queries skip it until the user moves it back to WAITING; a real
        RUNNING row cannot be ignored.  Other moves are display-only.  A real worker
        transition clears the override so RUNNING/COMPLETED/FAILURE again becomes
        authoritative.  The entire batch is owner- and generation-checked before
        any row changes, preventing a stale id from partially moving newer
        same-username generations.
        """

        normalized_ids = list(
            dict.fromkeys(str(item).strip() for item in candidate_ids if str(item).strip())
        )
        if not normalized_ids:
            raise ValidationError("At least one split candidate is required")
        if category not in {None, "waiting", "running", "completed"}:
            raise ValidationError("Invalid split candidate category")
        placeholders = ",".join("?" for _ in normalized_ids)
        now = isoformat()
        with self.database.write() as connection:
            live_rows = connection.execute(
                f"""
                SELECT id, queue_state, manual_category_override
                FROM split_candidates
                WHERE owner_user_id=? AND candidate_kind='manual'
                  AND id IN ({placeholders})
                """,
                (owner_user_id, *normalized_ids),
            ).fetchall()
            history_rows = connection.execute(
                f"""
                SELECT id, manual_category_override
                FROM split_candidate_history
                WHERE owner_user_id=? AND id IN ({placeholders})
                """,
                (owner_user_id, *normalized_ids),
            ).fetchall()
            found = {row["id"] for row in live_rows} | {
                row["id"] for row in history_rows
            }
            missing = [candidate_id for candidate_id in normalized_ids if candidate_id not in found]
            if missing:
                missing_placeholders = ",".join("?" for _ in missing)
                pending_failures = connection.execute(
                    f"""
                    SELECT candidate_id
                    FROM task_target_recovery_controls
                    WHERE owner_user_id=? AND state='pending'
                      AND candidate_id IN ({missing_placeholders})
                    """,
                    (owner_user_id, *missing),
                ).fetchall()
                if pending_failures:
                    raise ConflictError(
                        "Failure candidates must be resolved, not manually categorized"
                    )
                raise NotFoundError("Split candidate generation not found")

            if category == "completed" and any(
                row["queue_state"] == "claimed" for row in live_rows
            ):
                raise ConflictError(
                    "A running split candidate cannot be ignored; stop or finish it first"
                )

            live_by_id = {row["id"]: row for row in live_rows}
            history_by_id = {row["id"]: row for row in history_rows}
            for candidate_id in normalized_ids:
                row = live_by_id.get(candidate_id) or history_by_id[candidate_id]
                changed = row["manual_category_override"] != category
                if changed:
                    table = (
                        "split_candidates"
                        if candidate_id in live_by_id
                        else "split_candidate_history"
                    )
                    connection.execute(
                        f"""
                        UPDATE {table}
                        SET manual_category_override=?, manual_category_at=?,
                            updated_at=?
                        WHERE id=? AND owner_user_id=?
                        """,
                        (
                            category,
                            now if category is not None else None,
                            now,
                            candidate_id,
                            owner_user_id,
                        ),
                    )
                _event(
                    connection,
                    owner_user_id,
                    "split_candidate",
                    candidate_id,
                    "split_candidate.category_set",
                    {"category": category, "changed": changed},
                )
        if affected_only:
            return self._list_split_candidates_by_ids(owner_user_id, normalized_ids)
        return self.list_split_candidates(owner_user_id)

    def requeue_split_candidate(
        self,
        owner_user_id: str,
        candidate_id: str,
        *,
        allowed_window_ids: Iterable[Any] | None = None,
        _removal_token: object | None = None,
    ) -> dict[str, Any]:
        """Atomically resolve one failure generation into the delayed manual queue.

        Failure action ids come from the per-target recovery control, not from the
        username-deduplicated manual queue.  A delayed/replayed request therefore
        cannot accidentally requeue a newer failure for the same Instagram account.
        No task target is created here; a ready worker performs the separate claim.
        """

        candidate_id = str(candidate_id).strip()
        if not candidate_id:
            raise ValidationError("Invalid split candidate id")
        normalized_windows = (
            None
            if allowed_window_ids is None
            else _normalize_allowed_window_ids(allowed_window_ids)
        )
        now = isoformat()
        with self.database.write() as connection:
            recovery = connection.execute(
                """
                SELECT * FROM task_target_recovery_controls
                WHERE candidate_id=? AND owner_user_id=?
                """,
                (candidate_id, owner_user_id),
            ).fetchone()
            if recovery is None:
                raise NotFoundError("Split candidate not found")
            self._guard_target_removal(owner_user_id, recovery["target_id"], _removal_token)
            if recovery["state"] == "dismissed":
                raise NotFoundError("Split candidate not found")
            existing = connection.execute(
                """
                SELECT candidate.*,
                       COALESCE(
                         (
                           SELECT json_group_array(ordered.profile_id)
                           FROM (
                             SELECT affinity.profile_id
                             FROM split_candidate_window_affinity affinity
                             WHERE affinity.candidate_id=candidate.id
                             ORDER BY affinity.queue_order
                           ) ordered
                         ),
                         '[]'
                       ) AS allowed_window_ids_json,
                       EXISTS(
                         SELECT 1 FROM task_targets source
                         WHERE source.id=candidate.source_target_id
                       ) AS source_target_exists
                FROM split_candidates candidate
                WHERE candidate.owner_user_id=? AND candidate.username_norm=?
                """,
                (owner_user_id, recovery["username_norm"]),
            ).fetchone()
            if recovery["state"] == "requeued":
                if existing is None:
                    raise ConflictError("Recovered split candidate queue entry is missing")
                # A failure action identifies one source target, not every later
                # queue row with the same username. Completion can archive that
                # row and an explicit recollection can create a new generation.
                if (
                    existing["candidate_kind"] != "manual"
                    or existing["source_target_id"] != recovery["target_id"]
                    or existing["source_task_id"] != recovery["source_task_id"]
                ):
                    raise ConflictError("恢复请求已过期，不能修改新的同名采集目标")
                current = self._split_candidate_dict(existing)
                # Replayed recovery requests are read-only. An operator may have
                # edited the waiting row since the first response was lost; its
                # new window pool must not be overwritten by the old payload.
                # Later intentional edits use set_split_candidate_allowed_windows.
                if normalized_windows is not None and normalized_windows != current["allowed_window_ids"]:
                    raise ConflictError("指定窗口已更新，请在等待列表中重新选择窗口")
                return current
            same_source_recovery = bool(
                existing is not None
                and existing["candidate_kind"] == "manual"
                and existing["source_target_id"] == recovery["target_id"]
            )
            if (
                same_source_recovery
                and existing["queued_target_id"]
                and existing["queued_target_id"] != recovery["target_id"]
                and connection.execute(
                    """
                    SELECT 1 FROM task_targets
                    WHERE id=? AND status IN (
                        'pending', 'running', 'waiting_network', 'completed'
                    )
                    """,
                    (existing["queued_target_id"],),
                ).fetchone()
                is not None
            ):
                raise ConflictError(
                    "The same Instagram account is owned by another active target"
                )
            if existing is not None and not same_source_recovery:
                stale_marker_can_be_rebound = False
                if (
                    existing["candidate_kind"] == "manual"
                    and existing["queue_state"] in {"queued", "claimed"}
                    and existing["queued_target_id"]
                ):
                    active_marker = connection.execute(
                        """
                        SELECT 1 FROM task_targets
                        WHERE id=? AND status IN (
                            'pending', 'running', 'waiting_network'
                        )
                        """,
                        (existing["queued_target_id"],),
                    ).fetchone()
                    unresolved_marker = connection.execute(
                        """
                        SELECT 1 FROM task_target_recovery_controls
                        WHERE target_id=? AND owner_user_id=? AND state='pending'
                        """,
                        (existing["source_target_id"], owner_user_id),
                    ).fetchone()
                    stale_marker_can_be_rebound = (
                        active_marker is None and unresolved_marker is None
                    )
                if (
                    existing["candidate_kind"] != "manual"
                    or (
                        existing["queue_state"] in {"queued", "claimed"}
                        and not stale_marker_can_be_rebound
                    )
                ):
                    raise ConflictError(
                        "The same Instagram account is already waiting or running"
                    )

            manual_id = existing["id"] if existing is not None else str(uuid.uuid4())
            if existing is None:
                updated_manual = connection.execute(
                    """
                    INSERT INTO split_candidates(
                        id, owner_user_id, username_norm, username_display,
                        candidate_kind, source_target, source_task_id,
                        source_target_id, source_status, source_window_id,
                        last_error, profile_json, queue_state, queued_task_id,
                        queued_target_id, queued_at, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, 'manual', ?, ?, ?, 'requeued_by_user',
                             ?, ?, '{}', 'queued', NULL, NULL, ?, ?, ?)
                    """,
                    (
                        manual_id,
                        owner_user_id,
                        recovery["username_norm"],
                        recovery["username_display"],
                        recovery["username_display"],
                        recovery["source_task_id"],
                        recovery["target_id"],
                        recovery["source_window_id"],
                        recovery["last_error"],
                        now,
                        now,
                        now,
                    ),
                )
            else:
                updated_manual = connection.execute(
                    """
                    UPDATE split_candidates
                    SET username_display=?, candidate_kind='manual', source_target=?,
                        source_task_id=?, source_target_id=?,
                        source_status='requeued_by_user', source_window_id=?,
                        last_error=?, queue_state='queued', queued_task_id=NULL,
                        queued_target_id=NULL, queued_at=?,
                        manual_category_override=NULL, manual_category_at=NULL,
                        updated_at=?
                    WHERE id=? AND owner_user_id=?
                      AND (
                        queue_state='available'
                        OR (candidate_kind='manual' AND source_target_id=?)
                        OR (
                          candidate_kind='manual'
                          AND queued_target_id IS NOT NULL
                          AND NOT EXISTS(
                            SELECT 1 FROM task_targets active
                            WHERE active.id=split_candidates.queued_target_id
                              AND active.status IN (
                                'pending', 'running', 'waiting_network'
                              )
                          )
                          AND NOT EXISTS(
                            SELECT 1 FROM task_target_recovery_controls unresolved
                            WHERE unresolved.target_id=split_candidates.source_target_id
                              AND unresolved.owner_user_id=split_candidates.owner_user_id
                              AND unresolved.state='pending'
                          )
                        )
                      )
                    """,
                    (
                        recovery["username_display"],
                        recovery["username_display"],
                        recovery["source_task_id"],
                        recovery["target_id"],
                        recovery["source_window_id"],
                        recovery["last_error"],
                        now,
                        now,
                        manual_id,
                        owner_user_id,
                        recovery["target_id"],
                    ),
                )
            if updated_manual.rowcount != 1:
                raise ConflictError("Manual split queue changed concurrently")
            if normalized_windows is not None:
                _replace_split_candidate_window_affinity(
                    connection, manual_id, normalized_windows
                )
            updated = connection.execute(
                """
                UPDATE task_target_recovery_controls
                SET state='requeued', updated_at=?
                WHERE target_id=? AND owner_user_id=? AND candidate_id=?
                  AND state='pending'
                """,
                (now, recovery["target_id"], owner_user_id, candidate_id),
            )
            if updated.rowcount != 1:
                raise ConflictError("Failure recovery state changed concurrently")
            _event(
                connection,
                owner_user_id,
                "split_candidate",
                candidate_id,
                "split_candidate.requeued",
                {
                    "delayed_dispatch": True,
                    "source_target_id": recovery["target_id"],
                    "manual_candidate_id": manual_id,
                },
            )
            row = connection.execute(
                """
                SELECT candidate.*,
                       COALESCE(
                         (
                           SELECT json_group_array(ordered.profile_id)
                           FROM (
                             SELECT affinity.profile_id
                             FROM split_candidate_window_affinity affinity
                             WHERE affinity.candidate_id=candidate.id
                             ORDER BY affinity.queue_order
                           ) ordered
                         ),
                         '[]'
                       ) AS allowed_window_ids_json,
                       EXISTS(
                         SELECT 1 FROM task_targets source
                         WHERE source.id=candidate.source_target_id
                       ) AS source_target_exists
                FROM split_candidates candidate WHERE candidate.id=?
                """,
                (manual_id,),
            ).fetchone()
        return self._split_candidate_dict(row)

    def delete_failed_split_candidate(
        self, owner_user_id: str, candidate_id: str
    ) -> dict[str, Any]:
        """Dismiss one failure generation while retaining its source history.

        The disposition is a durable tombstone keyed by source target.  Missing or
        already-dismissed generation ids are idempotent successes, while a recovery
        already moved into the execution queue cannot be silently deleted.
        """

        candidate_id = str(candidate_id).strip()
        if not candidate_id:
            raise ValidationError("Invalid split candidate id")
        now = isoformat()
        with self.database.write() as connection:
            row = connection.execute(
                """
                SELECT * FROM task_target_recovery_controls
                WHERE candidate_id=? AND owner_user_id=?
                """,
                (candidate_id, owner_user_id),
            ).fetchone()
            if row is None:
                return {"candidate_id": candidate_id, "deleted": True, "already_absent": True}
            if row["state"] == "dismissed":
                return {"candidate_id": candidate_id, "deleted": True, "already_absent": True}
            if row["state"] != "pending":
                raise ConflictError("A requeued split candidate cannot be deleted")
            cursor = connection.execute(
                """
                UPDATE task_target_recovery_controls
                SET state='dismissed', updated_at=?
                WHERE target_id=? AND owner_user_id=? AND candidate_id=?
                  AND state='pending'
                """,
                (now, row["target_id"], owner_user_id, candidate_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Split candidate changed while it was being deleted")
            connection.execute(
                """
                DELETE FROM split_candidates
                WHERE owner_user_id=? AND candidate_kind='manual'
                  AND source_target_id=?
                """,
                (owner_user_id, row["target_id"]),
            )
            _event(
                connection,
                owner_user_id,
                "split_candidate",
                candidate_id,
                "split_candidate.deleted",
                {"history_preserved": True, "source_target_id": row["target_id"]},
            )
        return {"candidate_id": candidate_id, "deleted": True, "already_absent": False}

    def delete_waiting_split_candidate(
        self, owner_user_id: str, candidate_id: str
    ) -> dict[str, Any]:
        """Delete exactly one unclaimed split target from the waiting queue."""
        candidate_id = str(candidate_id).strip()
        if not candidate_id:
            raise ValidationError("Invalid split candidate id")
        now = isoformat()
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT * FROM split_candidates WHERE id=? AND owner_user_id=? AND candidate_kind='manual'",
                (candidate_id, owner_user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("Waiting split candidate not found")
            if row["queue_state"] != "queued" or row["queued_target_id"] is not None:
                raise ConflictError("Target has already been claimed and cannot be deleted from waiting queue")
            cursor = connection.execute(
                "DELETE FROM split_candidates WHERE id=? AND owner_user_id=? AND queue_state='queued' AND queued_target_id IS NULL",
                (candidate_id, owner_user_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Target was claimed while it was being deleted")
            if row["source_target_id"]:
                connection.execute(
                    "UPDATE task_target_recovery_controls SET state='dismissed', updated_at=? WHERE owner_user_id=? AND target_id=? AND state='requeued'",
                    (now, owner_user_id, row["source_target_id"]),
                )
            _event(connection, owner_user_id, "split_candidate", candidate_id, "split_candidate.waiting_deleted", {"username": row["username_display"]})
        return {"candidate_id": candidate_id, "deleted": True}

    def claim_next_split_candidate(
        self,
        owner_user_id: str,
        task_id: str,
        profile_id: str,
    ) -> dict[str, Any] | None:
        """Atomically bind one durable split wait row to one ready window.

        This transaction is the sole delayed-dispatch linearization point.  Before
        it commits the username exists only in split_candidates; afterwards exactly
        one RUNNING task_target owns it.  SQLite's process write lock plus the
        queue_state compare-and-set prevents duplicate claims across many workers.
        """

        now = isoformat()
        with self.database.write() as connection:
            task = self._owned_task(connection, owner_user_id, task_id)
            settings = _loads(task["settings_json"])
            if not settings.get("live_queue_enabled") or task["status"] != "running":
                return None
            if connection.execute(
                "SELECT 1 FROM task_windows WHERE task_id=? AND profile_id=?",
                (task_id, profile_id),
            ).fetchone() is None:
                raise ConflictError("BitBrowser window is not assigned to this task")

            # Skip usernames already completed/owned inside this task; another live
            # task for the same owner may still claim them.  Recoverable/pending rows
            # in this task are resumed in place so their checkpoints survive.
            candidate = connection.execute(
                f"""
                SELECT candidate.*,
                       COALESCE(
                         (
                           SELECT json_group_array(ordered.profile_id)
                           FROM (
                             SELECT affinity.profile_id
                             FROM split_candidate_window_affinity affinity
                             WHERE affinity.candidate_id=candidate.id
                             ORDER BY affinity.queue_order
                           ) ordered
                         ),
                         '[]'
                       ) AS allowed_window_ids_json
                FROM split_candidates candidate
                WHERE candidate.owner_user_id=?
                  {username_platform_sql(collection_platform(settings), "candidate.username_norm")}
                  AND candidate.candidate_kind='manual'
                  AND candidate.queue_state='queued'
                  AND candidate.queued_target_id IS NULL
                  AND candidate.dispatch_locked=0
                  AND NOT EXISTS(SELECT 1 FROM collection_dispatch_locks gate WHERE gate.owner_user_id=candidate.owner_user_id AND gate.locked=1)
                  AND COALESCE(candidate.manual_category_override, '')!='completed'
                  AND COALESCE(candidate.source_status, '')!='dismissed_by_user'
                  AND (
                    candidate.source_target_id IS NULL
                    OR candidate.source_task_id=?
                    OR NOT EXISTS (
                      SELECT 1
                      FROM task_targets source
                      JOIN task_windows source_window
                        ON source_window.task_id=source.task_id
                      WHERE source.id=candidate.source_target_id
                        AND NOT EXISTS (
                          SELECT 1 FROM task_list_dismissals dismissal
                          WHERE dismissal.task_id=source.task_id
                            AND dismissal.owner_user_id=candidate.owner_user_id
                        )
                    )
                  )
                  AND (
                    NOT EXISTS(
                      SELECT 1
                      FROM split_candidate_window_affinity affinity
                      WHERE affinity.candidate_id=candidate.id
                    )
                    OR EXISTS(
                      SELECT 1
                      FROM split_candidate_window_affinity affinity
                      WHERE affinity.candidate_id=candidate.id
                        AND affinity.profile_id=?
                    )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM task_targets existing
                    WHERE existing.task_id=?
                      AND existing.username_norm=candidate.username_norm
                      AND (existing.status IN ('running', 'waiting_network', 'completed')
                           OR existing.current_stage='deleted_archived')
                  )
                ORDER BY
                         CASE WHEN EXISTS(
                           SELECT 1
                           FROM split_candidate_window_affinity affinity
                           WHERE affinity.candidate_id=candidate.id
                             AND affinity.profile_id=?
                         ) THEN 0 ELSE 1 END,
                         COALESCE(candidate.queued_at, candidate.created_at),
                         candidate.created_at, candidate.id
                LIMIT 1
                """,
                (owner_user_id, task_id, profile_id, task_id, profile_id),
            ).fetchone()
            if candidate is None:
                return None

            existing = connection.execute(
                """
                SELECT * FROM task_targets
                WHERE task_id=? AND username_norm=?
                LIMIT 1
                """,
                (task_id, candidate["username_norm"]),
            ).fetchone()
            target_id = existing["id"] if existing is not None else str(uuid.uuid4())
            if (owner_user_id, target_id) in self._target_removal_tokens:
                # A returned row can be visible before its old browser finishes
                # closing. Stay idle; the remover wakes the queue after teardown.
                return None
            claimed = connection.execute(
                """
                UPDATE split_candidates
                SET queue_state='claimed', queued_task_id=?, queued_target_id=?,
                    source_task_id=?, source_target_id=?,
                    source_status='claimed_generation', source_window_id=?,
                    claimed_at=COALESCE(claimed_at, ?),
                    manual_category_override=NULL, manual_category_at=NULL,
                    updated_at=?
                WHERE id=? AND owner_user_id=? AND queue_state='queued'
                  AND queued_target_id IS NULL
                """,
                (
                    task_id,
                    target_id,
                    task_id,
                    target_id,
                    profile_id,
                    now,
                    now,
                    candidate["id"],
                    owner_user_id,
                ),
            )
            if claimed.rowcount != 1:
                return None

            reserve_split_identity(
                connection, candidate["username_norm"], candidate["username_display"], now,
                source="task_source", owner_user_id=owner_user_id,
            )
            if existing is None:
                queue_order = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(queue_order), 0) + 1 "
                        "FROM task_targets WHERE task_id=?",
                        (task_id,),
                    ).fetchone()[0]
                )
                updated = connection.execute(
                    """
                    INSERT INTO task_targets(
                        id, task_id, username_norm, username_display, queue_order,
                        status, preferred_window_id, current_window_id,
                        allowed_window_ids_json, current_stage, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, 'running', ?, ?, ?, 'opening_profile', ?, ?)
                    """,
                    (
                        target_id,
                        task_id,
                        candidate["username_norm"],
                        candidate["username_display"],
                        queue_order,
                        profile_id,
                        profile_id,
                        candidate["allowed_window_ids_json"],
                        now,
                        now,
                    ),
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE task_targets
                    SET status='running', preferred_window_id=COALESCE(preferred_window_id, ?),
                        current_window_id=?, allowed_window_ids_json=?,
                        current_stage='opening_profile',
                        last_error=NULL, updated_at=?
                    WHERE id=? AND task_id=?
                      AND status IN ('pending', 'recoverable', 'failed', 'stopped')
                    """,
                    (
                        profile_id,
                        profile_id,
                        candidate["allowed_window_ids_json"],
                        now,
                        target_id,
                        task_id,
                    ),
                )
            if updated.rowcount != 1:
                raise ConflictError("Split target could not be claimed atomically")
            if existing is not None:
                connection.execute(
                    """
                    UPDATE task_target_recovery_controls
                    SET state='requeued', updated_at=?
                    WHERE target_id=? AND owner_user_id=?
                    """,
                    (now, target_id, owner_user_id),
                )
            connection.execute(
                "UPDATE tasks SET status='running', updated_at=?, version=version+1 WHERE id=?",
                (now, task_id),
            )
            _event(
                connection,
                owner_user_id,
                "split_candidate",
                candidate["id"],
                "split_candidate.claimed",
                {"task_id": task_id, "target_id": target_id, "profile_id": profile_id},
            )
            row = connection.execute(
                "SELECT * FROM task_targets WHERE id=?", (target_id,)
            ).fetchone()
        return _target_dict(row)

    @staticmethod
    def _split_candidate_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            profile = _loads(row["profile_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            profile = {}
        if not isinstance(profile, dict):
            profile = {}
        try:
            allowed_window_ids = _loads(
                _row_value(row, "allowed_window_ids_json", "[]") or "[]"
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            allowed_window_ids = []
        if not isinstance(allowed_window_ids, list):
            allowed_window_ids = []
        allowed_window_ids = [
            str(profile_id)
            for profile_id in allowed_window_ids
            if isinstance(profile_id, str) and profile_id
        ]
        queue_state = row["queue_state"]
        real_lifecycle_state = "running" if queue_state == "claimed" else "waiting"
        manual_category = _row_value(row, "manual_category_override")
        lifecycle_state = manual_category or real_lifecycle_state
        disposition = (
            "ignored"
            if real_lifecycle_state == "waiting" and manual_category == "completed"
            else real_lifecycle_state
        )
        return {
            "id": row["id"],
            "username": row["username_display"],
            "kind": row["candidate_kind"],
            "group": "采集失败" if row["candidate_kind"] == "failure" else (row["source_target"] or "其他来源"),
            "source_target": row["source_target"],
            "source_task_id": row["source_task_id"],
            "source_target_id": row["source_target_id"],
            "dispatch_task_id": (
                row["source_task_id"] if row["source_target_id"] else None
            ),
            "requires_source_task": bool(
                _row_value(
                    row,
                    "source_target_exists",
                    1 if row["source_target_id"] else 0,
                )
            ),
            "source_status": row["source_status"],
            "source_window_id": row["source_window_id"],
            "last_error": row["last_error"],
            "profile": profile,
            "allowed_window_ids": allowed_window_ids,
            "window_assignment_mode": (
                "specified" if allowed_window_ids else "automatic"
            ),
            "queue_state": queue_state,
            "locked": bool(_row_value(row, "dispatch_locked", 0)),
            "lifecycle_state": lifecycle_state,
            "real_lifecycle_state": real_lifecycle_state,
            "manual_category": manual_category,
            "manual_category_at": _row_value(row, "manual_category_at"),
            "disposition": disposition,
            "ignored_unexecuted": disposition == "ignored",
            "requires_manual_action": False,
            "queued_task_id": row["queued_task_id"],
            "queued_target_id": row["queued_target_id"],
            "queued_at": row["queued_at"],
            "claimed_at": _row_value(row, "claimed_at"),
            "completed_at": None,
            "target_status": _row_value(row, "target_status"),
            "task_status": _row_value(row, "task_status"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _completed_split_candidate_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            profile = _loads(row["profile_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            profile = {}
        if not isinstance(profile, dict):
            profile = {}
        manual_category = _row_value(row, "manual_category_override")
        return {
            "id": row["id"],
            "username": row["username_display"],
            "kind": "manual",
            "group": row["source_target"] or "其他来源",
            "source_target": row["source_target"],
            "source_task_id": row["source_task_id"],
            "source_target_id": row["source_target_id"],
            "dispatch_task_id": row["source_task_id"],
            "requires_source_task": False,
            "source_status": "completed",
            "source_window_id": row["source_window_id"],
            "last_error": None,
            "profile": profile,
            "allowed_window_ids": [],
            "window_assignment_mode": "automatic",
            # Keep the old three-state queue field compatible.  New renderers use
            # lifecycle_state as the authority and therefore distinguish this
            # immutable record from an actively claimed generation.
            "queue_state": "claimed",
            "lifecycle_state": manual_category or "completed",
            "real_lifecycle_state": "completed",
            "manual_category": manual_category,
            "manual_category_at": _row_value(row, "manual_category_at"),
            "disposition": "completed",
            "ignored_unexecuted": False,
            "requires_manual_action": False,
            "queued_task_id": row["queued_task_id"],
            "queued_target_id": row["queued_target_id"],
            "queued_at": row["queued_at"],
            "claimed_at": row["claimed_at"],
            "completed_at": row["completed_at"],
            "target_status": "completed",
            "task_status": _row_value(row, "task_status"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_split_candidates(
        self,
        owner_user_id: str,
        *,
        row_limit: int | None = None,
        completed_limit: int | None = None,
        candidate_ids: Iterable[str] | None = None,
        platform: str | None = None,
    ) -> list[dict[str, Any]]:
        manual_scope = username_platform_sql(platform, 'candidate.username_norm')
        completed_scope = username_platform_sql(platform, 'history.username_norm')
        failure_scope = username_platform_sql(platform, 'recovery.username_norm')
        normalized_candidate_ids: list[str] | None = None
        if candidate_ids is not None:
            normalized_candidate_ids = list(
                dict.fromkeys(
                    str(candidate_id).strip()
                    for candidate_id in candidate_ids
                    if str(candidate_id).strip()
                )
            )
            if not normalized_candidate_ids:
                return []
            if len(normalized_candidate_ids) > 400:
                raise ValidationError(
                    "At most 400 exact split candidate ids may be listed at once"
                )
        candidate_placeholders = ",".join(
            "?" for _ in (normalized_candidate_ids or [])
        )
        manual_candidate_filter_sql = (
            f"AND candidate.id IN ({candidate_placeholders})"
            if normalized_candidate_ids is not None
            else ""
        )
        completed_candidate_filter_sql = (
            f"AND history.id IN ({candidate_placeholders})"
            if normalized_candidate_ids is not None
            else ""
        )
        failure_candidate_filter_sql = (
            f"AND recovery.candidate_id IN ({candidate_placeholders})"
            if normalized_candidate_ids is not None
            else ""
        )
        candidate_parameters: tuple[Any, ...] = tuple(
            normalized_candidate_ids or []
        )
        row_pagination_sql = ""
        row_parameters: tuple[Any, ...] = (owner_user_id, *candidate_parameters)
        manual_order_sql = (
            "CASE candidate.queue_state "
            "WHEN 'available' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
            "candidate.updated_at DESC, candidate.username_norm"
        )
        bounded_row_limit: int | None = None
        if row_limit is not None:
            # A workbench snapshot asks each priority bucket for one sentinel.
            # The combined result is sliced by the caller, while default/legacy
            # list consumers retain their complete result.
            bounded_row_limit = max(1, min(int(row_limit), 5001))
            row_pagination_sql = "LIMIT ?"
            row_parameters = (
                owner_user_id,
                *candidate_parameters,
                bounded_row_limit,
            )
            manual_order_sql = (
                "CASE WHEN candidate.manual_category_override='completed' "
                "THEN 1 ELSE 0 END, "
                "CASE candidate.queue_state "
                "WHEN 'queued' THEN 0 WHEN 'claimed' THEN 1 ELSE 2 END, "
                "candidate.updated_at DESC, candidate.username_norm"
            )
        completed_pagination_sql = ""
        completed_parameters: tuple[Any, ...] = (
            owner_user_id,
            *candidate_parameters,
        )
        if completed_limit is not None:
            # Snapshot callers request one sentinel beyond the public history
            # page.  Keep the legacy/default call unbounded for API compatibility.
            bounded_completed_limit = max(1, min(int(completed_limit), 5001))
            if bounded_row_limit is not None:
                bounded_completed_limit = min(
                    bounded_completed_limit, bounded_row_limit
                )
            completed_pagination_sql = "LIMIT ?"
            completed_parameters = (
                owner_user_id,
                *candidate_parameters,
                bounded_completed_limit,
            )
        elif bounded_row_limit is not None:
            completed_pagination_sql = "LIMIT ?"
            completed_parameters = (
                owner_user_id,
                *candidate_parameters,
                bounded_row_limit,
            )
        with self.database.read() as connection:
            manual_rows = connection.execute(
                f"""
                SELECT candidate.*,
                       COALESCE(
                         (
                           SELECT json_group_array(ordered.profile_id)
                           FROM (
                             SELECT affinity.profile_id
                             FROM split_candidate_window_affinity affinity
                             WHERE affinity.candidate_id=candidate.id
                             ORDER BY affinity.queue_order
                           ) ordered
                         ),
                         '[]'
                       ) AS allowed_window_ids_json,
                       EXISTS(
                         SELECT 1 FROM task_targets source
                         WHERE source.id=candidate.source_target_id
                       ) AS source_target_exists,
                       target.status AS target_status,
                       task.status AS task_status
                FROM split_candidates candidate
                LEFT JOIN task_targets target
                  ON target.id=candidate.queued_target_id
                 AND target.task_id=candidate.queued_task_id
                LEFT JOIN tasks task ON task.id=candidate.queued_task_id
                WHERE candidate.owner_user_id=? {manual_scope}
                  AND candidate.candidate_kind='manual'
                  {manual_candidate_filter_sql}
                  AND COALESCE(candidate.source_status, '')!='dismissed_by_user'
                  AND NOT EXISTS(
                    SELECT 1 FROM task_target_recovery_controls recovery
                    WHERE recovery.owner_user_id=candidate.owner_user_id
                      AND recovery.target_id=candidate.source_target_id
                      AND recovery.state IN ('pending', 'dismissed')
                  )
                ORDER BY {manual_order_sql}
                {row_pagination_sql}
                """,
                row_parameters,
            ).fetchall()
            completed_rows = connection.execute(
                f"""
                SELECT history.*, task.status AS task_status
                FROM split_candidate_history history
                LEFT JOIN tasks task ON task.id=history.queued_task_id
                WHERE history.owner_user_id=? {completed_scope}
                {completed_candidate_filter_sql}
                ORDER BY history.completed_at DESC, history.id
                {completed_pagination_sql}
                """,
                completed_parameters,
            ).fetchall()
            failure_rows = connection.execute(
                f"""
                SELECT recovery.*,
                       COALESCE(
                         (
                           SELECT manual.profile_json
                           FROM split_candidates manual
                           WHERE manual.owner_user_id=recovery.owner_user_id
                             AND manual.username_norm=recovery.username_norm
                             AND manual.candidate_kind='manual'
                           LIMIT 1
                         ),
                         '{{}}'
                       ) AS profile_json,
                       COALESCE(
                         (
                           SELECT source.allowed_window_ids_json
                           FROM task_targets source
                           WHERE source.id=recovery.target_id
                             AND source.task_id=recovery.source_task_id
                           LIMIT 1
                         ),
                         (
                           SELECT json_group_array(ordered.profile_id)
                           FROM (
                             SELECT affinity.profile_id
                             FROM split_candidate_window_affinity affinity
                             WHERE affinity.candidate_id=recovery.candidate_id
                             ORDER BY affinity.queue_order
                           ) ordered
                         ),
                         '[]'
                       ) AS allowed_window_ids_json,
                       EXISTS(
                         SELECT 1 FROM task_targets source
                         WHERE source.id=recovery.target_id
                       ) AS source_target_exists
                FROM task_target_recovery_controls recovery
                WHERE recovery.owner_user_id=? AND recovery.state='pending' {failure_scope}
                {failure_candidate_filter_sql}
                ORDER BY recovery.updated_at DESC, recovery.target_id
                {row_pagination_sql}
                """,
                row_parameters,
            ).fetchall()
        failures: list[dict[str, Any]] = []
        for row in failure_rows:
            try:
                profile = _loads(row["profile_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                profile = {}
            if not isinstance(profile, dict):
                profile = {}
            try:
                allowed_window_ids = _loads(
                    row["allowed_window_ids_json"] or "[]"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                allowed_window_ids = []
            if not isinstance(allowed_window_ids, list):
                allowed_window_ids = []
            allowed_window_ids = [
                profile_id
                for profile_id in allowed_window_ids
                if isinstance(profile_id, str) and profile_id
            ]
            failures.append({
                "id": row["candidate_id"],
                "username": row["username_display"],
                "kind": "failure",
                "group": "采集失败",
                "source_target": row["username_display"],
                "source_task_id": row["source_task_id"],
                "source_target_id": row["target_id"],
                "dispatch_task_id": (
                    row["source_task_id"] if row["source_target_exists"] else None
                ),
                "requires_source_task": bool(row["source_target_exists"]),
                "source_status": row["source_status"],
                "source_window_id": row["source_window_id"],
                "last_error": row["last_error"],
                "profile": profile,
                "allowed_window_ids": allowed_window_ids,
                "window_assignment_mode": (
                    "specified" if allowed_window_ids else "automatic"
                ),
                "queue_state": "available",
                "lifecycle_state": None,
                "real_lifecycle_state": None,
                "manual_category": None,
                "manual_category_at": None,
                "disposition": "failure",
                "ignored_unexecuted": False,
                "requires_manual_action": True,
                "queued_task_id": None,
                "queued_target_id": None,
                "queued_at": None,
                "claimed_at": None,
                "completed_at": None,
                "target_status": row["source_status"],
                "task_status": None,
                "created_at": row["updated_at"],
                "updated_at": row["updated_at"],
            })
        return (
            failures
            + [self._split_candidate_dict(row) for row in manual_rows]
            + [self._completed_split_candidate_dict(row) for row in completed_rows]
        )

    def _list_split_candidates_by_ids(
        self,
        owner_user_id: str,
        candidate_ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        normalized_ids = list(
            dict.fromkeys(
                str(candidate_id).strip()
                for candidate_id in candidate_ids
                if str(candidate_id).strip()
            )
        )
        if not normalized_ids:
            return []
        rows: list[dict[str, Any]] = []
        for offset in range(0, len(normalized_ids), 400):
            rows.extend(
                self.list_split_candidates(
                    owner_user_id,
                    candidate_ids=normalized_ids[offset : offset + 400],
                )
            )
        rows_by_id = {row["id"]: row for row in rows}
        return [
            rows_by_id[candidate_id]
            for candidate_id in normalized_ids
            if candidate_id in rows_by_id
        ]

    # Aggregated desktop result/history views --------------------------
    def list_all_results(
        self,
        owner_user_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
        platform: str | None = None,
    ) -> list[dict[str, Any]]:
        pagination_sql = ""
        parameters: list[Any] = [owner_user_id]
        bounded_offset = max(0, int(offset))
        if limit is not None:
            pagination_sql = "LIMIT ? OFFSET ?"
            parameters.extend((max(1, min(int(limit), 5001)), bounded_offset))
        elif bounded_offset:
            pagination_sql = "LIMIT -1 OFFSET ?"
            parameters.append(bounded_offset)
        platform_scope = task_platform_sql(platform, 't.settings_json') + username_platform_sql(platform, 'a.current_username_norm')
        with self.database.read() as connection:
            rows = connection.execute(
                f"""
                SELECT r.*, a.instagram_user_id, a.current_username_display,
                       t.name AS task_name, tt.username_display AS source_target,
                       tt.current_window_id
                FROM task_results r
                JOIN tasks t ON t.id=r.task_id
                JOIN task_targets tt ON tt.id=r.target_id
                JOIN instagram_accounts a ON a.id=r.account_id
                WHERE t.owner_user_id=? {platform_scope}
                ORDER BY r.updated_at DESC, r.id DESC
                {pagination_sql}
                """,
                tuple(parameters),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = self._result_dict(row)
            item.update(
                {
                    "task_name": row["task_name"],
                    "source_target": row["source_target"],
                    "window_id": row["current_window_id"],
                    "completed_at": row["updated_at"],
                    "source": item["sources"],
                }
            )
            results.append(item)
        return results

    def count_all_results(self, owner_user_id: str, *, platform: str | None = None) -> int:
        platform_scope = task_platform_sql(platform, 'task.settings_json') + account_platform_sql(platform, 'result.account_id')
        with self.database.read() as connection:
            return int(
                connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM task_results result
                    JOIN tasks task ON task.id=result.task_id
                    WHERE task.owner_user_id=? {platform_scope}
                    """,
                    (owner_user_id,),
                ).fetchone()[0]
            )

    def list_history(
        self,
        owner_user_id: str,
        *,
        task_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        platform: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a target-level history view with one database round-trip.

        ``task_id`` is intentionally optional for the history workspace, while the
        frequently-polled collection page can request only its active task.  Result
        counts and the latest checkpoint are aggregated in SQLite instead of opening
        two additional connections for every target.
        """

        pagination_sql = ""
        parameters: list[Any] = [owner_user_id, task_id, task_id]
        bounded_offset = max(0, int(offset))
        if limit is not None:
            pagination_sql = """
                ORDER BY task.updated_at DESC, target.queue_order, target.id
                LIMIT ? OFFSET ?
            """
            parameters.extend((max(1, min(int(limit), 5001)), bounded_offset))
        elif bounded_offset:
            pagination_sql = """
                ORDER BY task.updated_at DESC, target.queue_order, target.id
                LIMIT -1 OFFSET ?
            """
            parameters.append(bounded_offset)

        platform_scope = task_platform_sql(platform, 'task.settings_json') + username_platform_sql(platform, 'target.username_norm')
        with self.database.read() as connection:
            rows = connection.execute(
                f"""
                WITH filtered_targets AS (
                    SELECT
                        target.*,
                        task.name AS task_name,
                        task.status AS task_status,
                        task.modes_json,
                        task.settings_json,
                        task.assignment_mode,
                        task.restart_count,
                        task.started_at,
                        task.finished_at,
                        task.last_error AS task_last_error,
                        task.updated_at AS task_updated_at
                    FROM task_targets target
                    JOIN tasks task ON task.id=target.task_id
                    WHERE task.owner_user_id=? {platform_scope}
                      AND (? IS NULL OR task.id=?)
                    {pagination_sql}
                ),
                result_counts AS (
                    SELECT
                        result.target_id,
                        COUNT(*) AS collected,
                        SUM(CASE WHEN result.visibility='public' THEN 1 ELSE 0 END)
                            AS public_count,
                        SUM(CASE WHEN result.visibility='private' THEN 1 ELSE 0 END)
                            AS private_count,
                        SUM(CASE WHEN result.visibility NOT IN ('public', 'private')
                                 THEN 1 ELSE 0 END) AS excluded_count,
                        SUM(CASE WHEN result.qualified=1 THEN 1 ELSE 0 END)
                            AS qualified_count
                    FROM task_results result
                    JOIN filtered_targets target ON target.id=result.target_id
                    WHERE 1=1 {account_platform_sql(platform, 'result.account_id')}
                    GROUP BY result.target_id
                ),
                ranked_checkpoints AS (
                    SELECT
                        checkpoint.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY checkpoint.target_id
                            ORDER BY checkpoint.updated_at DESC, checkpoint.rowid DESC
                        ) AS checkpoint_rank
                    FROM task_checkpoints checkpoint
                    JOIN filtered_targets target ON target.id=checkpoint.target_id
                )
                SELECT
                    target.*,
                    COALESCE(counts.collected, 0) AS collected,
                    COALESCE(counts.public_count, 0) AS public_count,
                    COALESCE(counts.private_count, 0) AS private_count,
                    COALESCE(counts.excluded_count, 0) AS excluded_count,
                    COALESCE(counts.qualified_count, 0) AS qualified_count,
                    checkpoint.mode AS checkpoint_mode,
                    checkpoint.stage AS checkpoint_stage,
                    checkpoint.recoverable AS checkpoint_recoverable,
                    checkpoint.counters_json AS checkpoint_counters_json,
                    checkpoint.updated_at AS checkpoint_updated_at
                FROM filtered_targets target
                LEFT JOIN result_counts counts ON counts.target_id=target.id
                LEFT JOIN ranked_checkpoints checkpoint
                  ON checkpoint.target_id=target.id AND checkpoint.checkpoint_rank=1
                ORDER BY target.task_updated_at DESC, target.queue_order, target.id
                """,
                tuple(parameters),
            ).fetchall()
            mode_progress_by_target = self._mode_progress_for_targets(
                connection, (row["id"] for row in rows)
            )
            from .collection_coverage import coverage_for_targets
            mode_coverage_by_target = coverage_for_targets(connection, mode_progress_by_target)

        history: list[dict[str, Any]] = []
        for row in rows:
            modes = _loads(row["modes_json"])
            mode_progress = mode_progress_by_target.setdefault(row["id"], {})
            for mode in modes:
                mode_progress.setdefault(
                    mode,
                    {
                        "source_total": None,
                        "discovered": 0,
                        "processed": 0,
                        "saved": 0,
                        "skipped_global_duplicates": 0,
                        "qualified_for_review": 0,
                    },
                )
            counters = (
                _loads(row["checkpoint_counters_json"])
                if row["checkpoint_counters_json"]
                else {}
            )
            if not isinstance(counters, dict):
                counters = {}
            stage = row["current_stage"] or row["checkpoint_stage"]
            target_error = row["last_error"]
            if not target_error and stage in {
                "waiting_network",
                "interrupted_recoverable",
                "mode_unavailable",
            }:
                target_error = counters.get("message") or counters.get("reason")
            active_network_wait = (
                row["status"] == "waiting_network"
                and stage == "waiting_network"
            )
            window_id = row["current_window_id"] or row["preferred_window_id"]
            history.append(
                {
                    "id": row["id"],
                    "task_id": row["task_id"],
                    "platform": collection_platform(_loads(row["settings_json"])),
                    "target_id": row["id"],
                    "task_name": row["task_name"],
                    "source_target": row["username_display"],
                    "target_account": row["username_display"],
                    "window_id": window_id,
                    "status": row["status"],
                    "task_status": row["task_status"],
                    "modes": modes,
                    "mode_progress": mode_progress,
                    "mode_coverage": mode_coverage_by_target.get(row["id"], {}),
                    "settings": {key: value for key, value in _loads(row["settings_json"]).items()
                                 if key != "facebook_relation_strategy"},
                    "assignment_mode": row["assignment_mode"],
                    "restart_count": row["restart_count"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "current_stage": stage,
                    "last_success_at": row["last_success_at"],
                    "last_error": target_error,
                    "task_last_error": row["task_last_error"],
                    "network_state": "waiting_network" if active_network_wait else (
                        "interrupted_recoverable"
                        if stage == "interrupted_recoverable"
                        else None
                    ),
                    "retry_count": (
                        int(counters.get("retry_count") or 0)
                        if active_network_wait
                        else 0
                    ),
                    "next_retry_at": (
                        counters.get("next_retry_at") if active_network_wait else None
                    ),
                    "counts": {
                        "collected": row["collected"],
                        "public": row["public_count"],
                        "private": row["private_count"],
                        "excluded": row["excluded_count"],
                        "qualified": row["qualified_count"],
                    },
                    "checkpoint": (
                        {
                            "mode": row["checkpoint_mode"],
                            "stage": row["checkpoint_stage"],
                            "recoverable": bool(row["checkpoint_recoverable"]),
                            "counters": counters,
                            "updated_at": row["checkpoint_updated_at"],
                        }
                        if row["checkpoint_mode"]
                        else None
                    ),
                    # Compatibility fields used by the existing renderer.
                    "target_accounts": [row["username_display"]],
                    "window_ids": [window_id] if window_id else [],
                }
            )
        return history

    def count_history(
        self, owner_user_id: str, *, task_id: str | None = None, platform: str | None = None
    ) -> int:
        platform_scope = task_platform_sql(platform, 'task.settings_json') + username_platform_sql(platform, 'target.username_norm')
        with self.database.read() as connection:
            return int(
                connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM task_targets target
                    JOIN tasks task ON task.id=target.task_id
                    WHERE task.owner_user_id=? {platform_scope}
                      AND (? IS NULL OR task.id=?)
                    """,
                    (owner_user_id, task_id, task_id),
                ).fetchone()[0]
            )

    # Events -------------------------------------------------------------
    def list_events(self, owner_user_id: str, *, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM event_log
                WHERE owner_user_id=? AND seq>?
                ORDER BY seq LIMIT ?
                """,
                (owner_user_id, max(0, after_seq), limit),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "payload": _loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # Helpers ------------------------------------------------------------
    @staticmethod
    def _owned_task(connection: sqlite3.Connection, owner_user_id: str, task_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM tasks WHERE id=? AND owner_user_id=?", (task_id, owner_user_id)
        ).fetchone()
        if row is None:
            # Do not reveal whether another local application account owns this id.
            raise NotFoundError("Task not found")
        stored_task_settings(row["settings_json"])
        return row

    @staticmethod
    def _checkpoint_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "target_id": row["target_id"],
            "mode": row["mode"],
            "stage": row["stage"],
            "cursor": _loads(row["cursor_json"]),
            "counters": _loads(row["counters_json"]),
            "recoverable": bool(row["recoverable"]),
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _result_dict(row: sqlite3.Row) -> dict[str, Any]:
        assert_username_platform(None, row["current_username_display"])
        qualified = row["qualified"]
        profile_value = _loads(row["profile_json"])
        profile = profile_value if isinstance(profile_value, dict) else {}
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "target_id": row["target_id"],
            "account_id": row["account_id"],
            "instagram_user_id": row["instagram_user_id"],
            "platform": "instagram",
            "username": row["current_username_display"],
            "sources": _loads(row["sources_json"]),
            "visibility": _normalize_stored_visibility(row["visibility"], profile),
            "profile": profile,
            "screening": _loads(row["screening_json"]),
            "qualified": None if qualified is None else bool(qualified),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _action_target_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username_display"],
            "source_target": row["source_target"],
            "queue_order": row["queue_order"],
            "status": row["status"],
            "deferred_action": (
                row["control_after_attempt"]
                if "control_after_attempt" in row.keys()
                else None
            ),
            "last_error": row["last_error"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _action_attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
        details = _loads(row["details_json"])
        if not isinstance(details, dict):
            details = {}
        greeting_message = details.get("greeting_message")
        return {
            "id": row["id"],
            "target_id": row["target_id"],
            "attempt_number": row["attempt_number"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            # Top-level `message` keeps the renderer/log export simple while
            # `details.greeting_message` retains an explicit audit field.
            "message": greeting_message if isinstance(greeting_message, str) else None,
            "details": details,
        }

    @classmethod
    def _campaign_dict(
        cls,
        row: sqlite3.Row,
        targets: Iterable[sqlite3.Row],
        attempts: Iterable[sqlite3.Row],
    ) -> dict[str, Any]:
        try:
            stored_messages = _loads(row["messages_json"]) if "messages_json" in row.keys() else []
        except (TypeError, ValueError, json.JSONDecodeError):
            stored_messages = []
        messages = [
            str(item)
            for item in stored_messages
            if isinstance(item, str) and str(item).strip()
        ] if isinstance(stored_messages, list) else []
        if not messages and row["message"]:
            messages = [str(row["message"])]
        return {
            "id": row["id"],
            "operation": row["operation"],
            "execution_type": row["execution_type"],
            "profile_id": row["profile_id"],
            "message": row["message"],
            "messages": messages,
            "interval_min_seconds": row["interval_min_seconds"],
            "interval_max_seconds": row["interval_max_seconds"],
            "limit": row["limit_count"],
            "status": row["status"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "last_error": row["last_error"],
            "targets": [cls._action_target_dict(target) for target in targets],
            "attempts": [cls._action_attempt_dict(attempt) for attempt in attempts],
        }

    @staticmethod
    def _find_account(
        connection: sqlite3.Connection, instagram_user_id: str | None, username_norm: str
    ) -> sqlite3.Row | None:
        if instagram_user_id:
            row = connection.execute(
                "SELECT * FROM instagram_accounts WHERE instagram_user_id=?", (instagram_user_id,)
            ).fetchone()
            if row is not None:
                assert_username_platform(None, row["current_username_norm"])
                return row
        row = connection.execute(
            """
            SELECT a.* FROM instagram_username_aliases alias
            JOIN instagram_accounts a ON a.id=alias.account_id
            WHERE alias.username_norm=?
            """,
            (username_norm,),
        ).fetchone()
        if row is not None:
            assert_username_platform(None, row["current_username_norm"])
        return row
