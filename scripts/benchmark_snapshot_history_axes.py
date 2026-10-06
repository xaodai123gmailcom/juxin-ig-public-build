"""Read-only actual-route probes after isolated multi-target/task history seeding."""
from __future__ import annotations
import argparse,asyncio,json,sqlite3,tempfile,time,hashlib
from pathlib import Path
from benchmark_pure_ig_acceptance import (MeasuredDatabase,Inventory,Settings,
    CoreService,create_app,summary,rss,ROOT)
NOW='2026-10-01T00:00:00Z'


def seed(db,owner,size,axis,dismissal="half"):
    started=time.perf_counter()
    with db.write() as c:
        c.execute("INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) VALUES('live',?,'Synthetic live queue','running','[\"followers\"]','{\"live_queue_enabled\":true}',?,?)",(owner,NOW,NOW))
        if axis=='tasks':
            c.executemany("INSERT INTO tasks(id,owner_user_id,name,status,modes_json,settings_json,created_at,updated_at) VALUES(?,?,'Synthetic retained task','completed','[\"followers\"]','{}',?,?)",((f'hist{i:07}',owner,NOW,NOW) for i in range(size)))
            c.executemany('INSERT INTO task_list_dismissals(task_id,owner_user_id,dismissed_at) VALUES(?,?,?)',((f'hist{i:07}',owner,NOW) for i in range(0,size,1 if dismissal=="all" else 2)))
        c.executemany("INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,created_at,updated_at) VALUES(?,?,?,?,?,'completed',?,?)",((f'hist-target{i:07}', 'live' if axis=='targets' else f'hist{i:07}',f'synthetic_source{i}',f'synthetic_source{i}',i if axis=='targets' else 0,NOW,NOW) for i in range(size)))
        c.executemany('INSERT INTO task_target_list_dismissals(target_id,owner_user_id,task_id,dismissed_at) VALUES(?,?,?,?)',((f'hist-target{i:07}',owner,'live' if axis=='targets' else f'hist{i:07}',NOW) for i in range(0,size,1 if dismissal=="all" else 2)))
        for i in range(4):
            c.execute("INSERT INTO task_targets(id,task_id,username_norm,username_display,queue_order,status,current_window_id,created_at,updated_at) VALUES(?,'live',?,?,?,'running',?,?,?)",(f'active{i}',f'active_source{i}',f'active_source{i}',size+i,f'window{i}',NOW,NOW))
            c.execute("INSERT INTO task_windows(task_id,profile_id,queue_order,status) VALUES('live',?,?,'running')",(f'window{i}',i))
    return {'axis':axis,'completed_history':size,'active_targets':4,'selected_windows':4,'dismissed_history':size if dismissal=='all' else (size+1)//2,'seed_seconds':time.perf_counter()-started}


async def measure(db,owner,rounds):
    import httpx
    settings=Settings(startup_token='synthetic-history-token',database_path=db.path,data_dir=db.path.parent)
    app=create_app(settings,database=db,bitbrowser=Inventory());svc=app.state.service
    token=svc.login('history-benchmark','synthetic-benchmark-password')['token']
    headers={'X-Startup-Token':settings.startup_token,'Authorization':'Bearer '+token}
    url='/api/workbench/snapshot?limit=2000&history_limit=2000&compact=1&platform=instagram'
    samples=[]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline',headers=headers) as client:
        db.reset()
        for i in range(rounds):
            started=time.perf_counter();response=await client.get(url);duration=time.perf_counter()-started
            response.raise_for_status();payload=response.json()
            samples.append({'seconds':duration,'bytes':len(response.content),'rss_bytes':rss()})
        work=db.stop();db.reset(1000);response=await client.get(url);response.raise_for_status();vm=db.stop()
    statements=list(dict.fromkeys(db.sql));queries=[]
    with db.read() as c:
        for sql in statements:
            steps=[0]
            c.set_progress_handler(lambda:steps.__setitem__(0,steps[0]+1000) or 0,1000)
            started=time.perf_counter();rows=c.execute(sql).fetchall();duration=time.perf_counter()-started
            c.set_progress_handler(None,0)
            queries.append({'vm_steps':steps[0],'seconds':duration,'rows':len(rows),'sql':sql,
                            'plan':[list(r) for r in c.execute('EXPLAIN QUERY PLAN '+sql)]})
    queries.sort(key=lambda q:q['vm_steps'],reverse=True)
    tasks=payload.get('tasks',[])
    await app.state.snapshot_inventory.close()
    return {'url':url,**summary([s['seconds'] for s in samples]),'samples':samples,'work':work,
            'vm_sample':vm,'queries':queries,'task_count_returned':len(tasks),
            'live_task_target_ids':[t['id'] for task in tasks if task['id']=='live' for t in task['targets']],
            'response_bytes':samples[-1]['bytes'],'rss_bytes':rss()}


def main():
    p=argparse.ArgumentParser();p.add_argument('--axis',choices=('tasks','targets'),required=True);p.add_argument('--size',type=int,required=True);p.add_argument('--rounds',type=int,default=5);p.add_argument('--dismissal',choices=('half','all'),default='half');p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    with tempfile.TemporaryDirectory(prefix='history-axes-') as temp:
        db=MeasuredDatabase(Path(temp)/'synthetic.sqlite3');db.recording=False;db.initialize();svc=CoreService(db)
        owner=svc.register_user('history-benchmark','synthetic-benchmark-password')['id']
        fixture=seed(db,owner,a.size,a.axis,a.dismissal)
        with db.read() as c:c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        report=asyncio.run(measure(db,owner,a.rounds));report['fixture']=fixture
        report['source_sha256']={name:hashlib.sha256((ROOT/'backend/app'/name).read_bytes()).hexdigest() for name in ['service.py','database.py','workbench_aggregates.py','workbench_progress_aggregates.py']}
        report['database_bytes']=db.path.stat().st_size
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,indent=2))
    print(json.dumps({'fixture':fixture,**{k:report[k] for k in ['p50_ms','p95_ms','response_bytes','vm_sample','task_count_returned','rss_bytes']},'top_queries':[{k:q[k] for k in ['vm_steps','seconds','rows']}|{'sql':' '.join(q['sql'].split())[:140]} for q in report['queries'][:4]]}),flush=True)
if __name__=='__main__':main()
