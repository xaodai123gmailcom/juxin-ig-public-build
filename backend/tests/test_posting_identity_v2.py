import unittest,tempfile,asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from test_posting_workflow_v2 import DB
from app.posting_schema import initialize_posting_schema
from app.posting_executor import PostingExecutor
from app.errors import ValidationError
from app.standalone_nurture import OWN_LINK,OWN_METRICS
class Context:
 def __init__(self):self.uid='123456789';self.name='expected';self.own=True
 async def cookies(self,url):return [{'name':'ds_user_id','value':self.uid}]
 async def new_page(self):return Page(self)
class Page:
 def __init__(self,context):self.context=context
 async def goto(self,*a,**k):pass
 async def close(self):pass
 async def evaluate(self,script,*args):
  if script==OWN_LINK:return self.context.name
  if script==OWN_METRICS:return {} if self.context.own else None
class Worker:
 def __init__(self,*a):self.page=Page(Context())
 async def connect(self,*a,**k):pass
 @asynccontextmanager
 async def _destructive_action_lease(self):yield
class Browser:
 change=False;uploaded=False;submitted=False
 def __init__(self,worker,checkpoint,before):self.worker=worker;self.before=before
 async def publish(self,assets,caption,location):
  await self.account_snapshot({'username':self.worker.page.context.name});type(self).uploaded=True
  if self.change:self.worker.page.context.uid='999999999'
  await self.before('share');type(self).submitted=True;return {}
class Tests(unittest.IsolatedAsyncioTestCase):
 async def run_case(self,expected='expected',change=False,own=True):
  with tempfile.TemporaryDirectory() as folder:
   db=DB(Path(folder)/'db')
   with db.write() as c:
    initialize_posting_schema(c);c.execute("INSERT INTO posting_jobs(id,owner_user_id,request_key,theme,caption,status,created_at,updated_at) VALUES('j','o','key','a','b','running','now','now')")
   manager=SimpleNamespace(db=db,bitbrowser=None);job={'id':'j','profile_id':'w','expected_username':expected,'caption':'caption','expected_actor_id':''};Browser.change=change;Browser.uploaded=False;Browser.submitted=False
   class TestWorker(Worker):
    def __init__(self,*a):super().__init__();self.page.context.own=own
   async def noop(*a):pass
   with patch('app.posting_executor.PlaywrightWorker',TestWorker),patch('app.posting_executor.StudioBrowser',Browser):
    media=Path(folder)/'media.jpg';media.write_bytes(b'synthetic')
    import hashlib
    await PostingExecutor(manager).publish(job,{'path':str(media),'render_sha256':hashlib.sha256(b'synthetic').hexdigest()},noop,noop,noop)
 async def test_wrong_user_blocks_before_upload(self):
  with self.assertRaises(ValidationError):await self.run_case(expected='wrong')
  self.assertFalse(Browser.uploaded);self.assertFalse(Browser.submitted)
 async def test_cookie_swap_blocks_share(self):
  with self.assertRaises(ValidationError):await self.run_case(change=True)
  self.assertTrue(Browser.uploaded);self.assertFalse(Browser.submitted)
 async def test_missing_own_profile_proof_blocks_upload(self):
  with self.assertRaises(ValidationError):await self.run_case(own=False)
  self.assertFalse(Browser.uploaded)
 async def test_exact_own_user_cookie_header_all_required(self):
  await self.run_case();self.assertTrue(Browser.submitted)

class FileIntegrity(unittest.IsolatedAsyncioTestCase):
 async def test_changed_bytes_rejected_before_connect_or_upload(self):
  with tempfile.TemporaryDirectory() as directory:
   f=Path(directory)/'asset.jpg';f.write_bytes(b'tampered')
   manager=SimpleNamespace(bitbrowser=None)
   async def noop(*a):pass
   with patch('app.posting_executor.PlaywrightWorker') as worker:
    with self.assertRaises(ValidationError):await PostingExecutor(manager).publish({}, {'path':str(f),'render_sha256':'expected-other-hash'},noop,noop,noop)
    worker.assert_not_called()
