#!/usr/bin/env python3
"""Verify completed offline IG simulation evidence against the packaged source.

This validates report completeness and exact code binding; it does not rerun the
simulations or turn synthetic geometry into real-browser/live-account evidence.
"""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def verify_report(report,root,*,required_runs=500):
    def require(condition,message):
        if not condition:raise ValueError(message)
    code=report.get('production_code',{});files=code.get('files',{})
    actual_app={str(p.relative_to(root)).replace('\\','/') for p in (root/'backend/app').rglob('*.py')}
    require(actual_app and actual_app.issubset(files),'Report omits backend source files')
    require({'scripts/simulate_instagram_500.py','scripts/ig500_geometry.cjs','scripts/verify_instagram_500_report.py'}.issubset(files),'Report omits harness/verifier digests')
    require(code.get('sha256')==digest(files),'Code manifest aggregate hash mismatch')
    for path,expected in files.items():
        file=(root/path).resolve();require(file.is_relative_to(root.resolve()),'Unsafe code path')
        require(file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest()==expected,f'Code differs from tested report: {path}')
    require(report.get('code_unchanged_during_campaign') is True,'Code changed during campaign')
    require(report.get('runs_requested')==required_runs and report.get('runs_completed')==required_runs,'Wrong campaign size')
    require(report.get('passed')==required_runs and report.get('failed')==0,'Campaign has failed cases')
    records=report.get('records',[]);require(len(records)==required_runs,'Missing or duplicate case records')
    ids=set();seeds=set()
    for row in records:
        run_id=row.get('run_id');require(run_id not in ids,'Duplicate run ID');ids.add(run_id)
        require(row.get('seed') not in seeds,'Duplicate deterministic seed');seeds.add(row.get('seed'))
        require(run_id.startswith('ig500-'),'Invalid run ID');index=int(run_id[6:])
        require(row['seed']==951000+index,'Seed does not match run ID')
        n=row.get('input_count');require(type(n) is int and n>=1000,'Case below1000 ground-truth followers')
        expected=digest(sorted(f'r{index:04d}_member_{i:04d}' for i in range(n)))
        for key in ('input_sha256','actual_sha256','extracted_sha256','restart_actual_sha256'):
            require(row.get(key)==expected,f'{run_id}: {key} does not match independently reconstructed ground truth')
        require(row.get('actual_count')==n and row.get('extracted_count')==n,f'{run_id}: count mismatch')
        require(row.get('status')=='passed' and row.get('task_status')=='completed',f'{run_id}: task not complete')
        for key in ('missing','extraneous','extracted_missing','extracted_extraneous'):
            require(row.get(key)==[],f'{run_id}: {key}')
        for key in ('duplicate_results','duplicate_profile_reads','leases','foreign_key_errors'):
            require(row.get(key)==0,f'{run_id}: {key}')
        require(row.get('review_count')==n and row.get('global_identity_count')==n+1,f'{run_id}: persistence/dedupe mismatch')
        q=row.get('queue',{});require(q.get('total')==n and q.get('recorded')==n and q.get('pending')==0 and q.get('deduped')==0,f'{run_id}: queue mismatch')
        cursor=(row.get('checkpoint') or {}).get('cursor',{})
        require(cursor.get('candidate_spool_complete') is True and cursor.get('candidate_spool_natural_end') is True,f'{run_id}: unproven end')
        require(row.get('code_sha256')==code['sha256'],f'{run_id}: code binding mismatch')
        require(type(row.get('elapsed_seconds')) in (int,float) and row['elapsed_seconds']>0,f'{run_id}: timing absent')
        scenario=['plain','delayed_repaint','resize','duplicate_links','pause_resume','source_gap'][index%6]
        require(row.get('scenario')==scenario,f'{run_id}: scenario mismatch')
        require(row.get('exact_profile_fields_verified')==n and row.get('split_completions')==1,f'{run_id}: profile/split evidence missing')
        require(row.get('children_created',0)>=row.get('children_requested',1),f'{run_id}: screening topology absent')
        if scenario=='delayed_repaint':require(row.get('delayed_reads',0)>0,f'{run_id}: no delayed repaint executed')
        if scenario=='source_gap':require(row.get('source_gap_events',0)>0 and row.get('recoveries',0)>0,f'{run_id}: no gap recovery executed')
        if scenario=='pause_resume':
            pause=row.get('pause_proof',{})
            require(pause.get('status')=='paused' and pause.get('checkpoint_present') is True and pause.get('queue',{}).get('total',0)>0,f'{run_id}: pause checkpoint absent')
        require(row.get('geometry_frames',0)>1 and row.get('scroll_moves',0)>1 and row.get('dom_reads',0)>1,f'{run_id}: collection not executed')
    require(ids=={f'ig500-{i:04d}' for i in range(required_runs)},'Case ID coverage is not consecutive from zero')
    return {'verified_runs':required_runs,'verified_ground_truth_identities':sum(r['input_count'] for r in records),'code_sha256':code['sha256'],
        'browser_runs_in_this_report':0,'verification':'offline production-DOM/reader/manager/SQLite'}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,default=Path('verification/ig500_results.json'))
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);a=p.parse_args()
    print(json.dumps(verify_report(json.loads(a.report.read_text()),a.root),sort_keys=True))
