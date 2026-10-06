"""Single-photo posting ledger. Receipts and global asset reservations are never UI-deleted."""
def initialize_posting_schema(c):
    for sql in (
        '''CREATE TABLE IF NOT EXISTS posting_jobs(id TEXT PRIMARY KEY,owner_user_id TEXT NOT NULL,
        request_key TEXT NOT NULL,provider_name TEXT NOT NULL DEFAULT 'pexels',theme TEXT NOT NULL,caption TEXT NOT NULL,profile_id TEXT NOT NULL DEFAULT '',
        expected_username TEXT NOT NULL DEFAULT '',expected_actor_id TEXT NOT NULL DEFAULT '',asset_id TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'preparing',
        lease_token TEXT NOT NULL DEFAULT '',attempt_id TEXT NOT NULL DEFAULT '',submitted_at TEXT,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,hidden_at TEXT,message TEXT NOT NULL DEFAULT '',
        UNIQUE(owner_user_id,request_key))''',
        '''CREATE TABLE IF NOT EXISTS posting_assets(id TEXT PRIMARY KEY,provider TEXT NOT NULL DEFAULT 'pexels',
        provider_id TEXT NOT NULL,job_id TEXT NOT NULL UNIQUE,sha256 TEXT,render_sha256 TEXT,path TEXT NOT NULL DEFAULT '',
        recovery_path TEXT NOT NULL DEFAULT '',state TEXT NOT NULL DEFAULT 'reserved',source_url TEXT NOT NULL,
        photographer TEXT NOT NULL,photographer_url TEXT NOT NULL DEFAULT '',download_url TEXT NOT NULL,
        preview TEXT NOT NULL DEFAULT '',source_license TEXT NOT NULL DEFAULT '',license_url TEXT NOT NULL DEFAULT '',credit TEXT NOT NULL DEFAULT '',verified_at TEXT NOT NULL DEFAULT '',provider_sha1 TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,UNIQUE(provider,provider_id))''',
        'CREATE UNIQUE INDEX IF NOT EXISTS posting_asset_hash ON posting_assets(sha256) WHERE sha256 IS NOT NULL',
        'CREATE UNIQUE INDEX IF NOT EXISTS posting_asset_render_hash ON posting_assets(render_sha256) WHERE render_sha256 IS NOT NULL',
        '''CREATE TABLE IF NOT EXISTS posting_receipts(job_id TEXT PRIMARY KEY,owner_user_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,username TEXT NOT NULL,post_url TEXT NOT NULL DEFAULT '',confirmed_at TEXT NOT NULL,
        day_utc TEXT NOT NULL,evidence_json TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS posting_receipts_report_period ON posting_receipts(owner_user_id,julianday(confirmed_at))',
        'CREATE INDEX IF NOT EXISTS posting_jobs_page ON posting_jobs(owner_user_id,hidden_at,created_at,id)',
        'CREATE INDEX IF NOT EXISTS posting_receipts_date ON posting_receipts(confirmed_at,owner_user_id,profile_id)',
        'CREATE INDEX IF NOT EXISTS posting_receipts_owner_date ON posting_receipts(owner_user_id,confirmed_at)',
        'CREATE INDEX IF NOT EXISTS posting_jobs_queue ON posting_jobs(status,created_at)',
        'CREATE INDEX IF NOT EXISTS posting_jobs_owner ON posting_jobs(owner_user_id,hidden_at,created_at)',
        "CREATE INDEX IF NOT EXISTS posting_jobs_holds ON posting_jobs(profile_id,owner_user_id,id) WHERE lease_token<>''",
        'CREATE INDEX IF NOT EXISTS posting_jobs_window ON posting_jobs(profile_id,status)',
        '''CREATE TABLE IF NOT EXISTS posting_api_cache(cache_key TEXT PRIMARY KEY,response_json TEXT NOT NULL,expires_at REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS posting_api_usage(bucket TEXT PRIMARY KEY,count INTEGER NOT NULL DEFAULT 0)''',
        '''CREATE TABLE IF NOT EXISTS posting_api_backoff(id INTEGER PRIMARY KEY CHECK(id=1),until_epoch REAL NOT NULL DEFAULT 0)''',
        '''CREATE TABLE IF NOT EXISTS posting_retry_history(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,
        owner_user_id TEXT NOT NULL,attempt_id TEXT NOT NULL DEFAULT '',submitted_at TEXT,
        failure_stage TEXT NOT NULL,failure_code TEXT NOT NULL,retried_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS posting_withdraw_history(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,
        owner_user_id TEXT NOT NULL,profile_id TEXT NOT NULL,expected_username TEXT NOT NULL,
        expected_actor_id TEXT NOT NULL,withdrawn_at TEXT NOT NULL)''',
    ):c.execute(sql)

    if 'expected_actor_id' not in {r[1] for r in c.execute('PRAGMA table_info(posting_jobs)')}:c.execute("ALTER TABLE posting_jobs ADD COLUMN expected_actor_id TEXT NOT NULL DEFAULT ''")
    current={r[1] for r in c.execute('PRAGMA table_info(posting_jobs)')}
    for name in ('failure_stage','failure_code'):
        if name not in current:c.execute('ALTER TABLE posting_jobs ADD COLUMN '+name+" TEXT NOT NULL DEFAULT ''")
    if 'queue_revision' not in current:
        c.execute('ALTER TABLE posting_jobs ADD COLUMN queue_revision INTEGER NOT NULL DEFAULT 0')

    for table,columns in {'posting_jobs':['provider_name'], 'posting_assets':['source_license','license_url','credit','verified_at','provider_sha1']}.items():
        current={r[1] for r in c.execute('PRAGMA table_info('+table+')')}
        for name in columns:
            if name not in current:c.execute('ALTER TABLE '+table+' ADD COLUMN '+name+" TEXT NOT NULL DEFAULT "+("'pexels'" if name=='provider_name' else "''"))
