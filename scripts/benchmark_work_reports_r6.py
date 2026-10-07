#!/usr/bin/env python3
"""Reproducible synthetic report benchmark; never opens a user's data directory.

Example: python scripts/benchmark_work_reports_r6.py --records 600000 1000000
Optional --legacy-module loads the preserved pre-fix work_reports.py for a
same-database before/after comparison. Output is machine-readable JSON.
"""
import argparse
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from contextlib import nullcontext

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.database import Database, SCHEMA
from app.service import CoreService
from app.work_reports import work_report

START, END = '2026-10-02T00:00:00+00:00', '2026-10-03T00:00:00+00:00'
OLD, NOW = '2026-06-15T08:30:00+00:00', '2026-10-02T08:30:00+00:00'

def timed(method, db, owner, **kwargs):
    start = time.perf_counter()
    value = method(db, owner, START, END, platform='instagram', **kwargs)
    elapsed = time.perf_counter() - start
    return value, {'seconds': round(elapsed, 6), 'json_bytes': len(json.dumps(value).encode()),
                   'totals': value['totals'], 'rows': len(value.get('rows', []))}

def assert_legacy_report_parity(before, detail):
    """Preserve nonposting metrics and rows across the explicit scope removal."""
    retired = {'posting', 'confirmed_posting'}
    assert not retired.intersection(detail['totals']), detail['totals']
    assert all(not retired.intersection(row) for row in detail['rows']), detail['rows']
    old_totals = {key: value for key, value in before['totals'].items() if key not in retired}
    assert detail['totals'] == old_totals, (detail['totals'], old_totals)
    old_rows = [{key: value for key, value in row.items() if key not in retired} for row in before['rows']]
    key = lambda row: (row['profile_id'], row['window_name'], row['username'], row['instagram_user_id'])
    assert sorted(detail['rows'], key=key) == sorted(old_rows, key=key)


def run(records, legacy_path, current_records=None, keep_fixture=None):
    if keep_fixture is not None:
        keep_fixture = Path(keep_fixture) / str(records)
        keep_fixture.mkdir(parents=True, exist_ok=False)
    storage = nullcontext(str(keep_fixture)) if keep_fixture is not None else tempfile.TemporaryDirectory(prefix='juxin-r6-report-benchmark-')
    with storage as tmp:
        db = Database(Path(tmp) / 'synthetic.db'); db.initialize()
        owner = CoreService(db).register_user('synthetic-benchmark', 'synthetic benchmark password')['id']
        with db.write() as c:
            indexes = [(r['name'], r['sql']) for r in c.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND (name LIKE '%_report_period' OR name IN ('idx_results_report_identity','idx_candidates_report_identity','idx_exclusions_report_identity') OR name='idx_candidates_report_reviewed' OR name='idx_split_history_report_identity')")]
            for name, _ in indexes: c.execute(f'DROP INDEX {name}')
            c.execute("INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) VALUES('task',?,'benchmark','queued','[\"followers\"]','{}',?,?)", (owner, OLD, OLD))
            c.execute("INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,created_at,updated_at) VALUES('target','task','source','source',1,'pending',?,?)", (OLD, OLD))
        current = min(records, current_records if current_records is not None else min(2000, records // 10))
        seeded_at = time.perf_counter()
        for begin in range(0, records, 5000):
            accounts, candidates, exclusions = [], [], []
            for i in range(begin, min(begin + 5000, records)):
                ident, when = f'account-{i}', NOW if i < current else OLD
                profile = json.dumps({'executor': {'profile_id': f'window-{i % 24}', 'username': f'actor.{i % 24}', 'window_name': f'Window {i % 24}', 'instagram_user_id': str(i % 24)}, 'biography': 'synthetic profile text ' * 32})
                accounts.append((ident, ident, ident, when, when))
                if i % 2:
                    candidates.append((ident, owner, ident, profile, when, when))
                else:
                    exclusions.append((ident, ident, owner, ident, profile, when))
            with db.write() as c:
                c.executemany('INSERT INTO instagram_accounts(id,current_username_norm,current_username_display,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)', accounts)
                c.executemany("INSERT INTO workbench_candidates(id,owner_user_id,account_id,visibility,profile_json,created_at,updated_at) VALUES(?,?,?,'private',?,?,?)", candidates)
                c.executemany("INSERT INTO workbench_collection_exclusions(id,account_id,owner_user_id,username_display,reason_code,reason,profile_snapshot_json,excluded_at) VALUES(?,?,?,?,'not_us','synthetic fixture',?,?)", exclusions)
        # Later task copies of old identities must not inflate the selected day;
        # same-period duplicate task/review rows also remain exactly one account.
        with db.write() as c:
            c.executemany("INSERT INTO task_results(id,task_id,target_id,account_id,sources_json,visibility,profile_json,screening_json,created_at,updated_at) SELECT ?,'task','target',?,'[]','private',COALESCE((SELECT profile_json FROM workbench_candidates WHERE account_id=?),(SELECT profile_snapshot_json FROM workbench_collection_exclusions WHERE account_id=?)),'{}',?,?",
                          [(f'duplicate-{i}', f'account-{i}', f'account-{i}', f'account-{i}', NOW, NOW) for i in range(min(records, current * 2))])
            splits = max(100, records // 20)
            c.executemany("INSERT INTO split_completed_targets(target_id,owner_user_id,username_norm,username_display,source_task_id,source_window_id,completed_at) VALUES(?,?,?,?,?,'split-window',?)",
                          [(f'split-{i}', owner, f'source.{i}', f'source.{i}', 'task', NOW if i < 20 else OLD) for i in range(splits)])
            c.executemany("INSERT INTO split_candidate_history(id,owner_user_id,username_norm,username_display,source_target_id,source_status,source_window_id,completed_at,created_at,updated_at) VALUES(?,?,?,?,?,'completed','duplicate-window',?,?,?)",
                          [(f'split-history-{i}', owner, f'source.{i}', f'source.{i}', f'split-{i}', NOW, NOW, NOW) for i in range(100)])
        result = {'identities': records, 'source_records': records + min(records, current * 2), 'selected_period_identities': current, 'split_facts': splits, 'profile_payload_bytes_approx': 900,
                  'seed_seconds': round(time.perf_counter() - seeded_at, 3), 'retained_fixture': str(db.path) if keep_fixture else None}
        if legacy_path:
            spec = importlib.util.spec_from_file_location('app.legacy_benchmark_reports', legacy_path)
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            before, result['before_first'] = timed(module.work_report, db, owner)
            _, result['before_repeat'] = timed(module.work_report, db, owner)
        indexed_at = time.perf_counter()
        with db.write() as c:
            for _, sql in indexes: c.execute(sql)
        result['additive_index_seconds'] = round(time.perf_counter() - indexed_at, 3)
        after, result['summary_first'] = timed(work_report, db, owner, summary_only=True)
        _, result['summary_repeat'] = timed(work_report, db, owner, summary_only=True)
        detail, result['csv_detail_first'] = timed(work_report, db, owner)
        _, result['csv_detail_repeat'] = timed(work_report, db, owner)
        assert after['totals']['collection'] == current and after['totals']['split'] == 20, after
        assert {k: detail['totals'][k] for k in after['totals']} == after['totals']
        assert not {'posting', 'confirmed_posting'}.intersection(after['totals'])
        assert not {'posting', 'confirmed_posting'}.intersection(detail['totals'])
        assert all(not {'posting', 'confirmed_posting'}.intersection(row) for row in detail['rows'])
        result['posting_metrics_removed'] = True
        result['legacy_metric_and_row_parity'] = None
        if legacy_path:
            assert_legacy_report_parity(before, detail)
            result['legacy_metric_and_row_parity'] = True
        with db.read() as c:
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            result['database_bytes'] = db.path.stat().st_size
            result['integrity'] = c.execute('PRAGMA quick_check').fetchone()[0]
        return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', nargs='+', type=int, default=[600000, 1000000])
    parser.add_argument('--legacy-module', type=Path)
    parser.add_argument('--current-records', type=int, help='selected-day identities; default 2000 for large histories')
    parser.add_argument('--keep-fixture', type=Path, help='retain each synthetic database under this NEW directory for profiling')
    args = parser.parse_args()
    for count in args.records:
        if count < 100: parser.error('use at least 100 synthetic identities')
        print(json.dumps(run(count, args.legacy_module, args.current_records, args.keep_fixture), ensure_ascii=False), flush=True)
