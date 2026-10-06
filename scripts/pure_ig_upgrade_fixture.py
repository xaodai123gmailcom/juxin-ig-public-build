"""Isolated installed-Core upgrade fixture; never accepts a user's live database."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

NOW = '2026-10-02T00:00:00+00:00'
IG_ID = 'installed-pure-ig-keep-account'
FB_ID = 'installed-pure-ig-remove-account'
FB_TASK = 'installed-pure-ig-remove-task'


def seed(database, owner, *, isolated_directory):
    database = Path(database).resolve()
    directory = Path(isolated_directory).resolve()
    if database.parent != directory or not directory.name.startswith('Juxin-CoreSmoke-'):
        raise ValueError('Upgrade fixture requires the isolated Core probe directory')
    with closing(sqlite3.connect(database)) as c:
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('BEGIN IMMEDIATE')
        for account, username, stable in ((IG_ID, 'same.ig.upgrade', '123456789012345'),
                                          (FB_ID, 'fb:same.ig.upgrade', 'fbid:123456789012345')):
            c.execute('INSERT INTO instagram_accounts VALUES(?,?,?,?,?,?)',
                      (account, stable, username, 'Same Display Name', NOW, NOW))
            c.execute('INSERT INTO instagram_username_aliases VALUES(?,?,?,?)', (account, username, NOW, NOW))
            c.execute('INSERT INTO global_seen VALUES(?,?,?,?)', (account, '["followers"]', NOW, NOW))
        c.execute('''INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at)
            VALUES(?,?,?,'completed','["followers"]','{"platform":"facebook"}',?,?)''',
            (FB_TASK, owner, 'Retired synthetic task', NOW, NOW))
        c.execute('''INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,created_at,updated_at)
            VALUES(?,?,?,'Same Display Name',0,'completed',?,?)''',
            ('installed-pure-ig-remove-target', FB_TASK, 'fb:same.ig.upgrade', NOW, NOW))
        c.execute('''INSERT INTO task_results(id,task_id,target_id,account_id,sources_json,visibility,profile_json,screening_json,qualified,created_at,updated_at)
            VALUES(?,?,?,?,?,'public',?,'{}',0,?,?)''',
            ('installed-pure-ig-remove-result', FB_TASK, 'installed-pure-ig-remove-target', FB_ID,
             '["followers"]', '{"platform":"facebook","bio":"retire-this-synthetic-content"}', NOW, NOW))
        c.execute('DELETE FROM schema_migrations WHERE version=39')
        c.commit()
    return {'ig_added': 1, 'ig_digest': retained_digest(database),
            'ig_count_deltas': {'instagram_accounts': 1, 'instagram_username_aliases': 1, 'global_seen': 1}}


def retained_digest(database):
    with closing(sqlite3.connect(database)) as c:
        rows = {table: c.execute(f'SELECT * FROM {table} WHERE '+column+'=?', (IG_ID,)).fetchall()
                for table, column in (('instagram_accounts','id'), ('instagram_username_aliases','account_id'), ('global_seen','account_id'))}
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def verify(database, fixture):
    if retained_digest(database) != fixture['ig_digest']:
        raise RuntimeError('Installed pure IG upgrade changed the colliding IG identity')
    with closing(sqlite3.connect(database)) as c:
        for table, column, value in (('instagram_accounts','id',FB_ID),
            ('instagram_username_aliases','account_id',FB_ID), ('global_seen','account_id',FB_ID),
            ('tasks','id',FB_TASK), ('task_targets','task_id',FB_TASK), ('task_results','task_id',FB_TASK)):
            if c.execute(f'SELECT 1 FROM {table} WHERE {column}=?', (value,)).fetchone():
                raise RuntimeError('Installed pure IG upgrade retained retired rows: ' + table)
        if c.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Installed pure IG upgrade broke foreign keys')
    return {'verified': True, 'ig_identity_hash_preserved': True, 'same_display_and_numeric_collision': True,
            'classified_rows_removed': True, 'foreign_keys_valid': True, 'synthetic': True, 'user_data_touched': False}
