"""Independent stdlib-only oracle for recoverable retired-feature storage.

Reads SQLite and file bytes directly. It does not import the production migration
or trust its receipt, and must run before accepting installed proof flags.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3


def require(value, message):
    if not value:
        raise RuntimeError(message)


def canonical(row):
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def normalized(row, original):
    # Removed-feature startup has no approved legacy schema/data additions.
    # Do not silently strip candidate-created fields from the original oracle.
    return dict(row)


def capture_schema(database, tables):
    """Capture authority from the independent legacy seed before candidate code.

    The caller saves this value in its prelaunch, hash-bound input manifest.
    Never obtain this authority from a post-migration database.
    """
    result = {}
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as c:
        for table in tables:
            objects = c.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL AND type IN ('table','index','trigger')", (table,)).fetchall()
            require(any(row[0] == 'table' for row in objects), 'Legacy seed schema missing: ' + table)
            result[table] = [{'type': kind, 'name': name, 'sql': sql} for kind, name, sql in sorted(objects)]
    return result


def inspect_archive(database, expected_rows, *, expected_schema, material_sha256=None, retained_rows=()):
    database = Path(database)
    directory = Path(str(database) + '.posting-retirement')
    archive = directory / 'archive.sqlite3'
    require(archive.is_file() and not archive.is_symlink(), 'Verified recoverable posting archive is missing')
    with closing(sqlite3.connect(archive.resolve().as_uri() + '?mode=ro', uri=True)) as c:
        c.row_factory = sqlite3.Row
        require(c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Posting archive integrity failed')
        saved = [dict(row) for row in c.execute('SELECT * FROM archived_rows')]
        schemas = [dict(row) for row in c.execute('SELECT * FROM archived_schema')]
        require(len(saved) == sum(map(len, expected_rows.values())), 'Posting archive row count changed')
        expected_hashes = []
        for table, originals in expected_rows.items():
            candidates = [row for row in saved if row['source_table'] == table]
            require(len(candidates) == len(originals), 'Posting archive table count changed: ' + table)
            for original in originals:
                matches = [row for row in candidates if normalized(json.loads(row['row_json']), original) == original]
                require(len(matches) == 1, 'Original posting row missing or changed in archive: ' + table)
                row = matches[0]
                decoded = json.loads(row['row_json'])
                require(row['row_sha256'] == hashlib.sha256(canonical(decoded).encode()).hexdigest(), 'Posting archive row digest changed')
                if (table, original.get('id')) not in retained_rows:
                    expected_hashes.append((table, row['row_key'], row['row_sha256']))
        files = [dict(row) for row in c.execute('SELECT * FROM files')]
        if material_sha256 is not None:
            matches = [row for row in files if row['sha256'] == material_sha256]
            require(len(matches) == 1, 'Original material archive mapping is missing or duplicate')
            row = matches[0]
            path = Path(row['archive_path'])
            if not path.is_absolute():
                path = directory / path
            require(path.resolve().is_relative_to(directory.resolve()) and path.is_file() and not path.is_symlink(), 'Material archive path is unsafe or missing')
            data = path.read_bytes()
            require(type(row['size_bytes']) is int and len(data) == row['size_bytes'] and hashlib.sha256(data).hexdigest() == material_sha256, 'Original archived material bytes changed')
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as c:
        for table, originals in expected_rows.items():
            if not originals:
                continue
            # This authority was captured before the candidate process launched.
            # No schema additions are currently required for retired tables;
            # future migrations must enumerate any permitted additions explicitly.
            source = expected_schema.get(table)
            require(isinstance(source, list) and source and all(isinstance(row, dict) and
                set(row) == {'type', 'name', 'sql'} and row['type'] in {'table', 'index', 'trigger'} and
                isinstance(row['name'], str) and isinstance(row['sql'], str) for row in source),
                'Independent prelaunch schema authority is missing: ' + table)
            objects = [(row['type'], row['name'], row['sql']) for row in source]
            require(any(row[0] == 'table' and row[1] == table for row in objects), 'Independent table authority is missing: ' + table)
            current = c.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL AND type IN ('table','index','trigger')", (table,)).fetchall()
            require(sorted(current) == sorted(objects), 'Active schema differs from independent prelaunch authority: ' + table)
            ordered = sorted(objects, key=lambda row: ({'table': 0, 'index': 1, 'trigger': 2}[row[0]], row[1]))
            script = '\n'.join(row[2].rstrip(';') + ';' for row in ordered)
            matches = [row for row in schemas if row['source_table'] == table]
            require(len(matches) == 1 and matches[0]['schema_sql'] == script and
                matches[0]['schema_sha256'] == hashlib.sha256(script.encode()).hexdigest(),
                'Recoverable archive schema is missing or changed: ' + table)
            # Execute only after exact comparison with the independent original
            # topology. Table-only archives would silently lose UNIQUE indexes.
            with closing(sqlite3.connect(':memory:')) as restored:
                restored.executescript(script)
                topology = restored.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND type IN ('table','index','trigger')").fetchall()
                require(sorted(topology) == sorted(objects), 'Archived schema cannot reconstruct original indexes/triggers: ' + table)
        actual = set(c.execute('SELECT source_table,row_key,row_sha256 FROM posting_retirement_tombstones'))
        require(actual == set(expected_hashes), 'Retirement tombstones differ from verified archived rows')
    return {'verified': True, 'archived_rows': len(saved), 'copied_files': len(files),
        'archive_rows_sha256': hashlib.sha256(canonical(sorted(expected_hashes)).encode()).hexdigest()}
