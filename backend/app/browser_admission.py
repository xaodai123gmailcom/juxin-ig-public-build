"""Read-only durable fences shared by preflight and actual window admission."""
from .errors import ConflictError


def assert_no_durable_window_hold(connection, owner_user_id, profile_id):
    # Some isolated callers have only the posting schema. Missing optional
    # ledgers are distinct from a present ledger with an unresolved hold.
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_jobs'").fetchone():
        pending = connection.execute(
            "SELECT id,owner_user_id FROM studio_jobs WHERE profile_id=? AND kind='nurture' "
            "AND status='completed' AND json_extract(result_json,'$.window_hold')=1 LIMIT 1",
            (profile_id,),
        ).fetchone()
        if pending:
            raise ConflictError('养号窗口清理仍待确认，该窗口不能被其他操作接管', details={
                'profile_id': profile_id, 'operation_type': 'studio',
                'entity_id': pending['id'] if pending['owner_user_id'] == owner_user_id else None,
            })
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='posting_jobs'").fetchone():
        pending = connection.execute(
            "SELECT id,owner_user_id FROM posting_jobs WHERE profile_id=? "
            "AND lease_token IS NOT NULL AND lease_token<>'' LIMIT 1", (profile_id,),
        ).fetchone()
        if pending:
            raise ConflictError('发帖提交或清理仍待确认，该窗口不能被其他操作接管', details={
                'profile_id': profile_id, 'operation_type': 'posting',
                'entity_id': pending['id'] if pending['owner_user_id'] == owner_user_id else None,
            })
