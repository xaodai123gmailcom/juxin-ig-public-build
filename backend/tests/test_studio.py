from __future__ import annotations
import asyncio, io, json, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
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
        self.m.media.files.root=Path(self.tmp.name)/'Desktop'/'聚鑫国际素材'
        b=io.BytesIO();Image.new('RGB',(24,24)).save(b,format='PNG');self.png=b.getvalue();self.asset=self.m.media.store(self.owner,self.png,'样例','manual')
    def tearDown(self):self.tmp.cleanup()
    async def start(self,kind='posting',profiles=None,config=None,key='request-number-one'):
        return await self.m.command(self.owner,{'action':'start','kind':kind,'request_id':key,'profile_ids':profiles or ['w1'],'config':config or {'asset_ids':[self.asset]}})
    async def execute(self,ident):
        self.m.gates[ident]=asyncio.Event();self.m.gates[ident].set()
        await self.m._execute(self.m.get(self.owner,ident))
    def test_schema_upgrade_idempotent_and_old_monitor_data_untouched(self):
        self.db.initialize()
        with self.db.read() as c:
            self.assertEqual(list(range(1,42)),[r[0] for r in c.execute('SELECT version FROM schema_migrations ORDER BY version')])
            self.assertEqual('ok',c.execute('PRAGMA integrity_check').fetchone()[0])
    def test_upload_normalizes_image_and_rejects_non_media(self):
        row=self.m.media.get(self.owner,self.asset);self.assertTrue(Path(row['path']).read_bytes().startswith(b'\xff\xd8'))
        with self.assertRaises(ValidationError):self.m.media.store(self.owner,b'not an image','bad','manual')
    def test_assets_templates_jobs_and_stats_are_owner_scoped(self):
        async def run():
            await self.start()
            await self.m.command(self.owner,{'action':'save_template','kind':'posting','config':{'asset_ids':[self.asset]}})
        asyncio.run(run());self.assertEqual([],self.m.snapshot(self.other)['jobs']);self.assertEqual([],self.m.snapshot(self.other)['assets']);self.assertEqual({},self.m.snapshot(self.other)['templates'])
        with self.assertRaises(NotFoundError):self.m.media.get(self.other,self.asset)
    def test_key_never_in_snapshot_or_ui_payload(self):
        # Redaction must be tested with a nonempty, test-owned credential;
        # release builds do not contain the owner's private API keys.
        fixture_key='fixture-pexels-key-not-a-real-credential'
        with patch.dict('os.environ', {'IGAC_PEXELS_API_KEY':fixture_key}):
            self.assertEqual(fixture_key,self.m.media.pexels_key())
            snapshot=self.m.snapshot(self.owner)
            self.assertNotIn(fixture_key,json.dumps(snapshot))
            self.assertTrue(snapshot['credentials']['pexels_configured'])
    def test_missing_optional_key_is_reported_without_blocking_snapshot(self):
        with patch.dict('os.environ', {}, clear=True), patch('app.studio_credentials.PEXELS_API_KEY',''):
            snapshot=self.m.snapshot(self.owner)
            self.assertFalse(snapshot['credentials']['pexels_configured'])
            self.assertIsInstance(snapshot['jobs'],list)
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

    def test_pexels_key_cannot_follow_a_cross_host_redirect(self):
        from app.studio_media import PexelsRedirects
        from urllib.request import Request
        redirect=PexelsRedirects(); req=Request('https://api.pexels.com/v1/search',headers={'Authorization':'sample-secret'})
        for url in ('https://evil.example/path','http://api.pexels.com/v1/search'):
            with self.assertRaises(ValidationError):redirect.redirect_request(req,None,302,'Found',{},url)
        self.assertIsNotNone(redirect.redirect_request(req,None,302,'Found',{},'https://api.pexels.com/v1/search?page=2'))

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
    def test_successful_post_closes_and_releases_its_window(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',new_callable=AsyncMock,return_value={'published':1}):await self.execute(ident)
            self.assertEqual('completed',self.m.get(self.owner,ident)['status']);self.assertEqual(['w1'],self.browser.closed)
            with self.db.read() as c:self.assertEqual(0,c.execute('SELECT count(*) FROM browser_operation_leases').fetchone()[0])
        asyncio.run(run())
    def test_uncertain_publish_never_restarts_automatically(self):
        async def publish(browser,*args):await browser.before_effect('share');raise ResultUncertain('uncertain')
        async def run():
            ident=(await self.start())['job_ids'][0]
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',publish):await self.execute(ident)
            self.assertEqual('needs_review',self.m.get(self.owner,ident)['status']);self.m.recover();self.assertEqual('needs_review',self.m.get(self.owner,ident)['status'])
            with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'resume')
        asyncio.run(run())
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

    def test_cancel_during_publish_preserves_lease_and_window_for_review(self):
        async def run():
            ident=(await self.start())['job_ids'][0];submitted=asyncio.Event()
            async def publish(browser,*_):
                await browser.before_effect('share');submitted.set();await asyncio.Event().wait()
            self.m.gates[ident]=asyncio.Event();self.m.gates[ident].set()
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',publish):
                task=asyncio.create_task(self.m._execute(self.m.get(self.owner,ident)));self.m.tasks[ident]=task
                await asyncio.wait_for(submitted.wait(),2);await self.m.control(self.owner,ident,'cancel');await task
            self.assertEqual('needs_review',self.m.get(self.owner,ident)['status']);self.assertEqual([],self.browser.closed)
            with self.assertRaises(ConflictError):
                self.s.acquire_browser_lease(self.owner,'w1',operation_type='monitor',entity_id='after-review',ttl_seconds=90)
        asyncio.run(run())

    def test_scheduler_honors_concurrency_and_completes_all_windows(self):
        async def run():
            ids=(await self.start(profiles=['w1','w2'],config={'asset_ids':[self.asset],'concurrency':1,'interval_seconds':0}))['job_ids']
            active=0;maximum=0
            async def publish(browser,*_):
                nonlocal active,maximum
                active+=1;maximum=max(active,maximum);await asyncio.sleep(.05);active-=1;return {'published':1}
            with patch('app.studio.PlaywrightWorker',Worker),patch('app.studio.StudioBrowser.publish',publish):
                self.m.start_scheduler()
                try:
                    async with asyncio.timeout(5):
                        while any(self.m.get(self.owner,i)['status']!='completed' for i in ids):await asyncio.sleep(.05)
                finally:await self.m.shutdown()
            self.assertEqual(1,maximum);self.assertEqual({'w1','w2'},set(self.browser.closed))
        asyncio.run(run())

    def test_shutdown_during_disconnect_still_closes_owned_window(self):
        async def run():
            ident=(await self.start())['job_ids'][0];worker=Worker();entered=asyncio.Event();release=asyncio.Event()
            async def disconnect():entered.set();await release.wait()
            worker.disconnect.side_effect=disconnect
            with patch('app.studio.PlaywrightWorker',return_value=worker),patch('app.studio.StudioBrowser.publish',new=AsyncMock(return_value={'published':1})):
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

    def test_product_disables_posting_creation_draft_publish_and_resume(self):
        from app.main import create_app
        from app.config import Settings
        app=create_app(Settings(startup_token='studio-disabled-test-token-00000001',database_path=self.db.path,data_dir=Path(self.tmp.name)),database=self.db,bitbrowser=self.browser)
        manager=app.state.studio
        self.assertFalse(manager.posting_enabled)
        async def run():
            ident=(await self.start())['job_ids'][0]
            for body in ({'action':'start','kind':'posting'},{'action':'start','kind':'material'},{'action':'start_drafts','assignments':[]}):
                with self.assertRaisesRegex(ValidationError,'停用'):await manager.command(self.owner,body)
            for operation in ('resume','retry'):
                with self.assertRaisesRegex(ValidationError,'停用'):await manager.control(self.owner,ident,operation)
            result=await manager.command(self.owner,{'action':'start','kind':'nurture','request_id':'nurture-remains-enabled','profile_ids':['w2'],'config':{'minutes':1}})
            self.assertEqual(1,len(result['job_ids']))
            self.assertEqual(2,len(manager.snapshot(self.owner)['jobs']))
        asyncio.run(run())

    def test_disabled_posting_recovers_old_schedules_without_losing_history_assets_or_other_leases(self):
        async def seed():
            ids=(await self.start(profiles=['w1','w2','w3']))['job_ids']
            material=(await self.start('material',key='material-to-pause'))['job_ids'][0]
            nurture=(await self.start('nurture',['w4'],{'minutes':1},key='nurture-keeps-schedule'))['job_ids'][0]
            return ids,material,nurture
        ids,material,nurture=asyncio.run(seed())
        self.m.update(ids[0],due_at='2099-01-01T00:00:00+00:00')
        self.m.update(ids[1],status='running',inflight=1)
        self.m.update(ids[2],status='running',cursor=1,result_json=json.dumps({'published':1}))
        lease=self.s.acquire_browser_lease(self.owner,'other-window',operation_type='monitor',entity_id='keep-monitor',ttl_seconds=90)
        manager=StudioManager(self.s,self.browser,posting_enabled=False);manager.recover()
        self.assertEqual(['paused','needs_review','completed'],[manager.get(self.owner,i)['status'] for i in ids])
        self.assertEqual('paused',manager.get(self.owner,material)['status']);self.assertEqual('queued',manager.get(self.owner,nurture)['status'])
        self.assertTrue(Path(manager.media.get(self.owner,self.asset)['path']).is_file())
        self.assertEqual(5,len(manager.snapshot(self.owner)['jobs']))
        with self.db.read() as c:self.assertIsNotNone(c.execute('SELECT 1 FROM browser_operation_leases WHERE lease_token=?',(lease,)).fetchone())
        async def run():
            with patch.object(manager.media,'prepare') as prepare,patch('app.studio.PlaywrightWorker') as worker:
                await manager._execute(manager.get(self.owner,ids[0]));prepare.assert_not_called();worker.assert_not_called()
        asyncio.run(run())

    def test_disabled_posting_does_not_starve_nurture_scheduler(self):
        async def run():
            ident=(await self.start())['job_ids'][0]
            nurture=(await self.start('nurture',['w2'],{'minutes':1},key='scheduler-nurture'))['job_ids'][0]
            row=self.m.get(self.owner,ident)
            with self.db.write() as c:
                for i in range(205):
                    c.execute('INSERT INTO studio_jobs(id,owner_user_id,request_key,kind,profile_id,config_json,total_steps,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',('queued-post-'+str(i),self.owner,'queued-request-'+str(i),'posting','w1',row['config_json'],1,'2000-01-01T00:00:00+00:00',row['created_at'],row['updated_at']))
            manager=StudioManager(self.s,self.browser,posting_enabled=False);seen=[]
            async def execute(row):seen.append(row['id'])
            sleep=asyncio.sleep
            async def one_tick(_):manager.stopping=True;await sleep(0)
            with patch.object(manager,'_execute',execute),patch('app.studio.asyncio.sleep',one_tick):await manager._schedule()
            await sleep(0)
            self.assertEqual([nurture],seen)
            self.assertEqual('queued',manager.get(self.owner,ident)['status'])
        asyncio.run(run())

if __name__=='__main__':unittest.main()
