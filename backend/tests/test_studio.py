from __future__ import annotations
import asyncio, io, json, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.database import Database
from app.service import CoreService
from app.errors import ConflictError, NotFoundError, ValidationError
from app.studio import StudioManager, build_nurture_steps, config_for
from app.studio_worker import instagram_target, ResultUncertain

class Browser:
    def __init__(self): self.closed=[]
    def close_profile(self,p): self.closed.append(p)
class Worker:
    def __init__(self,*_): self.connect=AsyncMock();self.disconnect=AsyncMock();self.page=SimpleNamespace()

class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Database(Path(self.tmp.name)/'test.sqlite');self.db.initialize()
        self.s=CoreService(self.db,session_hours=1);self.owner=self.s.register_user('studio-owner','correct horse battery staple')['id']
        self.other=self.s.register_user('studio-other','correct horse battery staple')['id'];self.browser=Browser();self.m=StudioManager(self.s,self.browser)
    def tearDown(self):self.tmp.cleanup()
    async def start(self,kind='nurture',profiles=None,config=None,key='request-number-one'):
        return await self.m.command(self.owner,{'action':'start','kind':kind,'request_id':key,'profile_ids':profiles or ['w1'],'config':config or {'minutes':1}})
    async def execute(self,ident):
        self.m.gates[ident]=asyncio.Event();self.m.gates[ident].set()
        await self.m._execute(self.m.get(self.owner,ident))
    def test_schema_upgrade_idempotent_and_old_monitor_data_untouched(self):
        self.db.initialize()
        with self.db.read() as c:
            self.assertEqual(list(range(1,42)),[r[0] for r in c.execute('SELECT version FROM schema_migrations ORDER BY version')])
            self.assertEqual('ok',c.execute('PRAGMA integrity_check').fetchone()[0])
    def test_start_is_idempotent(self):
        async def run():self.assertEqual(await self.start(),await self.start())
        asyncio.run(run());self.assertEqual(1,len(self.m.snapshot(self.owner)['jobs']))
    def test_nurture_template_normalizes_legacy_targets_before_save(self):
        async def run():
            for cfg in ({'surfaces':['post'],'targets':['only.username']},{'surfaces':['profile'],'targets':['https://www.instagram.com/p/ABC/']}):
                await self.m.command(self.owner,{'action':'save_template','kind':'nurture','config':dict(cfg,minutes=7)})
                saved=self.m.snapshot(self.owner)['templates']['nurture']
                self.assertEqual(7,saved['minutes'])
                self.assertEqual(['reels'],saved['surfaces']);self.assertEqual([],saved['targets'])
            self.assertEqual([],self.m.snapshot(self.owner)['jobs'])
        asyncio.run(run())

    def test_nurture_normalizes_removed_actions_and_rejects_unknown_options(self):
        for forbidden in ('posting','dm'):
            cfg=config_for('nurture',{'surfaces':[forbidden],'comment_limit':1,
                'comment_probability':100,'comments':[' hello ','world'],'save_probability':100,
                'save_limit':20,'follow_probability':100,'follow_limit':20})
            self.assertEqual(['reels'],cfg['surfaces']);self.assertEqual([],cfg['comments'])
            self.assertEqual(70,cfg['like_probability'])
            for action in ('comment','save','follow'):
                self.assertEqual(0,cfg[action+'_probability']);self.assertEqual(0,cfg[action+'_limit'])
        with self.assertRaises(ValidationError):config_for('nurture',{'send_dm':True})
    def test_plan_duration_and_destination_validation(self):
        c=config_for('nurture',{'minutes':2});steps=build_nurture_steps(c);self.assertEqual(120,sum(s['seconds'] for s in steps))
        for url in ('https://example.com/p/123/','https://instagram.com.evil/x','file:///tmp/a','https://instagram.com/accounts/login/'):
            with self.assertRaises(ValidationError):instagram_target(url)
        self.assertEqual('https://www.instagram.com/example/',instagram_target('@example'))
    def test_legacy_mixed_targets_cannot_change_the_fixed_reels_plan(self):
        cfg=config_for('nurture',{'surfaces':['profile','post'],'targets':['@example','https://www.instagram.com/p/ABC_123/'],'minutes':10})
        self.assertEqual([],cfg['targets']);self.assertEqual(['reels'],cfg['surfaces'])
        steps=build_nurture_steps(cfg)
        self.assertEqual(600,sum(step['seconds'] for step in steps))
        for step in steps:
            self.assertEqual('reels',step['surface'])
            self.assertEqual('https://www.instagram.com/reels/',step['url'])
            self.assertTrue(8<=step['seconds']<=20)


    def test_removed_rounds_and_schedule_create_one_immediate_job_per_window(self):
        async def run():
            result=await self.start('nurture',['w1','w2'],{'rounds':2,'minutes':1,'interval_seconds':300,'scheduled_at':'2099-01-01T00:00:00Z'})
            self.assertEqual(2,len(result['job_ids']));self.assertEqual(2,len(set(result['job_ids'])))
        asyncio.run(run());jobs=self.m.snapshot(self.owner)['jobs']
        self.assertEqual(1,len({j['due_at'] for j in jobs}))
        self.assertEqual({'w1','w2'},{j['profile_id'] for j in jobs})
        for job in jobs:
            self.assertFalse(job['due_at'].startswith('2099'))
            self.assertEqual(1,job['config']['rounds']);self.assertEqual(0,job['config']['interval_seconds'])
            self.assertEqual('',job['config']['scheduled_at'])





    def test_api_requires_session_and_isolates_owner(self):
        from app.main import create_app
        from app.config import Settings
        settings=Settings(startup_token='studio-startup-token-test-000000001',database_path=self.db.path,data_dir=Path(self.tmp.name))
        app=create_app(settings,database=self.db,bitbrowser=self.browser)
        async def request(headers):
            scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','scheme':'http','method':'GET','path':'/api/studio/snapshot','raw_path':b'/api/studio/snapshot','query_string':b'', 'headers':[(k.lower().encode(),v.encode()) for k,v in headers.items()], 'client':('127.0.0.1',1),'server':('127.0.0.1',8765)}
            sent=[];done=False
            async def receive():
                nonlocal done
                if not done:done=True;return {'type':'http.request','body':b'','more_body':False}
                await asyncio.Event().wait()
            async def send(m):sent.append(m)
            await app(scope,receive,send)
            return next(m['status'] for m in sent if m['type']=='http.response.start'),json.loads(b''.join(m.get('body',b'') for m in sent if m['type']=='http.response.body'))
        async def run():
            self.assertEqual(401,(await request({'X-Startup-Token':settings.startup_token}))[0])
            await self.start()
            for username,expected in [('studio-owner',1),('studio-other',0)]:
                token=self.s.login(username,'correct horse battery staple')['token']
                status,body=await request({'X-Startup-Token':settings.startup_token,'Authorization':'Bearer '+token})
                self.assertEqual(200,status);self.assertEqual(expected,len(body['jobs']))
        asyncio.run(run())



    def test_control_owner_and_terminal_fence(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            with self.assertRaises(NotFoundError):await self.m.control(self.other,ident,'cancel')
            await self.m.control(self.owner,ident,'pause');self.assertEqual('paused',self.m.get(self.owner,ident)['status'])
            await self.m.control(self.owner,ident,'resume');self.assertEqual('queued',self.m.get(self.owner,ident)['status'])
            await self.m.control(self.owner,ident,'cancel')
            with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'resume')
        asyncio.run(run())


    def test_other_module_lock_causes_wait_without_opening_or_closing(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            lease=self.s.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id='another',ttl_seconds=600)
            with patch('app.studio.PlaywrightWorker') as factory:await self.execute(ident);factory.assert_not_called()
            self.assertEqual('waiting_window',self.m.get(self.owner,ident)['status']);self.assertEqual([],self.browser.closed)
            self.s.release_browser_lease('w1',lease)
        asyncio.run(run())


    def test_live_studio_lock_survives_occupancy_refresh_and_blocks_monitor(self):
        lease=self.s.acquire_browser_lease(self.owner,'w1',operation_type='studio',entity_id='studio-job',ttl_seconds=600)
        states=self.s.list_browser_lease_states(self.owner,active_studio_entity_ids={'studio-job'},active_monitor_entity_ids=set(),inactive_grace_seconds=0)
        self.assertEqual(1,len(states))
        with self.assertRaises(ConflictError):self.s.acquire_browser_lease(self.owner,'w1',operation_type='monitor',entity_id='monitor-run')
        self.s.release_browser_lease('w1',lease)


    def test_recovery_pauses_safe_progress_and_fences_inflight(self):
        async def run():return (await self.start(profiles=['w1','w2']))['job_ids']
        a,b=asyncio.run(run());self.m.update(a,status='running',cursor=2);self.m.update(b,status='running',inflight=1);self.m.recover()
        self.assertEqual(('paused',2),(self.m.get(self.owner,a)['status'],self.m.get(self.owner,a)['cursor']));self.assertEqual('needs_review',self.m.get(self.owner,b)['status'])


    def test_pause_resume_keeps_cursor_and_does_not_replay_steps(self):
        async def run():
            ident=(await self.start('nurture',config={'minutes':1,'dwell_min':30,'dwell_max':30}))['job_ids'][0]
            calls=[];waiting=asyncio.Event()
            async def step(browser,step,counts,config):
                calls.append(step)
                if len(calls)==1:await self.m.control(self.owner,ident,'pause');waiting.set()
                counts['browse']=counts.get('browse',0)+1;return counts
            self.m.gates[ident]=asyncio.Event();self.m.gates[ident].set()
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.nurture_step',step):
                task=asyncio.create_task(self.m._execute(self.m.get(self.owner,ident)));self.m.tasks[ident]=task
                await asyncio.wait_for(waiting.wait(),2);await asyncio.sleep(.01)
                self.assertEqual(1,self.m.get(self.owner,ident)['cursor']);self.assertEqual(1,len(calls))
                await self.m.control(self.owner,ident,'resume');await task
            expected=json.loads(self.m.get(self.owner,ident)['config_json'])['steps']
            self.assertEqual(expected,calls,'every persisted step runs exactly once across pause/resume')
            self.assertEqual(len(expected),self.m.get(self.owner,ident)['cursor'])
            self.assertEqual('completed',self.m.get(self.owner,ident)['status'])
        asyncio.run(run())


    def test_daily_counters_commit_atomically_and_ignore_replayed_step(self):
        async def run():return (await self.start('nurture',config={'minutes':1}))['job_ids'][0]
        ident=asyncio.run(run())
        self.m.commit_nurture_step(self.owner,ident,1,{'counts':{'browse':1,'like':1}})
        self.m.commit_nurture_step(self.owner,ident,1,{'counts':{'browse':1,'like':1}})
        self.m.commit_nurture_step(self.owner,ident,2,{'counts':{'browse':2,'like':1}})
        self.assertEqual({'browse':2,'like':1},self.m.daily_action_counts(self.owner,'w1'))
        self.assertEqual({},self.m.daily_action_counts(self.other,'w1'))


    def test_successful_nurture_closes_and_releases_its_window(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.nurture_step',new_callable=AsyncMock,return_value={'browse':1}):await self.execute(ident)
            self.assertEqual('completed',self.m.get(self.owner,ident)['status']);self.assertEqual(['w1'],self.browser.closed)
            with self.db.read() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
        asyncio.run(run())


    def test_uncertain_nurture_never_restarts_automatically(self):
        async def nurture_step(browser,*args):await browser.before_effect('like');raise ResultUncertain('uncertain')
        async def run():
            ident=(await self.start())['job_ids'][0]
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.nurture_step',nurture_step):await self.execute(ident)
            self.assertEqual('needs_review',self.m.get(self.owner,ident)['status']);self.m.recover();self.assertEqual('needs_review',self.m.get(self.owner,ident)['status'])
            with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'resume')
        asyncio.run(run())


    def test_cancel_during_nurture_preserves_lease_and_window_for_review(self):
        async def run():
            ident=(await self.start())['job_ids'][0];submitted=asyncio.Event()
            async def nurture_step(browser,*_):
                await browser.before_effect('like');submitted.set();await asyncio.Event().wait()
            self.m.gates[ident]=asyncio.Event();self.m.gates[ident].set()
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.nurture_step',nurture_step):
                task=asyncio.create_task(self.m._execute(self.m.get(self.owner,ident)));self.m.tasks[ident]=task
                await asyncio.wait_for(submitted.wait(),2);await self.m.control(self.owner,ident,'cancel');await task
            self.assertEqual('needs_review',self.m.get(self.owner,ident)['status']);self.assertEqual([],self.browser.closed)
            with self.assertRaises(ConflictError):
                self.s.acquire_browser_lease(self.owner,'w1',operation_type='monitor',entity_id='after-review',ttl_seconds=90)
        asyncio.run(run())


    def test_shutdown_during_disconnect_still_closes_owned_window(self):
        async def run():
            ident=(await self.start())['job_ids'][0];worker=Worker();entered=asyncio.Event();release=asyncio.Event()
            async def disconnect():entered.set();await release.wait()
            worker.disconnect.side_effect=disconnect
            with patch('app.studio.PlaywrightWorker',return_value=worker),patch('app.studio.StudioBrowser.nurture_step',new=AsyncMock(return_value={'browse':1})):
                task=asyncio.create_task(self.execute(ident))
                await asyncio.wait_for(entered.wait(),2)
                task.cancel();await asyncio.sleep(0)
                self.assertFalse(task.done(),'cancellation must wait for owned disconnect')
                with self.assertRaises(ConflictError):
                    self.s.acquire_browser_lease(self.owner,'w1',operation_type='monitor',entity_id='too-early',ttl_seconds=90)
                release.set()
                with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(['w1'],self.browser.closed)
            self.assertEqual('completed',self.m.get(self.owner,ident)['status'])
            token=self.s.acquire_browser_lease(self.owner,'w1',operation_type='monitor',entity_id='next',ttl_seconds=90)
            self.s.release_browser_lease('w1',token)
        asyncio.run(run())


    def test_nurture_templates_jobs_and_stats_are_owner_scoped(self):
        from app.account_profile_stats import save_account_snapshot
        async def run():
            ident=(await self.start())['job_ids'][0]
            await self.m.command(self.owner,{'action':'save_template','kind':'nurture','config':{'minutes':7}})
            self.m.commit_nurture_step(self.owner,ident,1,{'counts':{'browse':1,'like':1}})
            save_account_snapshot(self.db,self.owner,'w1',{'username':'owner_profile','posts_count':3})
            own=self.m.snapshot(self.owner);foreign=self.m.snapshot(self.other)
            self.assertEqual([ident],[j['id'] for j in own['jobs']])
            self.assertEqual(7,own['templates']['nurture']['minutes'])
            self.assertEqual(1,own['window_stats'][0]['nurture_count'])
            for field in ('jobs','window_stats','totals','daily'):
                self.assertEqual([],foreign[field],field)
            self.assertEqual({},foreign['templates'])
            with self.assertRaises(NotFoundError):self.m.get(self.other,ident)
            self.assertEqual({},self.m.daily_action_counts(self.other,'w1'))
        asyncio.run(run())

    def test_retired_queue_cannot_starve_nurture_scheduler_when_archive_is_pending(self):
        async def run():
            ident=(await self.start(profiles=['nurture-ready']))['job_ids'][0]
            row=self.m.get(self.owner,ident)
            with self.db.write() as c:
                for i in range(205):
                    c.execute('INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,config_json,total_steps,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        ('retired-queue-'+str(i),self.owner,'retired-request-'+str(i),'posting','legacy-window','{}',1,'2000-01-01T00:00:00+00:00',row['created_at'],row['updated_at']))
            seen=[]
            async def execute(job):seen.append(job['id'])
            with patch.object(self.m,'_execute',execute):
                self.m._schedule_ready()
                await asyncio.gather(*list(self.m.tasks.values()))
            self.assertEqual([ident],seen)
            with self.db.read() as c:
                self.assertEqual(205,c.execute("SELECT COUNT(*) FROM studio_jobs WHERE kind='posting' AND status='queued'").fetchone()[0])
            self.assertEqual([],self.browser.closed)
        asyncio.run(run())


if __name__=='__main__':unittest.main()
