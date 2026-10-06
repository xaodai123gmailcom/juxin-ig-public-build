"""Owner-isolated snapshots; success counts come from durable publication records."""
from .service import isoformat

def initialize_posting_stats(c):
    c.execute('''CREATE TABLE IF NOT EXISTS posting_account_snapshots(
        owner_user_id TEXT NOT NULL REFERENCES app_users(id),profile_id TEXT NOT NULL,
        username TEXT NOT NULL DEFAULT '',posts_count INTEGER,status TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',checked_at TEXT NOT NULL,
        PRIMARY KEY(owner_user_id,profile_id))''')
    c.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(24,datetime('now'))")
    columns={r[1] for r in c.execute('PRAGMA table_info(posting_account_snapshots)')}
    for column in ('followers_count','following_count'):
        if column not in columns:
            c.execute(f'ALTER TABLE posting_account_snapshots ADD COLUMN {column} INTEGER')
    c.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(38,datetime('now'))")

def save_account_snapshot(db,owner,profile,data):
    def valid(key):
        value=data.get(key)
        return value if type(value) is int and value>=0 else None
    count=valid('posts_count')
    with db.write() as c:
        c.execute('''INSERT INTO posting_account_snapshots
            (owner_user_id,profile_id,username,posts_count,status,message,checked_at,followers_count,following_count)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(owner_user_id,profile_id) DO UPDATE SET username=excluded.username,
            posts_count=excluded.posts_count,followers_count=excluded.followers_count,
            following_count=excluded.following_count,status=excluded.status,message=excluded.message,checked_at=excluded.checked_at''',
            (owner,profile,str(data.get('username',''))[:30],count,'ok' if count is not None else 'unavailable',str(data.get('message',''))[:220],isoformat(),valid('followers_count'),valid('following_count')))

def account_stats(c,owner):
    rows={r['profile_id']:dict(r) for r in c.execute('SELECT profile_id,username,posts_count,followers_count,following_count,status,message,checked_at FROM posting_account_snapshots WHERE owner_user_id=?',(owner,))}
    for row in c.execute("SELECT profile_id,count(*) AS published_count FROM studio_jobs WHERE owner_user_id=? AND kind='posting' AND status='completed' AND json_extract(result_json,'$.published')=1 GROUP BY profile_id",(owner,)):
        rows.setdefault(row['profile_id'],{'profile_id':row['profile_id'],'username':'','posts_count':None,'status':'unread','message':'尚未读取','checked_at':''})['published_count']=row['published_count']
    # Full durable history, including archived records; the UI's recent 500 jobs
    # must never cap lifetime counts. Resuming one job counts that round once.
    for row in c.execute("""SELECT profile_id,COUNT(*) AS nurture_count,
        MAX(COALESCE(json_extract(result_json,'$.nurture_started_at'),updated_at)) AS last_nurture_at
        FROM studio_jobs WHERE owner_user_id=? AND kind='nurture' AND
          (status='completed' OR cursor>0 OR json_extract(result_json,'$.nurture_started_at') IS NOT NULL)
        GROUP BY profile_id""",(owner,)):
        value=rows.setdefault(row['profile_id'],{'profile_id':row['profile_id'],'username':'','posts_count':None,'status':'unread','message':'尚未读取','checked_at':''})
        value.update(nurture_count=row['nurture_count'],last_nurture_at=row['last_nurture_at'])
    for row in rows.values():
        row.setdefault('published_count',0)
        row.setdefault('nurture_count',0)
        row.setdefault('last_nurture_at','')
        row.setdefault('followers_count',None)
        row.setdefault('following_count',None)
    return list(rows.values())
