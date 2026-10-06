"""Offline strict release receipt regression checks."""
import ast,copy,hashlib,json,runpy,tempfile,unittest
from pathlib import Path
M=runpy.run_path(str(Path(__file__).with_name('r64_crop_proof.py')))
class CropProofTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.commit='a'*40
  for name in M['SOURCE_FILES']:
   p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture')
  (self.root/'backend/app/instagram_crop_dom.py').write_text("CROP_BUTTON = 'probe'")
  self.png=b'\x89PNG\r\n\x1a\nfixture';(self.root/'r64-crop-icon-native.png').write_bytes(self.png)
  self.proof={'schema':1,'verified':True,'offline':True,'synthetic_offline':True,'live_accounts_tested':False,'platform':'win32','source_commit':self.commit,'baseline':False,'electron':'44.4.5','chromium':'fixture','source_hashes':{n:hashlib.sha256((self.root/n).read_bytes()).hexdigest() for n in M['SOURCE_FILES']},'probe_sha256':hashlib.sha256(b'probe').hexdigest(),'external_requests':[],'emulated_viewport':{'width':1274,'height':717},'capture':{'file':'r64-crop-icon-native.png','sha256':hashlib.sha256(self.png).hexdigest()},'scenarios':[]}
  for name in M['VARIANTS']:
   allowed=name in M['ALLOWED'];method=('crop_label' if name in {'title','aria','labelled_precedence'} else 'crop_corner_icon') if allowed else None
   self.proof['scenarios'].append({'variant':name,'clicked':allowed,'trusted_clicks':[True]*3 if allowed else [],'probe':{'count':2 if name=='duplicate' else int(allowed),'method':method}})
 def tearDown(self):self.tmp.cleanup()
 def validate(self,p=None):return M['validate_crop'](self.proof if p is None else p,self.root,self.commit,self.root)
 def test_complete_same_source_windows_receipt(self):self.assertEqual(self.validate(),self.proof)
 def test_every_identity_and_isolation_field_is_mandatory(self):
  for key in ('schema','verified','offline','synthetic_offline','live_accounts_tested','platform','source_commit','baseline','electron','chromium','source_hashes','probe_sha256','external_requests','emulated_viewport','capture'):
   with self.subTest(key=key):
    p=copy.deepcopy(self.proof);p.pop(key)
    with self.assertRaises((RuntimeError,FileNotFoundError)):self.validate(p)
 def test_missing_extra_reordered_or_altered_cases_fail(self):
  for mutation in (lambda p:p['scenarios'].pop(),lambda p:p['scenarios'].append(p['scenarios'][0]),lambda p:p['scenarios'].reverse(),lambda p:p['scenarios'][0].update(trusted_clicks=[True,True]),lambda p:p['scenarios'][-1].update(clicked=True),lambda p:p['scenarios'][0]['probe'].update(count=0)):
   p=copy.deepcopy(self.proof);mutation(p)
   with self.assertRaises(RuntimeError):self.validate(p)
 def test_non_windows_baseline_activity_stale_source_capture_and_false_trust_fail(self):
  for key,value in (('platform','linux'),('baseline',True),('external_requests',['https://example.invalid/']),('source_commit','b'*40),('verified',False)):
   p=copy.deepcopy(self.proof);p[key]=value
   with self.assertRaises(RuntimeError):self.validate(p)
  p=copy.deepcopy(self.proof);p['scenarios'][0]['trusted_clicks']=[1,1,1]
  with self.assertRaises(RuntimeError):self.validate(p)
  (self.root/'r64-crop-icon-native.png').write_bytes(self.png+b'changed')
  with self.assertRaises(RuntimeError):self.validate()
 def test_full_and_early_run_and_staging_cannot_omit_new_groups(self):
  r=Path(__file__).resolve().parents[1]
  early=(r/'ci/public_ci_early.py').read_text();stage=(r/'ci/public_ci_validate_installed.py').read_text();full=(r/'scripts/build_windows.ps1').read_text(encoding='utf-8-sig')
  for name in ('test_crop_icon_r64.py','test_crop_guard_r64.py','test_combined_recovery_r64.py','test_hidden_collection_blocker_r63.py','test_posting_withdraw*_r63.py','test_posting_durable_preflight_r63.py'):
   self.assertIn(name,early);self.assertIn(name,stage);self.assertIn(name,full)
  self.assertIn('desktop/tests/crop-icon-r64.cjs',early);self.assertIn('desktop\\tests\\crop-icon-r64.cjs',full)
  self.assertLess(stage.index('validate_crop(crop_native'),stage.index("print('PUBLIC_INSTALLED_ACCEPTANCE=PASS')"))
if __name__=='__main__':unittest.main()

