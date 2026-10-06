"""Read-only CSV exports of the current owner's approved account inventory."""
from __future__ import annotations

import csv
import io
import json
import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .errors import ValidationError
from .platform_scope import validate_platform, username_platform_sql, assert_candidate_platforms


HEADERS = (
    '账号', '显示名称', '账号类型', '主页链接', '粉丝数', '关注数', '帖子数',
    '最近发帖时间', '距最近发帖天数', '所在地', '地区信息状态', '采集时间', '审核通过时间',
)
_FORMULA_PREFIX = re.compile(r'^[\s\ufeff\x00-\x1f]*')
_NUMERIC_TEXT = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$')


def _record(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
        return decoded if isinstance(decoded, dict) else {}
    except (TypeError, ValueError):
        return {}


def _text(record: dict, *keys: str) -> str:
    return next((record[key] for key in keys if isinstance(record.get(key), str) and record[key].strip()), '')


def _number(record: dict, *keys: str) -> int | str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, float) and math.isfinite(value) and value >= 0 and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
            return int(value.strip())
    # A missing/unknown count must not be fabricated as zero.
    return ''


def safe_csv_cell(value: Any, *, textual_identity: bool = False) -> str:
    text = '' if value is None else str(value)
    # Excel may ignore whitespace/control characters before a formula. Preserve
    # the content as text and let csv.writer escape commas, quotes and newlines.
    stripped = _FORMULA_PREFIX.sub('', text)
    if stripped.startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    # Usernames/display names are text even when made of digits. An apostrophe
    # avoids Excel losing leading zeros, large integers or interpreting 1e10.
    # Numeric statistic columns remain numeric; never emit an ="..." formula.
    if textual_identity and _NUMERIC_TEXT.fullmatch(text.strip()):
        return "'" + text
    return text


def _location(profile: dict, screening: dict) -> tuple[str, str]:
    location = _record(screening.get('location'))
    value = (_text(profile, 'location_zh', 'location', 'location_country', 'country', 'country_name')
             or _text(location, 'value', 'country', 'location')
             or _text(screening, 'location_country', 'country'))
    state = _text(profile, 'location_status', 'location_visibility') or _text(location, 'status', 'reason')
    if value.strip().lower() in {'地区不公开', '所在地不公开', 'not disclosed', 'not public'} or state in {
        'hidden', 'not_public', 'not_disclosed', 'country_not_disclosed', 'location_not_disclosed',
    }:
        return '', '地区不公开'
    if value:
        return value, '已显示地区'
    return '', '无地区信息'


def _row(candidate) -> tuple:
    profile, screening = _record(candidate['profile_json']), _record(candidate['screening_json'])
    username = candidate['current_username_display']
    location, location_state = _location(profile, screening)
    public = candidate['visibility'] == 'public'
    profile_url = f'https://www.instagram.com/{quote(username, safe="._")}/'
    # Story activity is deliberately not exported as a recent post. Private
    # accounts cannot expose a verified post age through the collector.
    post_state = _text(profile, 'post_activity_status')
    post_days = _number(profile, 'post_activity_days') if public and post_state in {
        'identified', 'timestamp_found', 'timestamp_read',
    } else ''
    return (username, _text(profile, 'display_name', 'full_name', 'name'),
            '公开' if public else '私密', profile_url,
            _number(profile, 'followers', 'followers_count', 'follower_count'),
            _number(profile, 'following', 'following_count'),
            _number(profile, 'posts', 'posts_count', 'media_count'),
            _text(profile, 'recent_post_datetime') if public else '', post_days,
            location, location_state, candidate['created_at'], candidate['reviewed_at'])


def export_accounts(database, owner: str, *, visibility: str, scope: str,
                    candidate_ids: list[str] | None = None, platform: str | None = None) -> dict[str, Any]:
    validate_platform(platform)
    if visibility not in {'public', 'private'} or scope not in {'selected', 'all'}:
        raise ValidationError('无效的导出账号类型或范围')
    if candidate_ids is not None and (not isinstance(candidate_ids, list) or len(candidate_ids) > 5000
            or any(not isinstance(item, str) or not item.strip() or len(item) > 100 for item in candidate_ids)):
        raise ValidationError('无效的导出账号标识，最多选择 5000 个账号')
    ids = list(dict.fromkeys(candidate_ids or []))
    if scope == 'selected' and not ids:
        raise ValidationError('请先选择要导出的账号')
    if scope == 'all' and ids:
        raise ValidationError('导出全部时不能同时指定账号标识')
    output = io.StringIO(newline='')
    output.write('\ufeff')
    writer = csv.writer(output, lineterminator='\r\n')
    writer.writerow(HEADERS)
    count = 0
    with database.read() as connection:
        # All batches share a single read snapshot: collection/review cannot
        # insert an account into the middle of this export. No DB mutation.
        connection.execute('BEGIN')
        if scope == 'selected':
            assert_candidate_platforms(connection, owner, ids, platform)
        batches = [None] if scope == 'all' else [ids[start:start + 400] for start in range(0, len(ids), 400)]
        for batch in batches:
            # Select only exportable columns, never review_cache or credentials.
            sql = '''SELECT account.current_username_display,candidate.visibility,
                candidate.profile_json,candidate.screening_json,candidate.created_at,candidate.reviewed_at
                FROM workbench_candidates candidate
                JOIN instagram_accounts account ON account.id=candidate.account_id
                WHERE candidate.owner_user_id=? AND candidate.status='approved' AND candidate.visibility=?
                AND NOT EXISTS(
                    SELECT 1 FROM workbench_candidate_dismissals dismissal
                    WHERE dismissal.candidate_id=candidate.id
                )
                AND NOT EXISTS(
                    SELECT 1 FROM instagram_username_aliases alias
                    WHERE alias.account_id=candidate.account_id AND EXISTS(
                        SELECT 1 FROM action_success_ledger success
                        WHERE success.owner_user_id=? AND success.operation=?
                          AND success.username_norm=alias.username_norm
                    )
                )'''
            # Match the visible approved inventory exactly, including accounts
            # removed after a successful action under any known username alias.
            parameters: tuple = (owner, visibility, owner, 'greet' if visibility == 'public' else 'follow')
            sql += username_platform_sql(platform, 'account.current_username_norm')
            if batch is not None:
                sql += ' AND candidate.id IN (' + ','.join('?' for _ in batch) + ')'
                parameters += tuple(batch)
            sql += ' ORDER BY candidate.reviewed_at DESC,candidate.id DESC'
            for candidate in connection.execute(sql, parameters):
                writer.writerow([safe_csv_cell(cell, textual_identity=index in (0, 1))
                                 for index, cell in enumerate(_row(candidate))])
                count += 1
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    return {'filename': f'Juxin-{visibility}-accounts-{stamp}.csv',
            'mime_type': 'text/csv;charset=utf-8', 'csv': output.getvalue(), 'row_count': count,
            'skipped_count': len(ids) - count if scope == 'selected' else 0,
            'visibility': visibility, 'scope': scope,
            **({'platform': platform} if platform is not None else {})}
