"""Fail-closed native recovery release receipt checks."""
import copy,hashlib,runpy,tempfile,unittest
from pathlib import Path
M=runpy.run_path(str(Path(__file__).with_name('r64_recovery_ui_proof.py')))
class RecoveryUiProofTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.commit='a'*40
  for name in M['SOURCE_REQUIRED']:
   p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('source:'+name)
  png=b'\x89PNG\r\n\x1a\n'+b'png_fixture'*10
  for name in M['CAPTURES']:(self.root/name).write_bytes(png)
  self.proof={'schema':2,'gate':'r64-recovery-ui-native','verified':True,'synthetic_offline':True,'native_runtime':True,'required_mode':True,'cleanup_verified':True,'platform':'win32','windows_release_status':'passed','source_commit':self.commit,'github_sha':self.commit,'electron':'44.4.5','chromium':'fixture','headless':False,'native_window':{'visible':True,'offscreen':False,'content_size':[1440,1050]},'scope':{'production_app':True,'production_handlers':True,'native_input':True,'installed_core':False,'user_database':False,'live_instagram':False,'share':False,'os_dialog_automation':False},'confirmation_mode':'production-window.confirm/controlled-response','external_requests':[],'external_actions':[],'renderer_errors':[],'unexpected_endpoints':[],'scenarios':dict.fromkeys(M['SCENARIOS'],True),'native_input':[{'trusted':True}]*20,'source_sha256':{n:hashlib.sha256((self.root/n).read_bytes()).hexdigest() for n in M['SOURCE_REQUIRED']},'start_intents':[],'stop_intents':[{'body':{'action':'stop_cleanup_collection','job_id':'held-r64','task_id':'86feb8fc-f2a6-404f-9459-11dd3f884103','version':7}}]*2,'successful_stops':1,'cleanup_successes':1,'screenshots':[{'file':n,'bytes':len(png),'native_visible':True,'renderer_visibility':'visible','size':{'width':1440,'height':1050},'sha256':hashlib.sha256(png).hexdigest()} for n in M['CAPTURES']],'timing':{'elapsed_ms':1200,'overall_limit_ms':150000}}
 def tearDown(self):self.tmp.cleanup()
 def validate(self,p=None):return M['validate_recovery_ui'](self.proof if p is None else p,self.root,self.commit,self.root)
 def test_valid_receipt_passes(self):self.assertEqual(self.validate(),self.proof)
 def test_every_top_level_evidence_field_is_required(self):
  for key in self.proof:
   p=copy.deepcopy(self.proof);del p[key]
   with self.subTest(key=key),self.assertRaises((RuntimeError,FileNotFoundError)):self.validate(p)
 def test_every_scenario_and_production_source_required(self):
  for key in self.proof['scenarios']:
   p=copy.deepcopy(self.proof);p['scenarios'][key]=False
   with self.assertRaises(RuntimeError):self.validate(p)
  for key in M['SOURCE_REQUIRED']:
   p=copy.deepcopy(self.proof);p['source_sha256'].pop(key)
   with self.assertRaises(RuntimeError):self.validate(p)
 def test_no_linux_synthetic_input_external_activity_or_any_start(self):
  changes=(('platform','linux'),('headless',True),('native_window',{'visible':False,'offscreen':True,'content_size':[1440,1050]}),('source_commit','b'*40),('required_mode',False),('external_actions',['open']),('native_input',[{'trusted':False}]*20),('start_intents',[{'body':{'action':'start'}}]),('timing',{'elapsed_ms':150001,'overall_limit_ms':150000}))
  for key,value in changes:
   p=copy.deepcopy(self.proof);p[key]=value
   with self.assertRaises(RuntimeError):self.validate(p)
 def test_wrong_target_revision_capture_or_source_fails(self):
  p=copy.deepcopy(self.proof);p['stop_intents'][0]['body']['version']=6
  with self.assertRaises(RuntimeError):self.validate(p)
  p=copy.deepcopy(self.proof);p['stop_intents'][0]['body']['task_id']='different'
  with self.assertRaises(RuntimeError):self.validate(p)
  (self.root/M['CAPTURES'][0]).write_bytes(b'wrong')
  with self.assertRaises(RuntimeError):self.validate()
if __name__=='__main__':unittest.main()

