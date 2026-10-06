"""Durable, owner-isolated publishing and nurture state."""

def initialize_studio_schema(c):
    statements = [
        '''CREATE TABLE IF NOT EXISTS studio_templates(
            owner_user_id TEXT NOT NULL REFERENCES app_users(id), kind TEXT NOT NULL,
            config_json TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(owner_user_id,kind))''',
        '''CREATE TABLE IF NOT EXISTS studio_assets(
            id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL REFERENCES app_users(id),
            source TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL, media_type TEXT NOT NULL,
            preview TEXT NOT NULL DEFAULT '', attribution TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS studio_jobs(
            id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL REFERENCES app_users(id),
            request_key TEXT NOT NULL, kind TEXT NOT NULL, profile_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued', config_json TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
            total_steps INTEGER NOT NULL DEFAULT 0, inflight INTEGER NOT NULL DEFAULT 0,
            result_json TEXT NOT NULL DEFAULT '{}', message TEXT NOT NULL DEFAULT '',
            due_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(owner_user_id,request_key))''',
        '''CREATE TABLE IF NOT EXISTS studio_daily_actions(
            owner_user_id TEXT NOT NULL REFERENCES app_users(id), profile_id TEXT NOT NULL,
            day TEXT NOT NULL, action TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(owner_user_id,profile_id,day,action))''',
        'CREATE INDEX IF NOT EXISTS studio_jobs_due ON studio_jobs(status,due_at)',
        'CREATE INDEX IF NOT EXISTS studio_jobs_owner ON studio_jobs(owner_user_id,created_at)',
        'CREATE INDEX IF NOT EXISTS studio_assets_owner ON studio_assets(owner_user_id,created_at)',
    ]
    from .posting_account_stats import initialize_posting_stats
    initialize_posting_stats(c)
    for statement in statements:
        c.execute(statement)
    c.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(20,datetime('now'))")

    if 'deleted_at' not in {r[1] for r in c.execute('PRAGMA table_info(studio_jobs)')}:
        c.execute('ALTER TABLE studio_jobs ADD COLUMN deleted_at TEXT')
    c.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(25,datetime('now'))")

    for column in ('source_draft_id','draft_target_profile_id'):
        if column not in {r[1] for r in c.execute('PRAGMA table_info(studio_jobs)')}:
            c.execute(f"ALTER TABLE studio_jobs ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS studio_one_draft_assignment ON studio_jobs(owner_user_id,source_draft_id) WHERE source_draft_id<>''")
    c.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(26,datetime('now'))")
