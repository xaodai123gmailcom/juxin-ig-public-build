"""Owner-scoped manual review batches, separate from qualification decisions."""
from __future__ import annotations

from typing import Any

from .errors import ValidationError
from .platform_scope import validate_platform, account_platform_sql, assert_candidate_platforms


def _lane(visibility: str, stage: int) -> None:
    if visibility not in {'public', 'private'} or type(stage) is not int or stage not in (1, 2):
        raise ValidationError('无效的审核层级或账号类型')


def _page(offset: int, limit: int) -> None:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise ValidationError('无效的分页范围')


def query_review_queue(service, owner: str, *, visibility: str, review_stage: int,
                       offset: int = 0, limit: int = 100, platform: str | None = None) -> dict[str, Any]:
    _lane(visibility, review_stage)
    _page(offset, limit)
    validate_platform(platform)
    with service.database.read() as connection:
        # Rows, lane totals and revision must describe one committed snapshot.
        connection.execute('BEGIN')
        counts = {key: {'stage1': 0, 'stage2': 0} for key in ('public', 'private')}
        for row in connection.execute(
            "SELECT visibility,review_stage,COUNT(*) AS total FROM workbench_candidates "
            "WHERE owner_user_id=? AND status='pending' "
            + account_platform_sql(platform, 'workbench_candidates.account_id')
            + " GROUP BY visibility,review_stage",
            (owner,),
        ):
            counts[row['visibility']][f"stage{row['review_stage']}"] = row['total']
        scope_hint = "INDEXED BY idx_candidates_platform_review_stage" if platform is not None else ""
        rows = connection.execute(
            f"""SELECT candidate.*,account.instagram_user_id,account.current_username_display
            FROM workbench_candidates candidate {scope_hint}
            JOIN instagram_accounts account ON account.id=candidate.account_id
            WHERE candidate.owner_user_id=? AND candidate.status='pending'
              AND candidate.visibility=? AND candidate.review_stage=?"""
            + account_platform_sql(platform, 'candidate.account_id')
            + " ORDER BY candidate.created_at,candidate.id LIMIT ? OFFSET ?",
            (owner, visibility, review_stage, limit, offset),
        ).fetchall()
        revision = connection.execute(
            'SELECT revision FROM workbench_state_revision WHERE singleton_id=1'
        ).fetchone()[0]
    total = counts[visibility][f'stage{review_stage}']
    return {
        'items': [service._avatar_only_review_candidate(service._workbench_candidate_dict(row))
                  for row in rows],
        'total': total, 'offset': offset, 'limit': limit,
        'has_more': offset + len(rows) < total, 'counts': counts, 'snapshot_seq': revision,
        **({'platform': platform} if platform is not None else {}),
    }


def move_review_stage(service, owner: str, *, candidate_ids: list[str], visibility: str,
                      from_stage: int, to_stage: int, platform: str | None = None) -> dict[str, Any]:
    from .service import _bump_workbench_revision, isoformat
    _lane(visibility, from_stage)
    _lane(visibility, to_stage)
    validate_platform(platform)
    if from_stage == to_stage:
        raise ValidationError('转入层级必须不同于当前层级')
    if not isinstance(candidate_ids, list) or not 1 <= len(candidate_ids) <= 2000:
        raise ValidationError('请选择 1 至 2000 个账号')
    if any(not isinstance(item, str) or not item.strip() or len(item) > 100 for item in candidate_ids):
        raise ValidationError('无效的审核账号标识')
    ids = list(dict.fromkeys(candidate_ids))
    now = isoformat()
    moved = set()
    with service.database.write() as connection:
        assert_candidate_platforms(connection, owner, ids, platform)
        # BEGIN IMMEDIATE serializes decisions and transfers. Explicit IDs avoid
        # an "all pending" race with collection; batches respect old SQLite limits.
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            marks = ','.join('?' for _ in batch)
            selected = connection.execute(
                f"SELECT id FROM workbench_candidates WHERE owner_user_id=? "
                f"AND visibility=? AND status='pending' AND review_stage=? AND id IN ({marks})",
                (owner, visibility, from_stage, *batch),
            ).fetchall()
            selected_ids = [row['id'] for row in selected]
            if not selected_ids:
                continue
            chosen = ','.join('?' for _ in selected_ids)
            connection.execute(
                f"UPDATE workbench_candidates SET review_stage=?,review_transferred_at=?,updated_at=? "
                f"WHERE id IN ({chosen}) AND owner_user_id=? AND status='pending' "
                "AND visibility=? AND review_stage=?",
                (to_stage, now, now, *selected_ids, owner, visibility, from_stage),
            )
            moved.update(selected_ids)
        revision = _bump_workbench_revision(connection, now) if moved else connection.execute(
            'SELECT revision FROM workbench_state_revision WHERE singleton_id=1'
        ).fetchone()[0]
    return {
        'moved_ids': [item for item in ids if item in moved],
        'skipped_ids': [item for item in ids if item not in moved],
        'moved_count': len(moved), 'from_stage': from_stage, 'to_stage': to_stage,
        'review_transferred_at': now if moved else None, 'snapshot_seq': revision,
        **({'platform': platform} if platform is not None else {}),
    }
