import unittest,tempfile,sqlite3,asyncio,json,io,threading
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from app.posting_workflow import PostingManager
from app.posting_pexels import PexelsProvider
from app.errors import ConflictError,ValidationError
class DB:
 def __init__(self,path):self.path=path;self.live_browser_lease_tokens=set();self.lock=threading.RLock()
 @contextmanager
 def read(self):
  c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
  try:yield c
  finally:c.close()
 @contextmanager
 def write(self):
  with self.lock:
   with self.read() as c:
    c.execute('BEGIN IMMEDIATE')
    try:yield c;c.commit()
    except BaseException:c.rollback();raise
class Service:
 def __init__(self,db):self.database=db;self.seq=0
 async def acquire_browser_lease_async(self,owner,profile,**kw):
  self.seq+=1;token='lease-'+str(self.seq)
  try:
   with self.database.write() as c:c.execute('INSERT INTO browser_operation_leases VALUES(?,?,?,?,?)',(profile,owner,kw['operation_type'],kw['entity_id'],token))
  except sqlite3.IntegrityError:raise ConflictError('busy')
  self.database.live_browser_lease_tokens.add(token);return token
 def release_browser_lease(self,profile,token):
  with self.database.write() as c:c.execute('DELETE FROM browser_operation_leases WHERE profile_id=? AND lease_token=?',(profile,token))
  self.database.live_browser_lease_tokens.discard(token)
 def renew_browser_lease(self,*args,**kw):pass
class Provider:
 def __init__(self,db):self.db=db;self.count=0
 def configured(self):return True
 def prepare(self,job):
  self.count+=1;ident='asset-'+str(self.count)
  with self.db.write() as c:c.execute("INSERT INTO posting_assets(id,provider_id,job_id,path,state,source_url,photographer,download_url,created_at) VALUES(?,?,?,'','ready','https://www.pexels.com/photo/test','Test','https://images.pexels.com/test','now')",(ident,str(self.count),job['id']))
  return ident
 def cleanup(self,asset):pass
class Executor:
 mode='success';calls=0;close_fail=False
 def __init__(self,manager):self.manager=manager
 async def publish(self,job,asset,checkpoint,before,confirmed):
  type(self).calls+=1;await checkpoint();await before()
  if self.mode=='unknown':raise TimeoutError('fixture')
  if self.mode=='reject':
   from app.studio_worker import PostingRejected
   raise PostingRejected('fixture explicit rejection')
  result={'published':1,'verification':'instagram_dialog','confirmed_at':datetime.now(timezone.utc).isoformat()};await confirmed(result);await confirmed(result);return result
 async def close(self,profile,token):
  if self.close_fail:raise RuntimeError('fixture close unconfirmed')
 async def disconnect(self):pass
class Tests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.db=DB(Path(self.temp.name)/'test.db');self.service=Service(self.db)
  with self.db.write() as c:
   c.execute('CREATE TABLE browser_operation_leases(profile_id TEXT PRIMARY KEY,owner_user_id TEXT,operation_type TEXT,entity_id TEXT,lease_token TEXT)')
   c.execute('CREATE TABLE posting_account_snapshots(owner_user_id TEXT,profile_id TEXT,username TEXT,checked_at TEXT)')
   c.execute("INSERT INTO posting_account_snapshots VALUES('owner','window','correct_user','now')")
  self.provider=Provider(self.db);Executor.calls=0;Executor.mode='success';Executor.close_fail=False
  self.manager=PostingManager(self.service,None,provider=self.provider,executor_factory=Executor);self.manager.recover()
 def tearDown(self):self.temp.cleanup()
 def start_body(self,ids):
  return {'action':'start','job_ids':ids,'reviewed':[{k:self.manager.get('owner',ident)[k] for k in ('id','caption','asset_id','profile_id','expected_username','queue_revision')} for ident in ids]}
 async def prepared(self,count=1,key='request-00001'):
  ids=self.manager.generate('owner',{'request_id':key,'theme':'forest','caption':'Original 文案','count':count})['job_ids']
  for ident in ids:await self.manager._prepare(self.manager.get('owner',ident));await self.manager.command('owner',{'action':'assign','job_id':ident,'profile_id':'window','expected_username':'correct_user'})
  return ids
 async def test_generate_idempotent_caption_exact_and_bounds(self):
  ids=await self.prepared(2);again=self.manager.generate('owner',{'request_id':'request-00001','theme':'forest','caption':'Original 文案','count':2})['job_ids'];self.assertEqual(ids,again);self.assertEqual(self.provider.count,2)
  with self.assertRaises(ConflictError):self.manager.generate('owner',{'request_id':'request-00001','theme':'forest','caption':'changed','count':2})
 async def test_success_receipt_once_cleanup_hidden_and_totals(self):
  ident=(await self.prepared())[0];await self.manager.command('owner',self.start_body([ident]));await self.manager._execute(self.manager.get('owner',ident));snap=self.manager.snapshot('owner');self.assertEqual(snap['jobs'],[]);self.assertEqual(snap['totals']['success'],1);self.assertEqual(snap['totals']['today_success'],1);self.assertEqual(Executor.calls,1)
  self.assertEqual(self.manager.active_ids(),set())
 async def test_unknown_holds_and_cannot_restart_cancel_or_other_window_task(self):
  ids=await self.prepared(2);await self.manager.command('owner',self.start_body([ids[0]]));Executor.mode='unknown';await self.manager._execute(self.manager.get('owner',ids[0]));self.assertEqual(self.manager.get('owner',ids[0])['status'],'needs_review')
  for body in [self.start_body([ids[0]]),{'action':'cancel','job_id':ids[0]},self.start_body([ids[1]])]:
   with self.assertRaises(ConflictError):await self.manager.command('owner',body)
  self.manager.recover();self.assertIn(ids[0],self.manager.active_ids());self.assertEqual(self.manager.snapshot('owner')['totals']['success'],0)
 async def test_close_failure_retains_receipt_lease_and_retries_without_publish(self):
  ident=(await self.prepared())[0];await self.manager.command('owner',self.start_body([ident]));Executor.close_fail=True;await self.manager._execute(self.manager.get('owner',ident));job=self.manager.get('owner',ident);self.assertEqual(job['status'],'cleanup_pending');self.assertTrue(job['lease_token']);self.assertEqual(self.manager.snapshot('owner')['totals']['success'],1)
  Executor.close_fail=False;await self.manager._cleanup(job);self.assertEqual(self.manager.get('owner',ident)['status'],'completed');self.assertEqual(Executor.calls,1)
 async def test_window_race_and_account_mismatch(self):
  ids=await self.prepared(2)
  with self.assertRaises(ValidationError):await self.manager.command('owner',{'action':'assign','job_id':ids[0],'profile_id':'window','expected_username':'wrong'})
  await self.manager.command('owner',self.start_body(ids));Executor.mode='unknown';await asyncio.gather(*(self.manager._execute(self.manager.get('owner',i)) for i in ids));self.assertEqual(Executor.calls,1);self.assertEqual(sum(self.manager.get('owner',i)['status']=='queued' for i in ids),1)
 async def test_restart_submit_is_unknown_not_ready(self):
  ident=(await self.prepared())[0]
  with self.db.write() as c:c.execute("UPDATE posting_jobs SET status='submitting',attempt_id='once' WHERE id=?",(ident,))
  self.manager.recover();self.assertEqual(self.manager.get('owner',ident)['status'],'needs_review')
 async def test_failed_not_success_and_cancel_does_not_remove_registry(self):
  ident=(await self.prepared())[0];await self.manager.command('owner',self.start_body([ident]));Executor.mode='reject';await self.manager._execute(self.manager.get('owner',ident));self.assertEqual(self.manager.snapshot('owner')['totals']['failed'],1);self.assertEqual(self.manager.snapshot('owner')['totals']['success'],0)
  await self.manager.command('owner',{'action':'cancel','job_id':ident})
  with self.db.read() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM posting_assets').fetchone()[0],1)
 async def test_global_reservation_across_owners_and_recoverable_only_cleanup(self):
  one=(await self.prepared())[0];p=PexelsProvider(self.db,lambda:'fixture-not-a-secret')
  two=self.manager.generate('other',{'request_id':'other-request-1','theme':'forest','caption':'caption','count':1})['job_ids'][0]
  item={'provider_id':'1','source_url':'https://www.pexels.com/photo/test','photographer':'Test','download_url':'https://images.pexels.com/test'}
  self.assertIsNone(p.reserve(two,item))
  with self.assertRaises(ValidationError):p.cleanup({'id':'foreign','path':'/etc/hosts'})
 async def test_timezone_date_boundaries(self):
  with self.db.write() as c:c.execute("INSERT INTO posting_receipts VALUES('past','owner','w','u','','2000-01-01T00:00:00+00:00','2000-01-01','{}')")
  self.assertEqual(self.manager.snapshot('owner','Asia/Shanghai')['totals']['today_success'],0)
  self.assertEqual(self.manager.snapshot('owner')['totals']['success'],1)
  with self.assertRaises(ValidationError):self.manager.snapshot('owner','bad/zone')
 async def test_unconfigured_no_fake_task(self):
  self.manager.provider=PexelsProvider(self.db,lambda:'')
  with self.assertRaises(ValidationError):self.manager.generate('owner',{'request_id':'new-request-1','theme':'a','caption':'b','count':1})
  self.assertFalse(self.manager.snapshot('owner')['credentials']['pexels_configured'])
if __name__=='__main__':unittest.main()

class ConcurrencyAndScale(Tests):
 async def test_parallel_generation_same_request_is_one_batch(self):
  body={'request_id':'concurrent-batch','theme':'forest','caption':'exact','count':3}
  results=await asyncio.gather(*(asyncio.to_thread(self.manager.generate,'owner',body) for _ in range(8)))
  self.assertTrue(all(r==results[0] for r in results))
  with self.db.read() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM posting_jobs').fetchone()[0],3)
 async def test_receipt_date_query_uses_index_at_scale(self):
  with self.db.write() as c:
   c.executemany('INSERT INTO posting_receipts VALUES(?,?,?,?,?,?,?,?)',[(f'old-{i}','owner','window','user','','2000-01-01T00:00:00+00:00','2000-01-01','{}') for i in range(10000)])
   plan=' '.join(str(tuple(r)) for r in c.execute('EXPLAIN QUERY PLAN SELECT COUNT(*) FROM posting_receipts WHERE owner_user_id=? AND confirmed_at>=? AND confirmed_at<?',('owner','2026-01-01','2027-01-01')))
  self.assertIn('posting_receipts_owner_date',plan)
  self.assertEqual(self.manager.snapshot('owner')['totals']['success'],10000)
 async def test_request_underscore_has_no_like_wildcard(self):
  a=self.manager.generate('owner',{'request_id':'batch_000001','theme':'a','caption':'b','count':1})
  b=self.manager.generate('owner',{'request_id':'batchX000001','theme':'a','caption':'b','count':1})
  self.assertNotEqual(a,b)

class UnknownResolution(Tests):
 async def test_unknown_resolution_requires_explicit_no_repost_confirmation(self):
  ident=(await self.prepared())[0];await self.manager.command('owner',self.start_body([ident]));Executor.mode='unknown';await self.manager._execute(self.manager.get('owner',ident))
  with self.assertRaises(ConflictError):await self.manager.command('owner',{'action':'close_unknown','job_id':ident})
  await self.manager.command('owner',{'action':'close_unknown','job_id':ident,'confirm_no_repost':True});await self.manager.tasks[ident]
  self.assertEqual(self.manager.get('owner',ident)['status'],'unknown_closed');self.assertEqual(Executor.calls,1)
  self.assertEqual(self.manager.snapshot('owner')['totals']['success'],0);self.assertEqual(self.manager.snapshot('owner')['totals']['failed'],0)
  with self.assertRaises(ConflictError):await self.manager.command('owner',self.start_body([ident]))
 async def test_unknown_close_failure_keeps_executor_and_token(self):
  ident=(await self.prepared())[0];await self.manager.command('owner',self.start_body([ident]));Executor.mode='unknown';await self.manager._execute(self.manager.get('owner',ident));Executor.close_fail=True
  await self.manager.command('owner',{'action':'close_unknown','job_id':ident,'confirm_no_repost':True});await self.manager.tasks[ident]
  self.assertEqual(self.manager.get('owner',ident)['status'],'needs_review');self.assertTrue(self.manager.get('owner',ident)['lease_token']);self.assertIn(ident,self.manager.executors)

class ReviewedContentRace(Tests):
 async def test_start_rejects_caption_changed_since_review(self):
  ident=(await self.prepared())[0];review=self.start_body([ident]);await self.manager.command('owner',{'action':'edit','job_id':ident,'caption':'different'})
  with self.assertRaises(ConflictError):await self.manager.command('owner',review)
  self.assertEqual(self.manager.get('owner',ident)['status'],'ready');self.assertEqual(Executor.calls,0)
 async def test_start_rejects_asset_changed_since_review(self):
  ident=(await self.prepared())[0];review=self.start_body([ident]);review['reviewed'][0]['asset_id']='not-the-reviewed-asset'
  with self.assertRaises(ConflictError):await self.manager.command('owner',review)
 async def test_cancelled_cleanup_joins_close_before_releasing(self):
  ident=(await self.prepared())[0];await self.manager.command('owner',self.start_body([ident]));Executor.close_fail=True;await self.manager._execute(self.manager.get('owner',ident));job=self.manager.get('owner',ident);started=asyncio.Event();finish=asyncio.Event()
  class SlowClose(Executor):
   async def close(self,profile,token):started.set();await finish.wait()
  task=asyncio.create_task(self.manager._cleanup(job,SlowClose(self.manager)));await started.wait();task.cancel();await asyncio.sleep(0);task.cancel();self.assertTrue(self.manager.get('owner',ident)['lease_token']);finish.set()
  await asyncio.gather(task,return_exceptions=True)
  # Cancellation leaves durable state for an idempotent cleanup retry, never frees early.
  self.assertTrue(self.manager.get('owner',ident)['lease_token']);Executor.close_fail=False;await self.manager._cleanup(self.manager.get('owner',ident),Executor(self.manager));self.assertFalse(self.manager.get('owner',ident)['lease_token'])

class AuditFixes(Tests):
 async def test_malformed_start_ids(self):
  for ids in [[{}],[[]],[1],['x']]:
   with self.assertRaises(ValidationError):await self.manager.command('owner',{'action':'start','job_ids':ids,'reviewed':[{'id':[]} ]})
 async def test_actor_reset_only_before_attempt(self):
  ident=(await self.prepared())[0]
  with self.db.write() as c:c.execute("UPDATE posting_jobs SET status='failed',expected_actor_id='old' WHERE id=?",(ident,))
  await self.manager.command('owner',{'action':'assign','job_id':ident,'profile_id':'window','expected_username':'correct_user'})
  self.assertEqual(self.manager.get('owner',ident)['expected_actor_id'],'')
  with self.db.write() as c:c.execute("UPDATE posting_jobs SET attempt_id='once' WHERE id=?",(ident,))
  with self.assertRaises(ConflictError):await self.manager.command('owner',{'action':'assign','job_id':ident,'profile_id':'window','expected_username':'correct_user'})
 async def test_acquire_crash_and_missing_row_recovery(self):
  ident=(await self.prepared())[0]
  await self.manager.command('owner',self.start_body([ident]))
  token=await self.service.acquire_browser_lease_async('owner','window',operation_type='posting',entity_id=ident)
  self.manager.recover();job=self.manager.get('owner',ident)
  self.assertEqual(job['lease_token'],token);self.assertEqual(job['status'],'needs_review')
  with self.db.write() as c:c.execute('DELETE FROM browser_operation_leases')
  self.manager.recover();self.manager.assert_lease(self.manager.get('owner',ident))
 async def test_corrupt_lease_never_closes(self):
  ident=(await self.prepared())[0]
  token=await self.service.acquire_browser_lease_async('owner','window',operation_type='posting',entity_id=ident)
  with self.db.write() as c:c.execute("UPDATE posting_jobs SET lease_token=?,status='failed' WHERE id=?",(token,ident))
  class MustNotClose(Executor):
   async def close(self,*args):raise AssertionError('must not reach close')
  for column,value in [('owner_user_id','other'),('entity_id','other'),('operation_type','nurture')]:
   with self.db.write() as c:c.execute('UPDATE browser_operation_leases SET '+column+'=?',(value,))
   job=self.manager.get('owner',ident)
   with self.assertRaises(ConflictError):self.manager.assert_lease(job)
   await self.manager._cleanup(job,MustNotClose(self.manager))
   self.assertEqual(self.manager.get('owner',ident)['lease_token'],token)
   with self.db.write() as c:c.execute("UPDATE browser_operation_leases SET owner_user_id='owner',entity_id=?,operation_type='posting'",(ident,))
 async def test_paging_full_access_and_backoff_fairness(self):
  with self.db.write() as c:
   for i in range(250):
    c.execute("INSERT INTO posting_jobs(id,owner_user_id,request_key,theme,caption,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(str(i).zfill(4),'owner',str(i),'x','x','cleanup_pending' if i<150 else 'preparing',str(i).zfill(4),'now'))
  self.manager.retry_after={str(i).zfill(4):asyncio.get_running_loop().time()+30 for i in range(150)}
  self.assertEqual(self.manager._runnable_jobs()[0]['id'],'0150')
  cursor=None;seen=[]
  while True:
   page=self.manager.snapshot('owner',cursor=cursor,limit=37);seen.extend(j['id'] for j in page['jobs'])
   cursor=page['pagination']['next_cursor']
   if not cursor:break
  self.assertEqual(len(seen),250);self.assertEqual(len(set(seen)),250)

class AcquisitionAudit(Tests):
 async def test_aborted_acquisition_does_not_release_foreign_entity(self):
  ident=(await self.prepared())[0]
  await self.manager.command('owner',self.start_body([ident]))
  # A stale non-queued job now exits before admission. Simulate corruption
  # during acquisition instead, so the queued-to-running CAS still fails.
  original=self.service.acquire_browser_lease_async
  async def corrupt(owner,profile,**kw):
   token=await original(owner,profile,**kw)
   with self.db.write() as c:
    c.execute("UPDATE browser_operation_leases SET entity_id='sibling'")
    c.execute("UPDATE posting_jobs SET status='ready' WHERE id=?",(ident,))
   return token
  self.service.acquire_browser_lease_async=corrupt
  await self.manager._execute(self.manager.get('owner',ident))
  with self.db.read() as c:self.assertEqual(c.execute('SELECT entity_id FROM browser_operation_leases').fetchone()[0],'sibling')
 async def test_naive_success_timestamp_is_not_counted(self):
  ident=(await self.prepared())[0];await self.manager.command('owner',self.start_body([ident]))
  class Naive(Executor):
   async def publish(self,job,asset,checkpoint,before,confirmed):
    await checkpoint();await before();await confirmed({'published':1,'verification':'instagram_dialog','confirmed_at':'2026-10-02T00:00:00'})
  self.manager.executor_factory=Naive
  await self.manager._execute(self.manager.get('owner',ident))
  self.assertEqual(self.manager.get('owner',ident)['status'],'needs_review')
  self.assertEqual(self.manager.snapshot('owner')['totals']['success'],0)

class UninitializedLedger(unittest.TestCase):
 def test_active_ids_before_recover_is_read_only(self):
  with tempfile.TemporaryDirectory() as folder:
   db=DB(Path(folder)/'new.db');manager=PostingManager(Service(db),None,provider=Provider(db))
   self.assertEqual(manager.active_ids(),set())
   with db.read() as c:self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='posting_jobs'").fetchone())
 def test_active_ids_does_not_swallow_corrupt_ledger(self):
  with tempfile.TemporaryDirectory() as folder:
   db=DB(Path(folder)/'new.db')
   with db.write() as c:c.execute('CREATE TABLE posting_jobs(wrong_column TEXT)')
   manager=PostingManager(Service(db),None,provider=Provider(db))
   with self.assertRaises(sqlite3.OperationalError):manager.active_ids()
