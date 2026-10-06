"""Instagram-only scopes, including omitted legacy API arguments.

Instagram's historical identity namespace is unprefixed; Facebook identities are
stored with the reserved ``fb:`` prefix. Never infer platform from display-page
JSON: older Instagram rows legitimately have no platform field.
"""
from __future__ import annotations

from .errors import ConflictError, ValidationError


def validate_platform(platform: str | None) -> str | None:
    if platform is not None and (not isinstance(platform, str) or platform != 'instagram'):
        raise ValidationError('此版本仅支持 Instagram')
    return platform


def username_platform_sql(platform: str | None, expression: str) -> str:
    """Return an AND predicate for a trusted, code-owned username expression."""
    validate_platform(platform)
    return f" AND COALESCE({expression}, '') NOT GLOB 'fb:*' "


def task_platform_sql(platform: str | None, settings_expression: str) -> str:
    """Task platform is immutable task settings; missing legacy settings mean IG."""
    validate_platform(platform)
    # CASE is deliberately nested: json_type/json_extract must never run on
    # malformed legacy text. Only a valid object with a missing key defaults IG.
    return (f" AND CASE WHEN json_valid({settings_expression}) THEN "
            f"CASE WHEN json_type({settings_expression})='object' THEN "
            f"CASE WHEN json_type({settings_expression}, '$.platform') IS NULL THEN 'instagram' "
            f"ELSE json_extract({settings_expression}, '$.platform') END "
            "ELSE 'unknown' END ELSE 'unknown' END='instagram' ")


def account_platform_sql(platform: str | None, account_id_expression: str) -> str:
    validate_platform(platform)
    return (f" AND EXISTS(SELECT 1 FROM instagram_accounts platform_account INDEXED BY idx_accounts_platform_scope "
            f"WHERE platform_account.id={account_id_expression}"
            f"{username_platform_sql(platform, 'platform_account.current_username_norm')}) ")


def assert_username_platform(platform: str | None, username: str) -> None:
    validate_platform(platform)
    if str(username).strip().lower().startswith('fb:'):
        raise ConflictError('所选记录不属于当前平台，请刷新后重新选择',
                            details={'reason': 'platform_scope_mismatch', 'platform': 'instagram'})


def assert_candidate_platforms(connection, owner: str, candidate_ids, platform: str | None) -> None:
    validate_platform(platform)
    ids = list(dict.fromkeys(candidate_ids))
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        marks = ','.join('?' for _ in batch)
        rows = connection.execute(
            f'SELECT account.current_username_norm FROM workbench_candidates candidate '
            f'JOIN instagram_accounts account ON account.id=candidate.account_id '
            f'WHERE candidate.owner_user_id=? AND candidate.id IN ({marks})', (owner, *batch))
        for row in rows:
            assert_username_platform(platform, row['current_username_norm'])


def global_seen_platform_total(connection, platform: str | None) -> int:
    validate_platform(platform)
    return int(connection.execute(
        'SELECT total_count FROM global_seen_platform_stats WHERE platform=?', ('instagram',)).fetchone()[0])
