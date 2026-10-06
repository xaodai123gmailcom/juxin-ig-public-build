import unittest,io,json,tempfile,hashlib
from pathlib import Path
from urllib.error import HTTPError
from test_posting_workflow_v2 import DB
from app.posting_schema import initialize_posting_schema
from app.posting_pexels import PexelsProvider,MAX_BYTES
from app.errors import ValidationError
class Response:
 def __init__(self,raw,headers=None):self.stream=io.BytesIO(raw);self.headers=headers or {}
 def read(self,n=-1):return self.stream.read(n)
 def __enter__(self):return self
 def __exit__(self,*args):pass
class Opener:
 def __init__(self,responses):self.responses=responses;self.requests=[]
 def open(self,request,timeout):
  self.requests.append(request)
  result=self.responses.pop(0)
  if isinstance(result,Exception):raise result
  return result
class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.db=DB(Path(self.temp.name)/'db')
  with self.db.write() as c:initialize_posting_schema(c)
 def tearDown(self):self.temp.cleanup()
 def job(self,ident='j'):
  with self.db.write() as c:c.execute("INSERT INTO posting_jobs(id,owner_user_id,request_key,theme,caption,created_at,updated_at) VALUES(?, 'o',?,'forest','caption','now','now')",(ident,ident))
 def photo(self,ident='1'):
  return {'id':int(ident),'src':{'large2x':'https://images.pexels.com/photo.jpg'},'url':'https://www.pexels.com/photo/'+ident,'photographer':'Author'}
 def image(self):
  from PIL import Image
  b=io.BytesIO();Image.new('RGB',(400,400),(30,80,120)).save(b,'JPEG');return b.getvalue()
 def test_cache_and_no_secret_in_cache(self):
  opener=Opener([Response(json.dumps({'photos':[self.photo()]}).encode())]);p=PexelsProvider(self.db,lambda:'synthetic-fixture-credential',opener=opener)
  self.assertEqual(p.search('Forest'),p.search('Forest'));self.assertEqual(len(opener.requests),1)
  with self.db.read() as c:self.assertNotIn('synthetic-fixture',c.execute('SELECT response_json FROM posting_api_cache').fetchone()[0])
 def test_quota_local_guard_and_429_backoff(self):
  err=HTTPError('https://api.pexels.com/',429,'quota',{'Retry-After':'120','X-Ratelimit-Reset':'2000'},None);opener=Opener([err]);p=PexelsProvider(self.db,lambda:'fixture',opener=opener,clock=lambda:1000)
  with self.assertRaises(ValidationError):p.search('forest')
  with self.assertRaises(ValidationError):p.search('different')
  self.assertEqual(len(opener.requests),1)
 def test_download_stream_hash_no_key_on_media_and_recoverable_cleanup(self):
  self.job();raw=self.image();opener=Opener([Response(raw)]);p=PexelsProvider(self.db,lambda:'fixture',opener=opener);asset=p.reserve('j',{'provider_id':'1','source_url':'https://www.pexels.com/photo/1','photographer':'A','download_url':'https://images.pexels.com/a.jpg'})
  with self.db.read() as c:a=dict(c.execute('SELECT * FROM posting_assets WHERE id=?',(asset,)).fetchone())
  p.download(a)
  self.assertIsNone(opener.requests[0].get_header('Authorization'))
  with self.db.read() as c:a=dict(c.execute('SELECT * FROM posting_assets WHERE id=?',(asset,)).fetchone())
  self.assertEqual(a['sha256'],hashlib.sha256(raw).hexdigest());self.assertTrue(Path(a['path']).is_file())
  with self.db.write() as c:c.execute("UPDATE posting_jobs SET status='cleanup_pending'");c.execute("UPDATE posting_assets SET state='used'")
  p.cleanup(a)
  with self.db.read() as c:a=dict(c.execute('SELECT * FROM posting_assets WHERE id=?',(asset,)).fetchone())
  self.assertEqual(a['state'],'used');self.assertTrue(Path(a['recovery_path']).is_file());self.assertEqual(a['path'],'')
 def test_identical_file_different_provider_id_blocked_globally(self):
  p=PexelsProvider(self.db,lambda:'fixture',opener=Opener([Response(self.image()),Response(self.image())]))
  for i in [1,2]:
   self.job(str(i));ident=p.reserve(str(i),{'provider_id':str(i),'source_url':'https://www.pexels.com/photo/'+str(i),'photographer':'A','download_url':'https://images.pexels.com/a.jpg'})
   with self.db.read() as c:a=dict(c.execute('SELECT * FROM posting_assets WHERE id=?',(ident,)).fetchone())
   if i==1:p.download(a)
   else:
    with self.assertRaises(ValidationError):p.download(a)
  with self.db.read() as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM posting_assets WHERE state='ready'").fetchone()[0],1)
 def test_download_size_bound(self):
  self.job();p=PexelsProvider(self.db,lambda:'fixture',opener=Opener([Response(b'x'*(MAX_BYTES+1))]));ident=p.reserve('j',{'provider_id':'1','source_url':'https://www.pexels.com/photo/1','photographer':'A','download_url':'https://images.pexels.com/a.jpg'})
  with self.db.read() as c:a=dict(c.execute('SELECT * FROM posting_assets WHERE id=?',(ident,)).fetchone())
  with self.assertRaises(ValidationError):p.download(a)
  self.assertFalse(list(p.root.glob('*.jpg')))
 def test_validate_explicit_statuses_and_no_automatic_probe(self):
  for code,expected in [(401,'invalid'),(403,'forbidden'),(429,'rate_limited'),(500,'unconfirmed')]:
   
   with self.db.write() as c:c.execute('DELETE FROM posting_api_usage');c.execute('DELETE FROM posting_api_backoff')
   o=Opener([HTTPError('https://api.pexels.com',code,'test',{},None)]);p=PexelsProvider(self.db,lambda:'fixture',opener=o)
   self.assertTrue(p.configured());self.assertEqual(o.requests,[]);self.assertEqual(p.validate()['status'],expected)
   self.assertEqual(o.requests[0].full_url,'https://api.pexels.com/v1/curated?per_page=1')
 def test_external_hosts_and_redirects_fail_closed(self):
  p=PexelsProvider(self.db,lambda:'fixture',opener=Opener([Response(json.dumps({'photos':[dict(self.photo(),src={'large2x':'https://attacker.invalid/a.jpg'})]}).encode())]))
  self.assertEqual(p.search('a'),[])
  self.assertFalse(p._allowed('https://images.pexels.com.evil.invalid/a','images.pexels.com'))
if __name__=='__main__':unittest.main()
