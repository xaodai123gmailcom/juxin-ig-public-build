"""Standalone R6 fixed policy and durable safety, SQLite/fakes only."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_studio as fixtures
from app.errors import ConflictError, ValidationError
from app.studio import build_nurture_steps, config_for, standalone_config
from app.studio_worker import ResultUncertain
from app.standalone_nurture import StandaloneNurture, exact_count, signed_in_id


class FixedPolicyTests(unittest.TestCase):
    def test_defaults_legacy_overrides_and_finite_bounds(self):
        cfg=config_for('nurture',{'surfaces':['profile'],'targets':['last_seed'],'dwell_min':300,'dwell_max':3,'like_probability':0,'like_limit':1,'comment_probability':100,'rounds':20,'scheduled_at':'2099-01-01'})
        self.assertEqual((5,0,['reels'],8,20,70,0,1,''),(cfg['minutes'],cfg['concurrency'],cfg['surfaces'],cfg['dwell_min'],cfg['dwell_max'],cfg['like_probability'],cfg['comment_probability'],cfg['rounds'],cfg['scheduled_at']))
        for field in ('minutes','concurrency'):
            for value in (float('inf'),float('nan'),True,'5',-1,1001):
                with self.subTest(field=field,value=value),self.assertRaises(ValidationError):config_for('nurture',{field:value})
        self.assertEqual(120,config_for('nurture',{'minutes':120})['minutes'])

    def test_partition_has_no_short_tail_and_only_reels(self):
        for minutes in (1,2,5,120):
            for _ in range(12):
                steps=build_nurture_steps(config_for('nurture',{'minutes':minutes}))
                self.assertEqual(minutes*60,sum(s['seconds'] for s in steps))
                self.assertTrue(all(8<=s['seconds']<=20 and s['surface']=='reels' and s['url']=='https://www.instagram.com/reels/' for s in steps))

    def test_authoritative_counts_unknown_is_not_zero(self):
        for raw,want in [('0',0),('12,345',12345),('12.345',12345),('1.2K',None),('1.2万',None),('',None),(True,None),(None,None)]:
            self.assertEqual(want,exact_count(raw))


class DecisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b=SimpleNamespace(nurture_actions={},nurture_decisions={},nurture_observation=AsyncMock(),nurture_decision=AsyncMock())
        self.n=StandaloneNurture(self.b);self.n.like=AsyncMock()
        self.current={'key':'/reel/A','source':'blob:a','state':'unliked'}

    async def test_fixed_seventy_boundary_one_draw_on_revisit_and_restart(self):
        self.n.draw=Mock(return_value=.69999)
        await self.n.decide(self.current,{})
        await self.n.decide(self.current,{})
        newer=StandaloneNurture(self.b);newer.like=AsyncMock();newer.draw=Mock(side_effect=AssertionError('no redraw'))
        await newer.decide(self.current,{})
        self.n.draw.assert_called_once();self.n.like.assert_awaited_once();newer.like.assert_not_awaited()
        self.assertTrue(self.b.nurture_decisions['/reel/A']['selected'])
        self.n.draw=Mock(return_value=.70)
        await self.n.decide(dict(self.current,key='/reel/B'),{})
        self.assertFalse(self.b.nurture_decisions['/reel/B']['selected'])

    async def test_liked_unknown_and_negative_decisions_are_persisted_without_click(self):
        for i,state in enumerate(('liked','unknown','unliked')):
            n=StandaloneNurture(self.b);n.draw=Mock(return_value=.99);n.like=AsyncMock()
            current=dict(self.current,key='/reel/'+str(i),state=state)
            await n.decide(current,{})
            await n.decide(dict(current,state='unliked'),{})
            n.like.assert_not_awaited()
            self.assertIn(current['key'],self.b.nurture_decisions)
            self.assertEqual(1 if state=='unliked' else 0,n.draw.call_count)
        self.assertEqual(3,self.b.nurture_decision.await_count)

    async def test_pending_is_never_replayed_even_if_decision_exists(self):
        for state in ('pending','unresolved_stopped'):
            self.b.nurture_actions['like:/reel/A']={'state':state}
            with self.assertRaises(ResultUncertain):await self.n.decide(self.current,{})
        self.n.like.assert_not_awaited()

    async def test_login_cookie_requires_unique_numeric_identity(self):
        page=SimpleNamespace(context=SimpleNamespace(cookies=AsyncMock(return_value=[{'name':'ds_user_id','value':'123'}])))
        self.assertEqual('123',await signed_in_id(page))
        for cookies in ([],[{'name':'ds_user_id','value':'unknown'}],[{'name':'ds_user_id','value':'123'},{'name':'ds_user_id','value':'456'}]):
            page.context.cookies.return_value=cookies
            with self.assertRaises(ValidationError):await signed_in_id(page)


class PersistenceTests(unittest.TestCase):
    setUp=fixtures.StudioTests.setUp
    tearDown=fixtures.StudioTests.tearDown
    execute=fixtures.StudioTests.execute

    async def start(self,profiles=None,key='standalone-request',**cfg):
        return (await self.m.command(self.owner,{'action':'start','kind':'nurture','profile_ids':profiles or ['w1'],'request_id':key,'config':{'minutes':1,**cfg}}))['job_ids']

    def test_admission_atomic_busy_mixed_batch_and_idempotency(self):
        async def run():
            ids=await self.start()
            self.assertEqual(ids,await self.start())
            with self.assertRaises(ConflictError):await self.start(['free','w1'],key='another-request')
            self.assertEqual(1,len(self.m.snapshot(self.owner)['jobs']))
            self.assertEqual([],self.browser.closed)
        asyncio.run(run())

    def test_incompatible_legacy_jobs_are_paused_not_reinterpreted(self):
        async def run():
            ident=(await self.start())[0];cfg=json.loads(self.m.get(self.owner,ident)['config_json']);cfg.pop('standalone_policy')
            self.m.update(ident,config_json=json.dumps(cfg),cursor=1,result_json=json.dumps({'counts':{'like':2}}))
            self.m._schedule_ready()
            self.assertEqual('paused',self.m.get(self.owner,ident)['status'])
            self.assertEqual(1,self.m.get(self.owner,ident)['cursor'])
            with self.assertRaisesRegex(ValidationError,'旧版'):await self.m.control(self.owner,ident,'resume')
            self.assertFalse(self.m.active_ids());self.assertEqual([],self.browser.closed)
        asyncio.run(run())

    def test_corrupt_truncated_steps_fail_closed(self):
        async def run():
            ident=(await self.start())[0];cfg=json.loads(self.m.get(self.owner,ident)['config_json']);cfg['steps']=cfg['steps'][:1]
            with self.assertRaisesRegex(ValidationError,'时长'):standalone_config(cfg)
        asyncio.run(run())

    def test_profile_history_pending_receipt_and_single_lease_survive_recovery(self):
        async def run():
            ident=(await self.start())[0]
            snapshot={'username':'actual_owner','instagram_user_id':'321','posts_count':0,'followers_count':None,'following_count':12,'status':'partial','checked_at':'2026-10-02T10:00:00Z'}
            tokens=[]
            async def step(browser,*args):
                with self.db.read() as c:tokens.append(c.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?',('w1',)).fetchone()[0])
                await browser.account_snapshot(snapshot)
                await browser.nurture_ready()
                await browser.nurture_decision('/reel/A',{'selected':True})
                await browser.nurture_action_begin('like:/reel/A',{'action':'like','target':'/reel/A','state':'pending'})
                raise ResultUncertain('injected ambiguous click')
            with patch('app.studio.PlaywrightWorker',fixtures.Worker),patch('app.studio.StudioBrowser.nurture_step',step):await self.execute(ident)
            row=self.m.get(self.owner,ident);result=json.loads(row['result_json'])
            self.assertEqual('needs_review',row['status']);self.assertEqual(1,row['inflight'])
            self.assertEqual(snapshot,result['account_snapshot']);self.assertEqual('actual_owner',result['executor']['username'])
            self.assertIsNone(result['account_snapshot']['followers_count']);self.assertEqual(0,result['account_snapshot']['posts_count'])
            self.assertIn('nurture_actual_seconds',result);self.assertIn('nurture_finished_at',result)
            with self.db.read() as c:self.assertEqual(tokens[0],c.execute('SELECT lease_token FROM browser_operation_leases WHERE profile_id=?',('w1',)).fetchone()[0])
            self.assertEqual([],self.browser.closed);self.assertEqual([],self.m.snapshot(self.other)['jobs'])
            with self.assertRaises(ConflictError):await self.m.control(self.owner,ident,'resume')
        asyncio.run(run())

    def test_preflight_failure_keeps_unknown_metrics_and_zero_active_reels(self):
        async def run():
            ident=(await self.start())[0]
            async def fail(*_):raise ValidationError('profile login not verified')
            with patch('app.studio.PlaywrightWorker',fixtures.Worker),patch('app.studio.StudioBrowser.nurture_step',fail):await self.execute(ident)
            result=json.loads(self.m.get(self.owner,ident)['result_json'])
            self.assertEqual(0,result['nurture_actual_seconds']);self.assertEqual('failed',result['nurture_outcome'])
            self.assertIsNone(result['account_snapshot']['posts_count']);self.assertTrue(result['account_snapshots'])
            self.assertEqual([],self.browser.closed)
        asyncio.run(run())

    def test_success_report_date_is_immutable_across_cleanup_and_later_updates(self):
        async def run():
            from app.work_reports import work_report
            ident=(await self.start())[0]
            async def browse(browser,step,counts,config):return dict(counts,browse=counts.get('browse',0)+1)
            with patch('app.studio.PlaywrightWorker',fixtures.Worker),patch('app.studio.StudioBrowser.nurture_step',browse),patch('app.studio.isoformat',return_value='2026-10-02T12:00:00+00:00'):
                await self.execute(ident)
            row=self.m.get(self.owner,ident);result=json.loads(row['result_json'])
            self.assertEqual('completed',row['status'])
            self.assertEqual('2026-10-02T12:00:00+00:00',result['confirmed_at'])
            self.assertEqual(result['confirmed_at'],result['nurture_finished_at'])
            with self.db.write() as c:c.execute("UPDATE studio_jobs SET updated_at='2026-10-03T12:00:00+00:00' WHERE id=?",(ident,))
            self.assertEqual(1,work_report(self.db,self.owner,'2026-10-02T00:00:00Z','2026-10-03T00:00:00Z')['totals']['nurture'])
            self.assertEqual(0,work_report(self.db,self.owner,'2026-10-03T00:00:00Z','2026-10-04T00:00:00Z')['totals']['nurture'])
        asyncio.run(run())

    def test_pending_and_legacy_started_early_return_do_not_lose_elapsed_or_actor(self):
        async def run():
            for i,pending in enumerate((False,True)):
                ident=(await self.start(['w'+str(i)],key='started-early-'+str(i)))[0]
                cfg=json.loads(self.m.get(self.owner,ident)['config_json'])
                if not pending:cfg.pop('standalone_policy')
                result={'nurture_started_at':'2026-10-02T10:00:00Z','nurture_actual_seconds':12.5,'executor':{'username':'original'},'nurture_actions':{'like:/reel/A':{'state':'pending','action':'like'}} if pending else {}}
                self.m.update(ident,config_json=json.dumps(cfg),result_json=json.dumps(result),inflight=int(pending))
                with patch('app.studio.PlaywrightWorker') as worker:
                    await self.execute(ident);worker.assert_not_called()
                saved=json.loads(self.m.get(self.owner,ident)['result_json'])
                self.assertEqual(12.5,saved['nurture_actual_seconds']);self.assertEqual('original',saved['executor']['username'])
                self.assertNotIn('confirmed_at',saved)
        asyncio.run(run())

    def test_snapshot_does_not_ship_full_technical_decision_ledger(self):
        async def run():
            ident=(await self.start())[0]
            receipt={'nurture_decisions':{str(i):{'selected':False} for i in range(900)},'nurture_actions':{'old':{'state':'confirmed'},'pending':{'state':'pending'},'stopped':{'state':'unresolved_stopped'}}}
            self.m.update(ident,result_json=json.dumps(receipt))
            exposed=self.m.snapshot(self.owner)['jobs'][0]['result']
            self.assertNotIn('nurture_decisions',exposed);self.assertEqual(900,exposed['nurture_decision_count'])
            self.assertEqual({'pending','stopped'},set(exposed['nurture_actions']))
            self.assertEqual(receipt,json.loads(self.m.get(self.owner,ident)['result_json']))
        asyncio.run(run())

    def test_old_wall_clock_history_cannot_complete_as_verified_playback(self):
        async def run():
            ident=(await self.start())[0]
            before={'nurture_actual_seconds':59,'nurture_started_at':'2026-10-02T10:00:00Z','counts':{'like':1},'nurture_decisions':{'/reel/A':{'selected':True}}}
            self.m.update(ident,result_json=json.dumps(before))
            with patch('app.studio.PlaywrightWorker') as worker:await self.execute(ident)
            worker.assert_not_called()
            row=self.m.get(self.owner,ident);saved=json.loads(row['result_json'])
            self.assertEqual('paused',row['status']);self.assertEqual(59,saved['nurture_actual_seconds'])
            self.assertEqual(before['counts'],saved['counts']);self.assertNotIn('confirmed_at',saved)
            self.assertEqual([],self.browser.closed)
        asyncio.run(run())

    def test_verified_playback_is_saved_periodically_and_at_pause(self):
        async def run():
            ident=(await self.start())[0]
            async def step(browser,*_):
                await browser.nurture_ready()
                await browser.nurture_watched(.5);await browser.nurture_watched(.5)
                self.assertEqual(1,json.loads(self.m.get(self.owner,ident)['result_json'])['nurture_actual_seconds'])
                await browser.nurture_watched(.25)
                await self.m.control(self.owner,ident,'pause')
                waiting=asyncio.create_task(browser.checkpoint());await asyncio.sleep(0)
                self.assertEqual(1.25,json.loads(self.m.get(self.owner,ident)['result_json'])['nurture_actual_seconds'])
                await self.m.control(self.owner,ident,'resume');await waiting
                raise asyncio.CancelledError
            with patch('app.studio.PlaywrightWorker',fixtures.Worker),patch('app.studio.StudioBrowser.nurture_step',step):await self.execute(ident)
            self.assertEqual(1.25,json.loads(self.m.get(self.owner,ident)['result_json'])['nurture_actual_seconds'])
        asyncio.run(run())


class PreparationAndClockTests(unittest.IsolatedAsyncioTestCase):
    def make_browser(self,reels):
        from app.standalone_nurture import OWN_LINK, OWN_METRICS, REEL
        page=SimpleNamespace(url='https://www.instagram.com/',context=SimpleNamespace(cookies=AsyncMock(return_value=[{'name':'ds_user_id','value':'123'}])))
        async def goto(url,**kwargs):page.url=url
        reads={'counts':0,'reels':0}
        async def evaluate(script,*_):
            if script==OWN_LINK:return 'owner'
            if script==OWN_METRICS:
                reads['counts']+=1
                return {} if reads['counts']<2 else {'posts_count':'0','followers_count':'1,234','following_count':'3'}
            if script==REEL:
                index=reads['reels'];reads['reels']+=1
                return reels[min(index,len(reels)-1)]
            raise AssertionError('unexpected script')
        page.goto=AsyncMock(side_effect=goto);page.evaluate=AsyncMock(side_effect=evaluate)
        browser=SimpleNamespace(page=page,worker=SimpleNamespace(open_account_home_page=AsyncMock()),checkpoint=AsyncMock(),guard=AsyncMock(),account_snapshot=AsyncMock(),nurture_ready=AsyncMock())
        return browser,reads

    async def test_profile_counters_and_media_loading_finish_before_reels_clock(self):
        stable={'key':'/reel/A','source':'blob:a','state':'unliked'}
        b,reads=self.make_browser([None,stable,stable]);n=StandaloneNurture(b)
        async def ready():
            self.assertGreaterEqual(reads['counts'],2);self.assertGreaterEqual(reads['reels'],3)
            self.assertEqual('owner',b.account_snapshot.await_args.args[0]['username'])
        b.nurture_ready.side_effect=ready
        with patch('app.instagram_home.prepare_instagram_home',new=AsyncMock()),patch('app.standalone_nurture.asyncio.sleep',new=AsyncMock()):await n.prepare()
        b.nurture_ready.assert_awaited_once()
        self.assertEqual(1234,b.account_snapshot.await_args.args[0]['followers_count'])

    async def test_missing_media_never_starts_activity_clock(self):
        b,reads=self.make_browser([None]);n=StandaloneNurture(b)
        with patch('app.instagram_home.prepare_instagram_home',new=AsyncMock()),patch('app.standalone_nurture.asyncio.sleep',new=AsyncMock()):
            with self.assertRaisesRegex(ValidationError,'未开始养号计时'):await n.prepare()
        self.assertEqual(30,reads['reels']);b.nurture_ready.assert_not_awaited()
        self.assertEqual('ok',b.account_snapshot.await_args.args[0]['status'])

    async def test_actual_guard_time_counts_toward_dwell_and_short_tail_never_starts(self):
        now=[0.0]
        async def guard():now[0]+=.05
        async def sleep(seconds):now[0]+=seconds
        b=SimpleNamespace(nurture_elapsed=lambda:now[0],nurture_clock=lambda:now[0],nurture_remaining=lambda:60-now[0])
        n=StandaloneNurture(b);n.guard=AsyncMock(side_effect=guard)
        async def current():
            await n.guard()
            return {'key':'/reel/A','source':'video:A','time':now[0]}
        n.current=AsyncMock(side_effect=current)
        with patch('app.standalone_nurture.asyncio.sleep',sleep):elapsed=await n.dwell(8)
        self.assertGreaterEqual(elapsed,8);self.assertLess(elapsed,8.06)
        n.identity={'username':'owner'};n.current=AsyncMock(side_effect=AssertionError('short tail must not start'))
        b.nurture_remaining=lambda:7.9
        counts={'browse':2}
        self.assertIs(counts,await n.step({'seconds':8},counts));n.current.assert_not_awaited()
