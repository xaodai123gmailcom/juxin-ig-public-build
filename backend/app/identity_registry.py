"""Permanent global identities and owner provenance for safe workspace backups."""
import json
import re
import uuid


def remember_identity_owner(c, account_id, owner_user_id, now):
    """Keep provenance after a queue row is removed, without granting claim rights."""
    if owner_user_id is None:
        return
    c.execute('''INSERT INTO global_identity_owners(account_id,owner_user_id,first_seen_at,last_seen_at)
        VALUES(?,?,?,?) ON CONFLICT(owner_user_id,account_id) DO UPDATE SET
        first_seen_at=MIN(global_identity_owners.first_seen_at,excluded.first_seen_at),
        last_seen_at=MAX(global_identity_owners.last_seen_at,excluded.last_seen_at)''',
        (account_id, owner_user_id, now, now))


def identity_has_durable_history(c, account_id):
    """Use business history if a restored database lost its lookup membership."""
    return c.execute("""SELECT 1 FROM workbench_candidates WHERE account_id=?
        UNION ALL SELECT 1 FROM workbench_collection_exclusions WHERE account_id=?
        UNION ALL SELECT 1 FROM task_results WHERE account_id=?
        UNION ALL SELECT 1 FROM task_result_duplicate_archive WHERE account_id=?
        UNION ALL SELECT 1 FROM global_identity_owners WHERE account_id=? LIMIT 1""",
        (account_id,) * 5).fetchone() is not None


def remember_registered_identity(c, account_id, source, owner_user_id, now):
    """Persist dedupe membership in the caller's terminal-record transaction.

    This only repairs lookup/provenance rows. It never deletes history or moves
    an alias to a different identity; failed terminal writes roll back together.
    """
    account = c.execute("SELECT * FROM instagram_accounts WHERE id=?", (account_id,)).fetchone()
    if account is None:
        raise ValueError("Registered identity does not exist")
    c.execute("""INSERT OR IGNORE INTO instagram_username_aliases(
        account_id,username_norm,first_seen_at,last_seen_at) VALUES(?,?,?,?)""",
        (account_id, account["current_username_norm"], account["first_seen_at"], now))
    previous = c.execute("SELECT sources_json FROM global_seen WHERE account_id=?", (account_id,)).fetchone()
    sources = set(json.loads(previous[0])) if previous else set()
    sources.add(source)
    c.execute("""INSERT INTO global_seen(account_id,sources_json,first_seen_at,last_seen_at)
        VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET
        sources_json=excluded.sources_json,
        first_seen_at=MIN(global_seen.first_seen_at,excluded.first_seen_at),
        last_seen_at=MAX(global_seen.last_seen_at,excluded.last_seen_at)""",
        (account_id, json.dumps(sorted(sources)), account["first_seen_at"], now))
    remember_identity_owner(c, account_id, owner_user_id, now)


def merge_identity_owners(c, source_account_id, canonical_account_id):
    """Move only provenance when an empty renamed-identity placeholder collapses."""
    c.execute('''INSERT INTO global_identity_owners(account_id,owner_user_id,first_seen_at,last_seen_at)
        SELECT ?,owner_user_id,first_seen_at,last_seen_at FROM global_identity_owners
        WHERE account_id=?
        ON CONFLICT(owner_user_id,account_id) DO UPDATE SET
        first_seen_at=MIN(global_identity_owners.first_seen_at,excluded.first_seen_at),
        last_seen_at=MAX(global_identity_owners.last_seen_at,excluded.last_seen_at)''',
        (canonical_account_id, source_account_id))
    c.execute('DELETE FROM global_identity_owners WHERE account_id=?', (source_account_id,))


def reserve_split_identity(c, username, display, now, *, source='split_source', owner_user_id=None):
    username = username.strip().lower()
    if not re.fullmatch(r'[a-z0-9._]{1,30}', username):
        return
    account = c.execute('SELECT account_id FROM instagram_username_aliases WHERE username_norm=?', (username,)).fetchone()
    if account:
        ident = account[0]
    else:
        current = c.execute('SELECT id FROM instagram_accounts WHERE current_username_norm=?', (username,)).fetchone()
        ident = current[0] if current else str(uuid.uuid4())
        if not current:
            c.execute('INSERT INTO instagram_accounts(id,current_username_norm,current_username_display,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)', (ident, username, display, now, now))
        c.execute('INSERT OR IGNORE INTO instagram_username_aliases VALUES(?,?,?,?)', (ident, username, now, now))
    previous = c.execute('SELECT sources_json FROM global_seen WHERE account_id=?', (ident,)).fetchone()
    sources = set(json.loads(previous[0])) if previous else set()
    sources.add(source)
    c.execute('''INSERT INTO global_seen VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET
        sources_json=excluded.sources_json,
        first_seen_at=MIN(global_seen.first_seen_at,excluded.first_seen_at),
        last_seen_at=MAX(global_seen.last_seen_at,excluded.last_seen_at)''', (ident, json.dumps(sorted(sources)), now, now))
    remember_identity_owner(c, ident, owner_user_id, now)
    if previous is None:
        c.execute('''UPDATE workbench_state_revision
            SET revision=revision+1,updated_at=? WHERE singleton_id=1''', (now,))
    return ident


def repair_global_registry(c):
    # Membership/provenance repair never deletes, merges, reassigns or changes a
    # business record. Orphan global identities stay local when ownership is unknown.
    c.execute('''INSERT OR IGNORE INTO instagram_username_aliases(account_id,username_norm,first_seen_at,last_seen_at)
        SELECT id,current_username_norm,first_seen_at,last_seen_at FROM instagram_accounts''')
    c.execute('''INSERT OR IGNORE INTO global_seen(account_id,sources_json,first_seen_at,last_seen_at)
        SELECT a.id,'["restored_business_identity"]',a.first_seen_at,a.last_seen_at FROM instagram_accounts a
        WHERE a.id IN (SELECT account_id FROM workbench_candidates UNION SELECT account_id FROM workbench_collection_exclusions
                       UNION SELECT account_id FROM task_results UNION SELECT account_id FROM workbench_identity_claims
                       UNION SELECT account_id FROM task_result_duplicate_archive
                       UNION SELECT account_id FROM global_identity_owners)''')
    c.execute('''INSERT INTO global_identity_owners(account_id,owner_user_id,first_seen_at,last_seen_at)
        SELECT account_id,owner_user_id,MIN(seen_at),MAX(seen_at) FROM (
            SELECT account_id,owner_user_id,created_at AS seen_at FROM workbench_candidates
            UNION ALL SELECT account_id,owner_user_id,excluded_at FROM workbench_collection_exclusions
            UNION ALL SELECT r.account_id,t.owner_user_id,r.created_at
                FROM task_results r JOIN tasks t ON t.id=r.task_id
            UNION ALL SELECT account_id,claimed_by_user_id,claimed_at FROM workbench_identity_claims
            UNION ALL SELECT account_id,owner_user_id,created_at FROM task_result_duplicate_archive
        ) GROUP BY account_id,owner_user_id
        ON CONFLICT(owner_user_id,account_id) DO UPDATE SET
        first_seen_at=MIN(global_identity_owners.first_seen_at,excluded.first_seen_at),
        last_seen_at=MAX(global_identity_owners.last_seen_at,excluded.last_seen_at)''')
    name_sources = (
        ('split_source', '''SELECT username_norm,username_display,owner_user_id,created_at AS seen_at FROM split_candidates
            UNION ALL SELECT username_norm,username_display,owner_user_id,created_at FROM split_candidate_history'''),
        ('collection_source', '''SELECT target.username_norm,target.username_display,task.owner_user_id,target.created_at AS seen_at
            FROM task_targets target JOIN tasks task ON task.id=target.task_id
            UNION ALL SELECT recovery.username_norm,recovery.username_display,recovery.owner_user_id,recovery.updated_at
                FROM task_target_recovery_controls recovery JOIN app_users owner ON owner.id=recovery.owner_user_id'''),
        ('action_target', '''SELECT target.username_norm,target.username_display,campaign.owner_user_id,campaign.created_at AS seen_at
            FROM action_targets target JOIN action_campaigns campaign ON campaign.id=target.campaign_id
            UNION ALL SELECT username_norm,username_display,owner_user_id,completed_at FROM action_success_ledger'''),
    )
    for source, query in name_sources:
        rows = c.execute(f'''SELECT username_norm,MIN(username_display),owner_user_id,MIN(seen_at)
            FROM ({query}) GROUP BY username_norm,owner_user_id''')
        for username, display, owner, seen_at in rows:
            reserve_split_identity(c, username, display, seen_at, source=source, owner_user_id=owner)
    c.execute('UPDATE global_seen_stats SET total_count=(SELECT COUNT(*) FROM global_seen) WHERE singleton_id=1')
