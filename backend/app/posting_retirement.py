"""Recoverably retire idle legacy posting data without taking browser ownership.

The archive is a separate, durable SQLite database beside the current database.
It commits and is verified before active rows are removed. A crash between those
commits leaves duplicate recovery evidence, never an unbacked deletion. Live,
ambiguous, malformed and leased work stays unchanged in internal quarantine.
No browser lease, account inventory, login or shared identity data is deleted.
"""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone


class PostingRetirementError(RuntimeError):
    """No active rows are removed when a recovery archive cannot be verified."""


_POSTING_TABLES = (
    'posting_jobs', 'posting_assets', 'posting_receipts',
    'posting_retry_history', 'posting_withdraw_history',
    'posting_api_cache', 'posting_api_usage', 'posting_api_backoff',
)
_STUDIO_KINDS = {'posting', 'material'}
_IDLE_STATES = {'preparing', 'ready', 'prepared', 'review', 'queued',
                'waiting_window', 'paused', 'failed', 'cancelled', 'completed'}
_TERMINAL = {'completed', 'failed', 'cancelled'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _exists(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _object(value):
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False, default=lambda value: {
                          '$sqlite_blob_base64': base64.b64encode(value).decode('ascii')})


def _submission_evidence(result):
    """Unknown result shapes fail closed; historical attempts are retained too."""
    if result is None:
        return True
    if result.get('window_hold') or result.get('inflight'):
        return True
    failure = result.get('failure')
    if ('failure' in result and not isinstance(failure, dict)) or (
            isinstance(failure, dict) and (failure.get('result_uncertain')
                or failure.get('uncertain') or failure.get('stage') in {'unknown', 'submitting', 'needs_review'})):
        return True
    cleanup = result.get('window_cleanup')
    if cleanup is not None and (not isinstance(cleanup, dict) or
            cleanup.get('state') not in {'closed', 'reconciled_closed', 'released'}):
        return True
    return any(result.get(key) for key in (
        'attempt_id', 'submitted_at', 'submission', 'submit_started',
        'submission_started', 'result_uncertain', 'uncertain', 'unknown',
    ))


def legacy_job_requires_fence(row, *, standalone=False):
    """Read-only fence for retained jobs, including a missing/mismatched lease."""
    row = dict(row)
    if standalone:
        if row.get('lease_token'):
            return True
        if row.get('status') not in _IDLE_STATES:
            return True
        return row.get('status') != 'completed' and bool(
            row.get('attempt_id') or row.get('submitted_at') or row.get('failure_stage') == 'unknown')
    if row.get('kind') not in _STUDIO_KINDS:
        return False
    result = _object(row.get('result_json'))
    if (row.get('inflight') or row.get('status') not in _IDLE_STATES
            or row.get('status') not in _TERMINAL and row.get('cursor', 0) != 0):
        return True
    if _submission_evidence(result):
        return True
    # An uncompleted row with a success receipt is internally inconsistent.
    return row.get('status') != 'completed' and bool(result.get('published'))


def legacy_profile_hold(connection, profile_id):
    """Return an unresolved legacy owner; never synthesize or adopt a lease."""
    for table, suffix in (('studio_jobs', " AND kind IN ('posting','material')"),
                          ('posting_jobs', '')):
        if not _exists(connection, table):
            continue
        for row in connection.execute(f'SELECT * FROM {table} WHERE profile_id=?' + suffix,
                                      (profile_id,)):
            leased = _exists(connection, 'browser_operation_leases') and connection.execute(
                'SELECT 1 FROM browser_operation_leases WHERE operation_type=? AND entity_id=?',
                ('posting' if table == 'posting_jobs' else 'studio', row['id']),
            ).fetchone() is not None
            if (leased or legacy_job_requires_fence(row, standalone=table == 'posting_jobs')
                    or table == 'posting_jobs' and row['status'] != 'completed'
                    and _exists(connection, 'posting_receipts')
                    and connection.execute('SELECT 1 FROM posting_receipts WHERE job_id=?', (row['id'],)).fetchone()):
                return dict(row)
    return None


def legacy_window_state(connection, *, leases=None, include_nurture=False):
    """Batch-read retained ownership evidence within the caller's transaction.

    Old posting ledgers are optional and can lack later optional columns. Keep
    SELECT * and the shared Python fence predicate rather than projecting newer
    columns or interpreting uncertain JSON in SQL. Only receipt existence uses
    an indexed per-job lookup; the number of SQL statements is row-independent.
    """
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('studio_jobs','posting_jobs','posting_receipts','browser_operation_leases')")}
    if leases is None:
        leases = (connection.execute(
            'SELECT operation_type,entity_id FROM browser_operation_leases').fetchall()
            if 'browser_operation_leases' in tables else [])
    # A lease for a job still fences its durable original profile even when its
    # profile, login or token disagrees. Do not narrow this to matching owners.
    leased_entities = {(row['operation_type'], row['entity_id']) for row in leases}
    studio_entities, holds, cleanup = set(), {}, []
    hold_owners = {}
    def retain_hold(row):
        profile = row['profile_id']
        holds.setdefault(profile, row)
        hold_owners.setdefault(profile, set()).add(row['owner_user_id'])
    if 'studio_jobs' in tables:
        condition = "kind IN ('posting','material')"
        if include_nurture:
            # CASE keeps malformed legacy posting JSON out of json_extract.
            # Malformed completed nurture JSON still raises, preserving the
            # existing fail-closed behavior rather than returning it as free.
            condition = """CASE WHEN kind IN ('posting','material') THEN 1
                WHEN kind='nurture' AND status='completed'
                THEN json_extract(result_json,'$.window_hold')=1 ELSE 0 END"""
        for record in connection.execute('SELECT * FROM studio_jobs WHERE ' + condition):
            row = dict(record)
            if row['kind'] == 'nurture':
                cleanup.append(row)
                continue
            studio_entities.add(row['id'])
            if row['profile_id'] and (('studio', row['id']) in leased_entities
                    or legacy_job_requires_fence(row)):
                retain_hold(row)
    if 'posting_jobs' in tables:
        receipt = ('EXISTS(SELECT 1 FROM posting_receipts r WHERE r.job_id=p.id)'
                   if 'posting_receipts' in tables else '0')
        for record in connection.execute(
                'SELECT p.*, ' + receipt + ' AS _legacy_has_receipt FROM posting_jobs p'):
            # Positional extraction cannot be shadowed by a historical column
            # that happens to use the private computed alias. Preserve that
            # original column too when returning the unchanged durable row.
            has_receipt = record[-1]
            row = {name: record[index] for index, name in enumerate(record.keys()[:-1])}
            if row['profile_id'] and (('posting', row['id']) in leased_entities
                    or legacy_job_requires_fence(row, standalone=True)
                    or row['status'] != 'completed' and has_receipt):
                retain_hold(row)
    for profile, row in holds.items():
        # A planner-dependent first row cannot grant reconciliation authority.
        # This is derived display metadata on a copy, never durable ownership.
        row['_legacy_owner_ambiguous'] = len(hold_owners[profile]) > 1
    return studio_entities, [holds[profile] for profile in sorted(holds)], cleanup


def legacy_window_holds(connection):
    return legacy_window_state(connection)[1]


def legacy_studio_lease_entities(connection):
    if not _exists(connection, 'studio_jobs'):
        return set()
    return {row[0] for row in connection.execute(
        "SELECT id FROM studio_jobs WHERE kind IN ('posting','material')")}


def _asset_references(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'asset_id' and isinstance(item, str):
                found.add(item)
            elif key == 'asset_ids' and isinstance(item, list):
                found.update(v for v in item if isinstance(v, str))
            found.update(_asset_references(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_asset_references(item))
    return found


def _reject_symlinks(path):
    for entry in (path, *path.parents):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
                getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise PostingRetirementError('Retirement paths must not contain symbolic links or reparse points')


def _archive_location(database):
    path = Path(database.path).absolute()
    _reject_symlinks(path)
    root = path.parent.resolve(strict=True)
    folder = root / (path.name + '.posting-retirement')
    _reject_symlinks(folder)
    folder.mkdir(mode=0o700, exist_ok=True)
    if not folder.is_dir() or folder.resolve() != folder or not folder.is_relative_to(root):
        raise PostingRetirementError('Retirement archive is outside the current data directory')
    target = folder / 'archive.sqlite3'
    for candidate in (target, *(Path(str(target) + suffix) for suffix in ('-journal', '-wal', '-shm'))):
        _reject_symlinks(candidate)
    return root, folder, target


def _digest_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def _sync_dir(folder):
    # Windows cannot open directories as ordinary file handles. File fsync and
    # SQLite FULL synchronous still apply there; os.replace is same-volume.
    if os.name == 'nt':
        return
    descriptor = os.open(folder, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_material(source_text, root, folder):
    source = Path(source_text)
    if not source.is_absolute() or '..' in source.parts:
        raise PostingRetirementError('Material path is not an absolute managed path')
    _reject_symlinks(source)
    source = source.resolve(strict=True)
    if not source.is_relative_to(root) or source.is_relative_to(folder):
        raise PostingRetirementError('Material is outside the current managed data directory')
    materials = folder / 'materials'
    _reject_symlinks(materials)
    materials.mkdir(mode=0o700, exist_ok=True)
    temporary = None
    try:
        with source.open('rb') as original:
            before = os.fstat(original.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise PostingRetirementError('Material is not a regular file')
            digest = hashlib.sha256()
            size = 0
            with tempfile.NamedTemporaryFile(dir=materials, prefix='.pending-', delete=False) as copy:
                temporary = Path(copy.name)
                for block in iter(lambda: original.read(1024 * 1024), b''):
                    copy.write(block)
                    digest.update(block)
                    size += len(block)
                copy.flush()
                os.fsync(copy.fileno())
            after = os.fstat(original.fileno())
        _reject_symlinks(source)
        current = source.stat()
        identity = lambda value: (value.st_size, value.st_mtime_ns, value.st_ino, value.st_dev)
        if identity(before) != identity(after) or identity(before) != identity(current) or size != before.st_size:
            raise PostingRetirementError('Material changed during backup')
        sha = digest.hexdigest()
        target = materials / sha
        _reject_symlinks(target)
        if target.exists():
            if _digest_file(target) != (sha, size):
                raise PostingRetirementError('Existing archived material failed verification')
        else:
            os.replace(temporary, target)
            temporary = None
            _sync_dir(materials)
        if _digest_file(target) != (sha, size):
            raise PostingRetirementError('Archived material failed byte verification')
        return str(source), sha, str(target), size
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _open_archive(path):
    archive = sqlite3.connect(path, timeout=30)
    try:
        archive.row_factory = sqlite3.Row
        archive.execute('PRAGMA journal_mode=DELETE')
        archive.execute('PRAGMA synchronous=FULL')
        archive.executescript('''
            CREATE TABLE IF NOT EXISTS archived_rows(
                source_table TEXT NOT NULL, row_key TEXT NOT NULL,
                row_sha256 TEXT NOT NULL, row_json TEXT NOT NULL,
                archived_at TEXT NOT NULL,
                PRIMARY KEY(source_table,row_key,row_sha256));
            CREATE TABLE IF NOT EXISTS archived_schema(
                source_table TEXT NOT NULL, schema_sha256 TEXT NOT NULL,
                schema_sql TEXT NOT NULL, archived_at TEXT NOT NULL,
                PRIMARY KEY(source_table,schema_sha256));
            CREATE TABLE IF NOT EXISTS verified_closures(
                owner_user_id TEXT NOT NULL, profile_id TEXT NOT NULL,
                generation_sha256 TEXT NOT NULL, evidence_json TEXT NOT NULL, verified_at TEXT NOT NULL,
                PRIMARY KEY(owner_user_id,profile_id,generation_sha256));
            CREATE TABLE IF NOT EXISTS files(
                original_path TEXT NOT NULL, sha256 TEXT NOT NULL,
                archive_path TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                archived_at TEXT NOT NULL, PRIMARY KEY(original_path,sha256));
        ''')
    except BaseException:
        archive.close()
        raise
    return archive


def _key_columns(connection, table):
    return [row['name'] for row in sorted(connection.execute(f'PRAGMA table_info({table})'),
                                        key=lambda row: row['pk']) if row['pk']]


def _collect(connection, active_studio_ids, active_posting_ids, closed_keys=frozenset()):
    """Return full row snapshots plus a strictly narrower deletion set."""
    rows = []
    removable = set()
    quarantined = []
    leases = [dict(row) for row in connection.execute('SELECT * FROM browser_operation_leases')]
    studio = [dict(row) for row in connection.execute('SELECT * FROM studio_jobs')] if _exists(connection, 'studio_jobs') else []
    standalone = [dict(row) for row in connection.execute('SELECT * FROM posting_jobs')] if _exists(connection, 'posting_jobs') else []
    posting_kept = set()
    studio_kept = []
    receipts = {row[0] for row in connection.execute('SELECT job_id FROM posting_receipts')} if _exists(connection, 'posting_receipts') else set()
    leased_posting = {row['entity_id'] for row in leases if row['operation_type'] == 'posting'}
    leased_studio = {row['entity_id'] for row in leases if row['operation_type'] == 'studio'}
    for row in standalone:
        held = (row['id'] in active_posting_ids or row['id'] in leased_posting
                or legacy_job_requires_fence(row, standalone=True)
                or row['id'] in receipts and row['status'] != 'completed')
        if held and ('posting_jobs', row['id']) not in closed_keys:
            posting_kept.add(row['id'])
            quarantined.append({'source_table': 'posting_jobs', 'id': row['id'],
                                'owner_user_id': row['owner_user_id']})
    for row in studio:
        if row['kind'] not in _STUDIO_KINDS:
            studio_kept.append(row)
            continue
        rows.append(('studio_jobs', row))
        held = (row['id'] in active_studio_ids or row['id'] in leased_studio
                or legacy_job_requires_fence(row))
        if held and ('studio_jobs', row['id']) not in closed_keys:
            studio_kept.append(row)
            quarantined.append({'source_table': 'studio_jobs', 'id': row['id'],
                                'owner_user_id': row['owner_user_id']})
        else:
            removable.add(('studio_jobs', row['id']))
    protected_assets = set()
    protected_owners = set()
    for row in studio_kept:
        for field in ('config_json', 'result_json'):
            value = _object(row[field])
            if value is None:
                protected_owners.add(row['owner_user_id'])
            else:
                protected_assets.update(_asset_references(value))
    for table in _POSTING_TABLES:
        if not _exists(connection, table):
            continue
        for record in connection.execute(f'SELECT * FROM {table}'):
            row = dict(record)
            rows.append((table, row))
            job = row.get('job_id', row.get('id') if table == 'posting_jobs' else None)
            if (job not in posting_kept and job not in active_posting_ids
                    and (job not in leased_posting or ('posting_jobs', job) in closed_keys)):
                # A dedup asset may be shared by an old inconsistent job row.
                if table != 'posting_assets' or not any(
                        j.get('asset_id') == row['id'] and j['id'] in posting_kept for j in standalone):
                    removable.add((table, _json([row[k] for k in _key_columns(connection, table)])))
    if _exists(connection, 'studio_templates'):
        for record in connection.execute("SELECT * FROM studio_templates WHERE kind IN ('posting','material')"):
            row = dict(record)
            rows.append(('studio_templates', row))
            removable.add(('studio_templates', _json([row[k] for k in _key_columns(connection, 'studio_templates')])))
    if _exists(connection, 'studio_assets'):
        for record in connection.execute('SELECT * FROM studio_assets'):
            row = dict(record)
            rows.append(('studio_assets', row))
            if row['id'] not in protected_assets and row['owner_user_id'] not in protected_owners:
                removable.add(('studio_assets', _json([row[k] for k in _key_columns(connection, 'studio_assets')])))
    return rows, removable, quarantined


def retire_legacy_posting(database, *, active_studio_ids=(), active_posting_ids=()):
    """Verified, idempotent archive of idle data; browser leases are never released."""
    return _retire_legacy_posting(database, active_studio_ids=active_studio_ids,
                                 active_posting_ids=active_posting_ids)


def _retire_legacy_posting(database, *, active_studio_ids=(), active_posting_ids=(), closed_scope=None):
    """Archive and remove conclusively idle posting data; never release a lease.

    Invoke after schema initialization and before recovery/dispatch under the
    instance lock. Later calls are safe after posting admission is disabled;
    callers must pass any still-live legacy manager IDs. Unsafe rows remain
    internal quarantine and the returned summary must not be called full removal.
    Missing/out-of-root material or any corrupt backup aborts all active deletion.
    External user-selected originals and managed originals are never moved/deleted.
    """
    result = {'archive_path': None, 'archived_rows': 0, 'retired_rows': 0,
              'quarantined_jobs': [], 'copied_files': 0}
    with database.browser_surface_lock:
        temporary_lock = database._instance_lock_file is None
        if temporary_lock:
            database.acquire_instance_lock()
        try:
            with database.write() as connection:
                closed_keys = set()
                if closed_scope is not None:
                    owner, profile, original, active_profile, evidence = closed_scope
                    if _closed_snapshot(database, connection, owner, profile, active_profile) != original:
                        _blocked('窗口记录已变化，请刷新后重试')
                    closed_keys = {(table, row['id']) for table, row in original['jobs']}
                rows, removable, quarantined = _collect(
                    connection, set(active_studio_ids), set(active_posting_ids), closed_keys)
                if closed_scope is not None:
                    target_posting = {ident for table, ident in closed_keys if table == 'posting_jobs'}
                    target_assets = set()
                    for table, row in original['jobs']:
                        if table == 'studio_jobs':
                            target_assets.update(_asset_references(_object(row['config_json'])))
                            target_assets.update(_asset_references(_object(row['result_json'])))
                        elif row.get('asset_id'):
                            target_assets.add(row['asset_id'])
                    rows = [(table, row) for table, row in rows if (table, row.get('id')) in closed_keys
                            or row.get('job_id') in target_posting
                            or table in {'studio_assets', 'posting_assets'} and row.get('id') in target_assets]
                    quarantined = [row for row in quarantined
                                   if (row['source_table'], row['id']) in closed_keys]
                    if original['lease'] is not None:
                        rows.append(('browser_operation_leases', original['lease']))
                result['quarantined_jobs'] = quarantined
                if not rows:
                    return result
                root, folder, path = _archive_location(database)
                result['archive_path'] = str(path)
                archive = _open_archive(path)
                entries = []
                schemas = {}
                files = {}
                stamp = _now()
                try:
                    with archive:
                        closure_record = None
                        if closed_scope is not None:
                            closure_record = (owner, profile, hashlib.sha256(_json([original, evidence]).encode()).hexdigest(),
                                              _json(evidence), stamp)
                            archive.execute('INSERT OR IGNORE INTO verified_closures VALUES(?,?,?,?,?)', closure_record)
                        for table, row in rows:
                            columns = _key_columns(connection, table)
                            if not columns:
                                raise PostingRetirementError('Source table has no stable archive key')
                            key = _json([row[column] for column in columns])
                            payload = _json(row)
                            sha = hashlib.sha256(payload.encode('utf-8')).hexdigest()
                            entries.append((table, key, sha, payload, row, columns))
                            result['archived_rows'] += archive.execute(
                                'INSERT OR IGNORE INTO archived_rows VALUES(?,?,?,?,?)',
                                (table, key, sha, payload, stamp)).rowcount
                            # Table SQL alone omits explicit partial UNIQUE
                            # indexes (material hashes/draft assignment) and
                            # triggers. Save executable complete object topology.
                            definitions = connection.execute(
                                "SELECT sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL "
                                "AND type IN ('table','index','trigger') ORDER BY "
                                "CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name", (table,),
                            ).fetchall()
                            schema = '\n'.join(definition[0].rstrip(';') + ';' for definition in definitions)
                            schemas[table] = (hashlib.sha256(schema.encode()).hexdigest(), schema)
                            archive.execute('INSERT OR IGNORE INTO archived_schema VALUES(?,?,?,?)',
                                (table, *schemas[table], stamp))
                            if table in {'posting_assets', 'studio_assets'}:
                                for field in ('path', 'recovery_path'):
                                    material = row.get(field)
                                    if material and material not in files:
                                        files[material] = _copy_material(material, root, folder)
                                        archive.execute('INSERT OR IGNORE INTO files VALUES(?,?,?,?,?)',
                                                        (*files[material], stamp))
                    archive.close()
                    archive = None
                    _sync_dir(folder)
                    # Persist the new archive directory's link in its parent too.
                    _sync_dir(root)
                    # Reopen the committed archive rather than trusting an INSERT
                    # or a digest computed before storage reached its transaction.
                    with closing(sqlite3.connect(path)) as verified:
                        if verified.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or verified.execute('PRAGMA foreign_key_check').fetchone():
                            raise PostingRetirementError('Retirement archive integrity check failed')
                        if closure_record is not None:
                            saved = verified.execute('SELECT evidence_json FROM verified_closures WHERE owner_user_id=? AND profile_id=? AND generation_sha256=?',
                                                     closure_record[:3]).fetchone()
                            if saved is None or saved[0] != closure_record[3]:
                                raise PostingRetirementError('Closed-window archive proof failed verification')
                        for table, (schema_sha, schema) in schemas.items():
                            saved = verified.execute('SELECT schema_sql FROM archived_schema WHERE source_table=? AND schema_sha256=?',
                                                     (table, schema_sha)).fetchone()
                            if saved is None or saved[0] != schema or hashlib.sha256(saved[0].encode()).hexdigest() != schema_sha:
                                raise PostingRetirementError('Archived schema failed verification')
                        for table, key, sha, payload, row, columns in entries:
                            saved = verified.execute(
                                'SELECT row_json FROM archived_rows WHERE source_table=? AND row_key=? AND row_sha256=?',
                                (table, key, sha)).fetchone()
                            if saved is None or saved[0] != payload or hashlib.sha256(
                                    saved[0].encode('utf-8')).hexdigest() != sha:
                                raise PostingRetirementError('Archived row failed verification')
                        for source, sha, target, size in files.values():
                            saved = verified.execute('SELECT archive_path,size_bytes FROM files WHERE original_path=? AND sha256=?',
                                                     (source, sha)).fetchone()
                            if saved != (target, size) or _digest_file(Path(target)) != (sha, size):
                                raise PostingRetirementError('Archived material manifest failed verification')
                    if closed_scope is not None and _closed_snapshot(
                            database, connection, owner, profile, active_profile) != original:
                        _blocked('窗口记录在核验期间已变化，请刷新后重试')
                    result['copied_files'] = len(files)
                    connection.execute('''CREATE TABLE IF NOT EXISTS posting_retirement_tombstones(
                        source_table TEXT NOT NULL,row_key TEXT NOT NULL,row_sha256 TEXT NOT NULL,
                        retired_at TEXT NOT NULL,PRIMARY KEY(source_table,row_key,row_sha256))''')
                    for table, key, sha, payload, row, columns in entries:
                        can_remove = ((table, row.get('id')) in removable if table == 'studio_jobs'
                                      else (table, key) in removable)
                        if not can_remove:
                            continue
                        connection.execute('INSERT OR IGNORE INTO posting_retirement_tombstones VALUES(?,?,?,?)',
                                           (table, key, sha, stamp))
                        clause = ' AND '.join('"' + column + '"=?' for column in columns)
                        result['retired_rows'] += connection.execute(
                            f'DELETE FROM {table} WHERE {clause}', tuple(row[column] for column in columns)).rowcount
                    if closed_scope is not None and original['lease'] is not None:
                        lease = original['lease']
                        removed = connection.execute(
                            'DELETE FROM browser_operation_leases WHERE profile_id=? AND owner_user_id=? '
                            'AND operation_type=? AND entity_id=? AND lease_token=?',
                            tuple(lease[key] for key in ('profile_id', 'owner_user_id', 'operation_type', 'entity_id', 'lease_token')),
                        ).rowcount
                        if removed != 1:
                            _blocked('窗口占用凭证已变化，已保留占用')
                finally:
                    if archive is not None:
                        archive.close()
        except (OSError, sqlite3.DatabaseError, ValueError, TypeError) as error:
            raise PostingRetirementError('Posting retirement backup failed; active data was retained') from error
        finally:
            if temporary_lock:
                database.release_instance_lock()
    return result



def _blocked(message):
    from .errors import ConflictError
    raise ConflictError(message, details={'reason': 'unresolved_window_hold'})


def _closed_snapshot(database, connection, owner, profile, active_profile):
    if not callable(active_profile) or active_profile(profile):
        _blocked('窗口仍有活动任务，不能核验历史占用')
    jobs = []
    for table, suffix in (('studio_jobs', " AND kind IN ('posting','material')"), ('posting_jobs', '')):
        if not _exists(connection, table):
            continue
        for row in connection.execute(f'SELECT * FROM {table} WHERE profile_id=?' + suffix + ' ORDER BY id', (profile,)):
            row = dict(row)
            if row['owner_user_id'] != owner:
                _blocked('窗口仍有其他用户的历史占用，请由所属用户核验')
            if row.get('lease_token') in database.live_browser_lease_tokens:
                _blocked('窗口仍有活动占用凭证，请等待任务释放')
            jobs.append((table, row))
    lease = connection.execute('SELECT * FROM browser_operation_leases WHERE profile_id=?', (profile,)).fetchone()
    if lease is not None:
        lease = dict(lease)
        if lease['owner_user_id'] != owner or lease['lease_token'] in database.live_browser_lease_tokens:
            _blocked('窗口仍有活动或其他用户的占用凭证')
        expected_table = {'studio': 'studio_jobs', 'posting': 'posting_jobs'}.get(lease['operation_type'])
        matched = next((row for table, row in jobs if table == expected_table and row['id'] == lease['entity_id']), None)
        if matched is None or (expected_table == 'posting_jobs' and matched.get('lease_token') not in ('', lease['lease_token'])):
            _blocked('窗口占用记录不一致，已保留占用等待核验')
        if any(row.get('lease_token') not in (None, '', lease['lease_token']) for _, row in jobs):
            _blocked('窗口存在不同占用记录，已保留占用等待核验')
    return {'jobs': jobs, 'lease': lease}


def reconcile_retired_posting(database, provider, owner_user_id, profile_id, *, active_profile):
    """Explicit closed-window verification; never opens/closes or resubmits.

    The provider owns absence verification and freezes connections, opening,
    closing and action tickets through the transaction. Exact original records
    are archived, including unresolved external outcomes. Only their closed
    matching generation may be removed. A successor or unknown owner is refused.
    """
    from .errors import UpstreamUnavailableError
    with database.browser_surface_lock:
        with database.read() as connection:
            original = _closed_snapshot(database, connection, owner_user_id, profile_id, active_profile)
        if not original['jobs']:
            return {'status': 'already_clear', 'reconciled': False, 'retired_rows': 0}
        guard = getattr(provider, 'closed_profile_guard', None)
        if not callable(guard):
            raise UpstreamUnavailableError('当前窗口服务不能安全核验关闭状态，请保留占用并更新桌面程序',
                                           details={'reason': 'closed_profile_guard_unsupported'})
        with guard(profile_id, owner_user_id) as evidence:
            if (not isinstance(evidence, dict) or evidence.get('closed') is not True
                    or evidence.get('profile_id') != profile_id
                    or evidence.get('owner_user_id') != owner_user_id
                    or evidence.get('verification') != 'desktop-absence-v1'):
                raise UpstreamUnavailableError('窗口服务未提供可信的关闭核验，已保留占用')
            result = _retire_legacy_posting(database, closed_scope=(
                owner_user_id, profile_id, original, active_profile, dict(evidence)))
    return {'status': 'reconciled_closed', 'reconciled': True, 'retired_rows': result['retired_rows']}
