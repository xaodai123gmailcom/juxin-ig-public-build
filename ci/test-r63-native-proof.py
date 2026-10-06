import copy,hashlib,importlib.util,tempfile,unittest,struct,zlib
from pathlib import Path
spec=importlib.util.spec_from_file_location('native_proof',Path(__file__).with_name('r63_native_proof.py'))
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)

class NativeStagingContract(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  files={'host_source':'desktop/src/embedded-browser.ts','host_compiled':'dist-electron/embedded-browser.js','fixture':'desktop/tests/nurture-cleanup-native-r63.cjs'}
  for key,name in files.items():p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(key)
  self.proof={'schema':1,'gate':'r63-nurture-cleanup-native','verified':True,'required_mode':True,'native_runtime':True,'cleanup_verified':True,'synthetic_offline':True,'platform':'win32','source_commit':'a'*40,'electron':'fixture','chromium':'fixture','external_actions':[],'offline_sessions':[{'external_requests':[]}],'scenarios':dict.fromkeys(mod.SCENARIOS,True),'observations':[{'scenario':x} for x in mod.SCENARIOS],'sha256':{k:hashlib.sha256(k.encode()).hexdigest() for k in files}}
  def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
  png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',900,700,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\0'*2700)*700))+chunk(b'IEND',b'')
  output=self.root/'installer-output';output.mkdir();(output/'r63-nurture-cleanup-native.png').write_bytes(png)
  self.proof['screenshot_capture']={'completed':True,'attempts':[{'atMs':0,'native':{'windowVisible':True,'minimized':False,'paneVisible':True,'paneBounds':{'x':20,'y':20,'width':900,'height':700},'pageBounds':{'x':0,'y':0,'width':900,'height':700}},'renderer':{'ready':'complete','visibility':'visible','painted':True,'width':900,'height':700}}]};self.proof['screenshot']={'file':'r63-nurture-cleanup-native.png','pixel_size':{'width':900,'height':700},'viewport':{'width':900,'height':700},'bytes':len(png),'sha256':hashlib.sha256(png).hexdigest(),'publish_disabled':True}
 def validate(self,p):return mod.validate_native(p,self.root,'a'*40)
 def test_complete_bound_proof_passes(self):self.assertIs(self.validate(self.proof),self.proof)
 def test_each_required_flag_platform_runtime_identity_and_hash_fails_closed(self):
  for key in ('verified','required_mode','native_runtime','cleanup_verified','synthetic_offline','platform','source_commit','electron','chromium'):
   with self.subTest(key=key):
    p=copy.deepcopy(self.proof);p.pop(key)
    with self.assertRaises(RuntimeError):self.validate(p)
  for key in self.proof['sha256']:
   p=copy.deepcopy(self.proof);p['sha256'][key]='0'*64
   with self.assertRaises(RuntimeError):self.validate(p)
 def test_missing_or_extra_scenario_and_missing_observation_rejected(self):
  for key in mod.SCENARIOS:
   p=copy.deepcopy(self.proof);p['scenarios'].pop(key)
   with self.assertRaises(RuntimeError):self.validate(p)
  for field in ('scenarios','observations'):
   p=copy.deepcopy(self.proof);p[field]={} if field=='scenarios' else []
   with self.assertRaises(RuntimeError):self.validate(p)
 def test_external_activity_or_empty_sessions_rejected(self):
  for key,value in [('external_actions',['publish']),('offline_sessions',[]),('offline_sessions',[{'external_requests':['https://example.com']}])]:
   p=copy.deepcopy(self.proof);p[key]=value
   with self.assertRaises(RuntimeError):self.validate(p)
 def test_missing_or_changed_capture_rejected(self):
  for key,value in [('sha256','0'*64),('bytes',1),('pixel_size',{'width':1,'height':1}),('publish_disabled',False),('file','elsewhere.png')]:
   p=copy.deepcopy(self.proof);p['screenshot'][key]=value
   with self.assertRaises(RuntimeError):self.validate(p)
  p=copy.deepcopy(self.proof);p['screenshot_capture']['completed']=False
  with self.assertRaises(RuntimeError):self.validate(p)
 def test_capture_uses_actual_attempt_list_and_successful_real_paint(self):
  for attempts in (1,[],[{'renderer':{'painted':True}}]):
   p=copy.deepcopy(self.proof);p['screenshot_capture']['attempts']=attempts
   with self.assertRaises(RuntimeError):self.validate(p)
  for group,key,value in [('renderer','painted',False),('renderer','visibility','hidden'),('native','paneVisible',False),('native','minimized',True)]:
   p=copy.deepcopy(self.proof);p['screenshot_capture']['attempts'][-1][group][key]=value
   with self.assertRaises(RuntimeError):self.validate(p)
if __name__=='__main__':unittest.main()

