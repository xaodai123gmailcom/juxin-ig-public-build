#!/usr/bin/env python3
"""Reproducible offline production IG collection/SQLite campaign.

This is not a live-account or 500-real-browser claim. Each independent case runs
production DOM scripts in Node geometry, production PlaywrightWorker collection,
ExecutionManager scheduling/screening, durable candidate batches and real SQLite.
Only browser navigation/profile facts, geometry, and logical waiting are fixtures.
"""
from __future__ import annotations
import argparse, asyncio, hashlib, json, os, random, shutil, subprocess, sys, time, traceback, tempfile, re, threading, concurrent.futures, multiprocessing
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.collection_surface import RELATION_ROWS_SCRIPT, relation_scroll_script
from app.database import Database
from app.execution_manager import ExecutionManager
from app.playwright_worker import PlaywrightWorker, WorkerExecutionError
from app.service import CoreService

def source_relative_key(path,root):
    """Stable manifest keys for POSIX and Windows production runs."""
    return path.relative_to(root).as_posix()

PRODUCTION_FILES=sorted(source_relative_key(p,ROOT) for p in (ROOT/'backend/app').rglob('*.py'))+['scripts/simulate_instagram_500.py','scripts/ig500_geometry.cjs','scripts/verify_instagram_500_report.py']

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def code_digest():
    files={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in PRODUCTION_FILES if (ROOT/p).is_file()}
    return {'files':files,'sha256':digest(files)}

def storage_kind(path):
    try:
        return subprocess.run(['stat','-f','-c','%T',str(path)],capture_output=True,text=True,check=True,timeout=5).stdout.strip()
    except (OSError,subprocess.SubprocessError):return 'unverified'

def case_spec(index,count):
    seed=951000+index;r=random.Random(seed)
    names=[f'r{index:04d}_member_{i:04d}' for i in range(count)]
    r.shuffle(names)
    row_height=r.choice([36,40,48,60]);client=r.choice([280,360,480,600])
    clip=r.choice([180,220,260,320]);clip=min(clip,client)
    scenario=['plain','delayed_repaint','resize','duplicate_links','pause_resume','source_gap'][index%6]
    options={'scale':r.choice([.75,1,1.25,1.5]),'row_height':row_height,'client':client,'clip':clip,
        'header':r.choice([0,24,40]),'overflow':'auto','virtual':True,'link_height':r.choice([18,row_height]),
        'link_offset':0,'duplicate_links':scenario=='duplicate_links','recommendations':True,
        'resize_at':17 if scenario=='resize' else -1,'resize_to':min(client,clip+80)}
    return {'run_id':f'ig500-{index:04d}','index':index,'seed':seed,'count':count,'names':names,
        'scenario':scenario,'children':1+index%3,'geometry':options}

def frames_for(spec):
    command=['node',str(Path(__file__).with_name('ig500_geometry.cjs'))]
    p=subprocess.run(command,input=json.dumps({'names':spec['names'],'options':spec['geometry'],
        'projection':RELATION_ROWS_SCRIPT,'advance':relation_scroll_script('advance',[]),
        'measure':relation_scroll_script('measure')}),text=True,capture_output=True,timeout=120,check=True)
    value=json.loads(p.stdout)
    # Ground truth is generated before DOM construction, not inferred from frames.
    actual={p.strip('/') for p in value['observed']}
    if actual!=set(spec['names']):raise AssertionError(f'Production DOM mismatch: missing={len(set(spec["names"])-actual)}, extra={sorted(actual-set(spec["names"]))[:10]}')
    return value['frames']

class Browser:
    def list_all_windows(self):return {'windows':[{'id':'simulation-window','name':'isolated fixture','is_open':False}],'stale':False}
    def close_profile(self,*_):return {'closed':True}

class Visible:
    @property
    def first(self):return self
    async def count(self):return 0
    async def is_visible(self):return False
    async def inner_text(self,**_):return 'Followers'
    def locator(self,*_):return self

class Surface(Visible):
    def __init__(self,state):self.state=state;self.index=0;self.pending=0;self.previous=0;self.reads=0;self.moves=0;self.gap_used=False
    async def evaluate(self,script):
        self.reads+=1
        if 'relation-action: reset' in script:self.index=0;self.pending=0;return {'valid':True,'top':0}
        if 'relation-action: measure' in script:return self.state.frames[self.index]['measurement']
        if 'relation-action: advance' in script:
            frame=self.state.frames[self.index];movement=frame.get('movement',dict(frame['measurement'],moved=False))
            match=re.search(r'const acknowledgedNames = new Set\((\[.*?\])\);',script)
            if not match or set(json.loads(match[1]))!={r['username'] for r in frame['positioned_rows']}:
                raise AssertionError('Production collector did not acknowledge the exact observed positioned frame')
            if self.pending:raise AssertionError('Collector advanced before delayed frame acknowledgement')
            admitted={r['username'] for r in frame['positioned_rows'] if not r.get('recommended')}
            if admitted:
                with self.state.db.read() as con:
                    persisted={r[0] for r in con.execute('SELECT username_norm FROM task_mode_candidates WHERE target_id=? AND username_norm IN ('+','.join('?' for _ in admitted)+')',(self.state.target,*sorted(admitted)))}
                if not admitted.issubset(persisted):raise AssertionError('Scroll attempted before visible identities were durably queued')
            if self.state.spec['scenario']=='source_gap' and not self.gap_used and self.index>=len(self.state.frames)//3:
                self.gap_used=True;self.state.gap_events+=1
                with self.state.db.read() as con:self.state.gap_pending.append(con.execute("SELECT COUNT(*) FROM task_mode_candidates WHERE target_id=? AND state='pending'",(self.state.target,)).fetchone()[0])
                raise WorkerExecutionError('Synthetic source-only gap',reason='instagram_followers_list_incomplete',pause_required=True)
            if movement.get('moved'):
                self.previous=self.index;self.index+=1;self.moves+=1
                self.pending=(1+self.state.spec['seed']%3 if self.state.spec['scenario']=='delayed_repaint' else 0)
            return movement
        if script==RELATION_ROWS_SCRIPT:
            index=self.index
            if self.pending:self.pending-=1;index=self.previous;self.state.delayed_reads+=1
            f=self.state.frames[index]
            return {k:f[k] for k in ('hrefs','positioned_rows','recommendations_reached','diagnostics')}
        raise AssertionError('Unknown production DOM operation')

class State:
    def __init__(self,spec,frames):
        self.spec=spec;self.frames=frames;self.surface=Surface(self);self.profile_reads=[];self.activity_reads=[];self.child_ids=[]
        self.closed=[];self.delayed_reads=0;self.gap_events=0;self.gap_pending=[];self.recoveries=0;self.error_events=[]
        self.pause_ready=asyncio.Event();self.pause_release=asyncio.Event();self.pause_done=False
        self.active_profiles=0;self.max_active_profiles=0;self.source_active=False;self.overlap=False

class SimulatedWorker(PlaywrightWorker):
    supports_avatar_image_capture=False
    def __init__(self,browser,state,slot=0):
        super().__init__(browser);self.state=state;self.slot=slot
        self.logical_seconds=0.0
        self.collection_poll_interval_seconds=0
        self.collection_initial_idle_rounds=2;self.collection_settled_idle_rounds=2
        self.collection_loading_grace_seconds=20.0
        self.page=SimpleNamespace(url='https://www.instagram.com/source/',is_closed=lambda:False)
    def _collection_monotonic(self):
        # Only the documented relation-loop clock seam is accelerated. Every
        # operation still yields and production controls/writes run unchanged.
        self.logical_seconds+=.01;return self.logical_seconds
    async def connect(self,profile_id,**_):self.profile_id=profile_id
    async def disconnect(self):self.state.closed.append(self.slot)
    async def _guard(self):return
    async def _ensure_window_surface_stable(self):return
    async def _navigate_profile(self,target):self.page.url=f'https://www.instagram.com/{target}/';return target
    async def _visible_relation_count(self,*_):return self.state.spec['count']
    async def _source_profile_metrics(self,*_,**__):return {'followers':self.state.spec['count'],'following':100,'posts':25}
    async def _open_relation_surface(self,*_):return self.state.surface
    async def _has_visible_relation_loading_indicator(self,*_):return False
    async def _relation_surface_failure(self,*_):return None
    async def _replace_stuck_page_once(self,*args):
        self.state.recoveries+=1
        error=next((arg for arg in args if isinstance(arg,WorkerExecutionError)),None)
        self.state.error_events.append({'frame_index':self.state.surface.index,'reason':getattr(error,'code',None),
            'details':dict(getattr(error,'details',{}))})
        self.state.error_events=self.state.error_events[-12:]
    async def _finish_page_recovery(self,**_):return
    async def create_parallel_screening_worker(self):
        slot=len(self.state.child_ids)+1;self.state.child_ids.append(slot)
        child=SimulatedWorker(self.bitbrowser,self.state,slot);child.profile_id=self.profile_id
        return child
    def release_parallel_screening_worker(self,*_):return False
    async def read_visible_profile(self,username,**kwargs):
        self.state.active_profiles+=1;self.state.max_active_profiles=max(self.state.max_active_profiles,self.state.active_profiles)
        self.state.overlap|=self.state.surface.index<len(self.state.frames)-1
        try:
            (self.state.activity_reads if kwargs.get('include_activity') else self.state.profile_reads).append(username)
            if self.state.spec['scenario']=='pause_resume' and not kwargs.get('include_activity') and not self.state.pause_done and len(self.state.profile_reads)>40:
                self.state.pause_done=True;self.state.pause_ready.set();await self.state.pause_release.wait()
            await asyncio.sleep(0)
            n=int(username.rsplit('_',1)[-1])
            return {'username':username,'instagram_user_id':str(9000000000+self.state.spec['index']*10000+n),
                'display_name':f'Fixture {n}','visibility':'private' if n%7==0 else 'public',
                'followers':1200+n%11,'following':100+n%29,'posts':20+n%13,
                'activity_days':1,'is_verified':False}
        finally:self.state.active_profiles-=1
    async def capture_visible_review_snapshot(self,username,**_):
        # Browser/image seam, like read_visible_profile above. The synthetic
        # profile has no pixels; do not run real navigation retry sleeps against
        # a SimpleNamespace page. Production recording/review gates still run.
        return {'review_cache':{},'avatar_capture_reason':'synthetic_profile_without_image'}
    async def read_visible_account_location(self,*_):return 'United States'
    async def read_visible_profile_recovery_evidence(self,*_,**__):return {}

async def run_case(spec,directory,template,owner,timeout):
    started=time.perf_counter();dbpath=directory/(spec['run_id']+'.sqlite3');shutil.copyfile(template,dbpath)
    db=Database(dbpath);service=CoreService(db);browser=Browser();manager=None;state=None
    service_times={};timing_lock=threading.Lock()
    for name in [n for n in dir(service) if not n.startswith('_')]:
        fn=getattr(service,name,None)
        if not callable(fn):continue
        def timed(*a,__fn=fn,__name=name,**kw):
            t=time.perf_counter()
            try:return __fn(*a,**kw)
            finally:
                with timing_lock:
                    item=service_times.setdefault(__name,{'calls':0,'seconds':0});item['calls']+=1;item['seconds']+=time.perf_counter()-t
        setattr(service,name,timed)
    result={'run_id':spec['run_id'],'seed':spec['seed'],'scenario':spec['scenario'],'input_count':spec['count'],
        'input_sha256':digest(sorted(spec['names'])),'input_geometry':spec['geometry'],'children_requested':spec['children'],
        'verification':'production-js-offline-geometry + production-worker-manager-SQLite','status':'error'}
    try:
        t=time.perf_counter();frames=await asyncio.to_thread(frames_for,spec);result['geometry_seconds']=round(time.perf_counter()-t,4)
        state=State(spec,frames)
        task=service.create_task(owner,name=spec['run_id'],modes=['followers'],targets=[f'source_{spec["index"]}'],window_ids=['simulation-window'],
            settings={'platform':'instagram','parallel_screening_workers':spec['children'],'discard_count_limits_enabled':False,
                'exclude_public_zero_posts':False,'local_person_recognition':False,'location_enabled':False})
        target=task['targets'][0]['id'];state.db=db;state.target=target
        manager=ExecutionManager(service,browser,worker_factory=lambda b:SimulatedWorker(b,state),lease_heartbeat_interval_seconds=1)
        await manager.start(owner,task['id'])
        if spec['scenario']=='pause_resume':
            await asyncio.wait_for(state.pause_ready.wait(),timeout)
            await manager.pause(owner,task['id']);before=service.task_mode_candidate_stats(owner,task['id'],target,'followers')
            checkpoint=service.get_checkpoint(owner,task['id'],target,'followers')
            result['pause_proof']={'queue':before,'checkpoint_present':bool(checkpoint),'status':service.get_task(owner,task['id'])['status']}
            await manager.resume(owner,task['id']);state.pause_release.set()
        await asyncio.wait_for(manager.wait(task['id']),timeout)
        final=service.get_task(owner,task['id']);rows=service.list_results(owner,task['id']);actual=[r['username'] for r in rows]
        expected=set(spec['names']);missing=expected-set(actual);extra=set(actual)-expected
        stats=service.task_mode_candidate_stats(owner,task['id'],target,'followers');checkpoint=service.get_checkpoint(owner,task['id'],target,'followers')
        with db.read() as con:
            reviews=con.execute('SELECT COUNT(*) FROM workbench_candidates WHERE owner_user_id=?',(owner,)).fetchone()[0]
            leases=con.execute('SELECT COUNT(*) FROM browser_operation_leases').fetchone()[0]
            fk=list(con.execute('PRAGMA foreign_key_check'))
            dedupe=con.execute('SELECT COUNT(*) FROM global_seen').fetchone()[0]
            queued={r[0] for r in con.execute('SELECT username_norm FROM task_mode_candidates WHERE target_id=?',(target,))}
            split_completions=con.execute('SELECT COUNT(*) FROM split_completed_targets WHERE target_id=?',(target,)).fetchone()[0]
        result.update(actual_count=len(actual),actual_sha256=digest(sorted(actual)),missing=sorted(missing),extraneous=sorted(extra),
            extracted_count=len(queued),extracted_sha256=digest(sorted(queued)),extracted_missing=sorted(expected-queued),extracted_extraneous=sorted(queued-expected),
            duplicate_results=len(actual)-len(set(actual)),duplicate_profile_reads=len(state.profile_reads)-len(set(state.profile_reads)),
            queue=stats,task_status=final['status'],checkpoint=checkpoint,review_count=reviews,global_identity_count=dedupe,
            leases=leases,foreign_key_errors=len(fk),profile_reads=len(state.profile_reads),geometry_frames=len(frames),
            scroll_moves=state.surface.moves,dom_reads=state.surface.reads,delayed_reads=state.delayed_reads,
            source_gap_events=state.gap_events,source_gap_pending=state.gap_pending,recoveries=state.recoveries,split_completions=split_completions,children_created=len(state.child_ids),
            source_screening_overlap=state.overlap,max_active_profile_reads=state.max_active_profiles)
        assert queued==expected,(len(expected-queued),len(queued-expected))
        assert not missing and not extra,(len(missing),len(extra))
        assert len(actual)==len(expected) and result['duplicate_results']==0
        assert stats['pending']==0 and stats['recorded']==len(expected),stats
        assert final['status']=='completed',final
        assert reviews==len(expected),(reviews,len(expected))
        assert not leases and not fk
        assert dedupe==len(expected)+1,(dedupe,len(expected)+1)
        assert split_completions==1,'Source split completion not durably archived'
        assert len(state.profile_reads)==len(expected),'Durable dedupe reopened a saved profile'
        assert manager._candidate_spool_complete(checkpoint,require_natural_end=True),checkpoint
        reopened=CoreService(Database(dbpath))
        persisted=[r['username'] for r in reopened.list_results(owner,task['id'])]
        assert set(persisted)==expected and len(persisted)==len(expected),'Independent connection restart changed result identities'
        result['restart_actual_sha256']=digest(sorted(persisted))
        for row in rows:
            n=int(row['username'].rsplit('_',1)[-1]);profile=row['profile']
            assert (profile.get('followers'),profile.get('following'),profile.get('posts'))==(1200+n%11,100+n%29,20+n%13),'Persisted profile fields changed'
        result['exact_profile_fields_verified']=len(rows)
        result['activity_reads']=len(state.activity_reads)
        result['status']='passed'
    except Exception as e:result.update(error=f'{type(e).__name__}: {e}',traceback=traceback.format_exc())
    finally:
        if state:state.pause_release.set()
        if manager:await manager.shutdown()
        result['elapsed_seconds']=round(time.perf_counter()-started,4)
        result['database_bytes']=dbpath.stat().st_size
        result['service_timings']=service_times
        if state:
            result['source_error_events']=state.error_events
            result['final_frame_index']=state.surface.index
            result['final_frame_measurement']=state.frames[state.surface.index]['measurement']
        if result['status']!='passed' and 'task' in locals():
            try:
                persisted=service.list_results(owner,task['id']);actual=[r['username'] for r in persisted];expected=set(spec['names'])
                result.update(actual_count=len(actual),actual_sha256=digest(sorted(actual)),missing=sorted(expected-set(actual)),extraneous=sorted(set(actual)-expected),
                    queue=service.task_mode_candidate_stats(owner,task['id'],target,'followers'),checkpoint=service.get_checkpoint(owner,task['id'],target,'followers'),
                    task_status=service.get_task(owner,task['id'])['status'])
            except Exception as failure_snapshot_error:result['failure_snapshot_error']=str(failure_snapshot_error)
    return result

def run_case_process(spec,storage,template,owner,timeout):
    return asyncio.run(run_case(spec,Path(storage),Path(template),owner,timeout))

async def main_async(args):
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    storage=Path(args.database_directory or out);storage.mkdir(parents=True,exist_ok=True)
    template=storage/'template.sqlite3';db=Database(template);db.initialize();svc=CoreService(db)
    owner=svc.register_user('ig500-synthetic','offline simulation password')['id']
    with db.read() as con:con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    start=time.perf_counter();code=code_digest();sem=asyncio.Semaphore(args.parallel);records=[]
    pool=concurrent.futures.ProcessPoolExecutor(max_workers=args.parallel,mp_context=multiprocessing.get_context('spawn')) if args.processes else None
    async def one(i):
        async with sem:
            spec=case_spec(i,args.followers)
            if pool:
                result=await asyncio.get_running_loop().run_in_executor(pool,run_case_process,spec,str(storage),str(template),owner,args.timeout)
            else:result=await run_case(spec,storage,template,owner,args.timeout)
            result['code_sha256']=code['sha256'];records.append(result)
            with (out/'runs.jsonl').open('a') as f:f.write(json.dumps(result,ensure_ascii=False)+'\n')
            print(json.dumps({k:result.get(k) for k in ['run_id','status','actual_count','elapsed_seconds','error']}),flush=True)
            if args.retain_databases=='representative' and result['status']=='passed':
                path=storage/(spec['run_id']+'.sqlite3')
                if i<6 and storage!=out:shutil.copyfile(path,out/path.name)
                if storage!=out or i>=6:path.unlink(missing_ok=True)
    await asyncio.gather(*(one(i) for i in range(args.start,args.start+args.runs)))
    if pool:pool.shutdown(wait=True)
    final={'runs_requested':args.runs,'runs_completed':len(records),'passed':sum(r['status']=='passed' for r in records),
        'failed':sum(r['status']!='passed' for r in records),'followers_per_run':args.followers,'elapsed_seconds':time.perf_counter()-start,
        'production_code':code,'code_unchanged_during_campaign':code==code_digest(),
        'verification_boundary':'Offline geometry DOM plus actual production IG reader/manager/SQLite; zero real-browser runs in this campaign.',
        'storage_kind':storage_kind(storage),'database_mode':'file-backed SQLite',
        'power_loss_durability_tested':False,'sqlite_synchronous':'FULL (production unchanged)',
        'clock_acceleration':'PlaywrightWorker._collection_monotonic seam advances0.01 logical seconds per check; poll interval0; production loading grace20s retained; real controls/writes preserved',
        'records':sorted(records,key=lambda r:r['run_id'])}
    (out/'manifest.json').write_text(json.dumps(final,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in final.items() if k not in ('records','production_code')}),flush=True)
    return 0 if final['failed']==0 and final['code_unchanged_during_campaign'] else 1

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--runs',type=int,default=500);p.add_argument('--start',type=int,default=0)
    p.add_argument('--followers',type=int,default=1200);p.add_argument('--parallel',type=int,default=1);p.add_argument('--timeout',type=float,default=300)
    p.add_argument('--output',required=True);p.add_argument('--database-directory');p.add_argument('--processes',action='store_true');p.add_argument('--retain-databases',choices=['all','representative'],default='representative');args=p.parse_args()
    if args.runs<1 or args.followers<1000 or args.parallel<1:p.error('At least one run,1000 followers and one worker required')
    raise SystemExit(asyncio.run(main_async(args)))
