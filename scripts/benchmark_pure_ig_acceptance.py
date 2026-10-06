"""Offline real-route / real callback acceptance across growing synthetic history.

Each tier runs in a fresh process and an isolated, disposable database. No live
accounts, network requests, shared fixtures, or production monkey-patches.
SQL VM samples are separate from latency samples; no OS-cold claim is made.
"""
from __future__ import annotations
import argparse, ast, asyncio, collections, hashlib, json, math, os, resource
import sqlite3, statistics, sys, tempfile, textwrap, time
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'scripts')]
from app.async_cleanup import finish_owned
from app.config import Settings
from app.database import Database
from app.execution_manager import ExecutionManager
from app.main import create_app
from app.service import CoreService
from snapshot_scale_fixture import seed_snapshot_scale

class MeasuredDatabase(Database):
    def __init__(self,path):
        super().__init__(path); self.recording=False; self.vm_interval=0; self.reset()
    def reset(self,vm_interval=0):
        self.recording=True; self.vm_interval=vm_interval
        self.metrics=collections.Counter(); self.sql=[]
    def stop(self):
        self.recording=False; return dict(self.metrics)
    def _connect(self):
        return self._measure_connection(super()._connect())
    def _connect_discovery(self):
        return self._measure_connection(super()._connect_discovery())
    def _measure_connection(self,c):
        if self.recording:self.metrics['connections']+=1
        def trace(statement):
            if not self.recording:return
            self.metrics['sql_statements_with_trigger_expansions']+=1
            head=statement.lstrip().split(None,1)[0].upper()
            self.metrics[head]+=1
            if self.vm_interval and head in {'SELECT','WITH'}:self.sql.append(statement)
        c.set_trace_callback(trace)
        if self.vm_interval:
            interval=self.vm_interval
            def progress():
                if self.recording:self.metrics['vm_steps']+=interval
                return 0
            c.set_progress_handler(progress,interval)
        return c
class Inventory:
    def list_all_windows(self):return {'windows':[],'connection':{'connected':True,'state':'ready'}}
def percentile(xs,q):
    xs=sorted(xs); return xs[max(0,math.ceil(q*len(xs))-1)]
def summary(xs):
    return {'count':len(xs),'p50_ms':1000*statistics.median(xs),'p95_ms':1000*percentile(xs,.95),'max_ms':1000*max(xs)}
def io_counters():
    return {line.split(":")[0]:int(line.split(":")[1]) for line in Path("/proc/self/io").read_text().splitlines()}
def rss():
    data=Path('/proc/self/status').read_text()
    return int(next(line.split()[1] for line in data.splitlines() if line.startswith('VmRSS:')))*1024

def callback(service,fixture):
    """Compile the current production nested callback unchanged, with real writes."""
    text=(ROOT/'backend/app/execution_manager.py').read_text()
    outer=next(n for n in ast.walk(ast.parse(text)) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_execute_candidate_spooled_mode_body')
    node=next(n for n in ast.walk(outer) if isinstance(n,ast.AsyncFunctionDef) and n.name=='candidate_sink')
    body=textwrap.dedent('\n'.join(text.splitlines()[node.lineno-1:node.end_lineno]))
    lock=asyncio.Lock(); control=SimpleNamespace(owner_user_id=fixture['owner_id'],task_id=fixture['task_id'],pause_event=asyncio.Event())
    control.pause_event.set()
    manager=SimpleNamespace(service=service,_await_durable_thread_call=ExecutionManager._await_durable_thread_call,_record_profile_progress=lambda *a,**kw:None)
    async def controls():pass # browser control gates only; all persistence remains real
    async def write(current,*,stage):
        await ExecutionManager._save_candidate_progress_checkpoint(manager,control,fixture['target_id'],'followers',current,discovery_complete=False,stage=stage)
    ns={'asyncio':asyncio,'Any':object,'self':manager,'control':control,'target_id':fixture['target_id'],'mode':'followers','direct_handoff':True,'batch_discovery':True,'candidate_available':asyncio.Event(),'checkpoint_lock':lock,'check_source_controls':controls,'write_progress_unlocked':write,'finish_owned':finish_owned}
    exec('def factory():\n    last_checkpoint_total=0\n'+textwrap.indent(body,'    ')+'\n    return candidate_sink',ns)
    return ns['factory'](),hashlib.sha256(body.encode()).hexdigest()

def identity_benchmark(db,svc,f,rounds):
    result={}; args=(f['owner_id'],f['task_id'],f['target_id'],'followers')
    actions={
      'existing_terminal_duplicate':lambda i:svc.should_skip_relationship_hover(f['owner_id'],f'scale_u{i:07}',source='followers',source_target=f['target_id']),
      'unseen_duplicate_probe':lambda i:svc.should_skip_relationship_hover(f['owner_id'],f'not_in_ledger_{i}',source='followers',source_target=f['target_id']),
      'durable_recognition':lambda i:svc.discover_task_mode_candidate(*args,f'fresh_recognition_{i}'),
      'repeated_terminal_recognition':lambda i:svc.discover_task_mode_candidate(*args,f'scale_u{i:07}'),
    }
    for name,action in actions.items():
        db.reset(); times=[]
        for i in range(rounds):
            before=time.perf_counter(); value=action(i); times.append(time.perf_counter()-before)
            if name=='existing_terminal_duplicate':assert value is True
            elif name=='unseen_duplicate_probe':assert value is False
            elif name=='durable_recognition':assert not value['duplicate']
            else:assert value['duplicate']
        metrics=db.stop(); db.reset(1); action(rounds+10); vm=db.stop()
        result[name]={**summary(times),'work':metrics,'single_call_vm_steps':vm.get('vm_steps',0)}
    return result

async def measure(db,f,rounds):
    import httpx
    settings=Settings(startup_token='synthetic-acceptance-token',database_path=db.path,data_dir=db.path.parent)
    app=create_app(settings,database=db,bitbrowser=Inventory());svc=app.state.service
    token=svc.login('snapshot-benchmark','synthetic-benchmark-password')['token']
    headers={'X-Startup-Token':settings.startup_token,'Authorization':'Bearer '+token}
    routes=[];rss_before=rss()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline',headers=headers) as client:
        for scope in ['instagram',None]:
            url='/api/workbench/snapshot?limit=2000&history_limit=2000&compact=1'+('&platform='+scope if scope else '')
            records=[];db.reset();io_before=io_counters()
            for i in range(rounds):
                begin=time.perf_counter();cpu=time.process_time();response=await client.get(url);elapsed=time.perf_counter()-begin;cpu_elapsed=time.process_time()-cpu
                response.raise_for_status();p=response.json()
                for key in ['total_collected','total_public','total_private','total_split']:assert p['counts'][key]==f[key],(key,p['counts'])
                assert p['global_dedupe_count']==f['global_dedupe_count']
                records.append({'seconds':elapsed,'cpu_seconds':cpu_elapsed,'bytes':len(response.content),'rss_bytes':rss()})
            metrics=db.stop();io_after=io_counters();db.reset(1000);response=await client.get(url);response.raise_for_status();vm=db.stop()
            statements=list(dict.fromkeys(db.sql));plans=[]
            with db.read() as c:
                for sql in statements:
                    try:plan=[list(row) for row in c.execute('EXPLAIN QUERY PLAN '+sql)]
                    except sqlite3.Error:continue
                    plans.append({'sql':sql,'plan':plan})
            routes.append({'scope':scope or 'pure_ig_default','url':url,**summary([r['seconds'] for r in records]),'requests':rounds,'response_bytes':records[-1]['bytes'],'work':metrics,'process_io_delta':{k:io_after[k]-io_before[k] for k in io_before},'vm_sample':vm,'samples':records,'query_plans':plans})
        # A mutation must be reflected in the very next snapshot, including
        # result/review progress. No timer or aggregate-cache TTL is allowed.
        owner,task,target=f['owner_id'],f['task_id'],f['target_id']
        name='fresh_snapshot_result'
        claim=svc.claim_workbench_identity(owner,username=name,source='followers',source_target=target)
        profile={'username':name,'visibility':'public','posts_count':3}
        svc.record_result(owner,task,target,username=name,instagram_user_id=None,source_mode='followers',visibility='public',profile=profile,screening={},qualified=True,dedupe_claim_id=claim['claim_id'])
        candidate=svc.create_workbench_candidate(owner,claim_id=claim['claim_id'],username=name,visibility='public',profile=profile,screening={},review_cache={},source_mode='followers',source_target=target)
        svc.decide_workbench_candidate(owner,candidate_id=candidate['id'],decision='approved')
        response=await client.get('/api/workbench/snapshot?limit=2000&history_limit=2000&compact=1&platform=instagram')
        response.raise_for_status();fresh=response.json()
        assert fresh['counts']['total_collected']==f['total_collected']+1
        assert fresh['counts']['total_public']==f['total_public']+1
        assert fresh['global_dedupe_count']==f['global_dedupe_count']+1
        assert any(row['id']==candidate['id'] for row in fresh['approved']['public'])
        fresh_target=next(t for task_row in fresh['tasks'] for t in task_row['targets'] if t['id']==target)
        assert fresh_target['mode_progress']['followers']['qualified_for_review']==f['qualified_for_review']+1
        freshness={'next_snapshot_exact_result_review_counts':True,'latest_approved_row_visible':True}
    identities=identity_benchmark(db,svc,f,30)
    cb,cb_hash=callback(svc,f);durations=[];db.reset();results=[]
    for batch in range(10):
        names=[f'callback_{batch}_{i}' for i in range(100)]
        begin=time.perf_counter();result=await cb(names);durations.append(time.perf_counter()-begin);results.append(result)
    callback_work=db.stop()
    # New service/database object must see each committed reservation immediately.
    reopened=CoreService(Database(db.path))
    resumed=reopened.claim_workbench_identity(f['owner_id'],username='callback_9_99',source='followers',source_target=f['target_id'],allow_owned_resume=True)
    assert resumed['resumed'] and not resumed['duplicate'],resumed
    assert reopened.should_skip_relationship_hover(f['owner_id'],'scale_u0000000',source='followers',source_target=f['target_id'])
    await app.state.snapshot_inventory.close()
    return {'routes':routes,'freshness':freshness,'identity_paths':identities,'production_callback':{**summary(durations),'identities_per_second':1000/sum(durations),'work':callback_work,'batch_size':100,'batches':10,'callback_sha256':cb_hash,'latest_total':results[-1]['total'],'reopened_unfinished_claim_resumable':True},'rss_before_routes_bytes':rss_before,'rss_after_all_bytes':rss(),'max_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,'exact_durable_counts_verified':True}

def main():
    p=argparse.ArgumentParser();p.add_argument('--identities',type=int,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--rounds',type=int,default=7);p.add_argument('--temporary-root',type=Path);p.add_argument('--profile-bytes',type=int,default=200);args=p.parse_args()
    hashes={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((ROOT/'backend/app').glob('*.py'))}
    if args.temporary_root:args.temporary_root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pure-ig-scale-',dir=args.temporary_root) as tmp:
        db=MeasuredDatabase(Path(tmp)/'synthetic.sqlite3');db.recording=False;db.initialize();svc=CoreService(db)
        owner=svc.register_user('snapshot-benchmark','synthetic-benchmark-password')['id']
        fixture=seed_snapshot_scale(db.path,owner,result_count=max(1,round(args.identities*441552/602831)),identity_count=args.identities,split_count=min(1272,args.identities//50),profile_bytes=args.profile_bytes)
        with db.read() as c:c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        report=asyncio.run(measure(db,fixture,args.rounds));report.update(fixture=fixture,database_bytes=db.path.stat().st_size,source_sha256=hashes,profile_padding_bytes=args.profile_bytes,temporary_root=str(args.temporary_root or tempfile.gettempdir()),cache_note='Fresh SQLite connections with natural OS cache; timing samples exclude VM instrumentation. HTTP calls are in-process ASGI, no live requests.')
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'identities':args.identities,'routes':[{k:r[k] for k in ('scope','p50_ms','p95_ms','response_bytes','vm_sample')} for r in report['routes']],'identity_paths':report['identity_paths'],'callback':report['production_callback'],'max_rss_bytes':report['max_rss_bytes']}),flush=True)
if __name__=='__main__':main()
