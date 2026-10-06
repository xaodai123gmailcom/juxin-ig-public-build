"""Pure receipt rejection tests; the simulated EXE is never executed."""
import copy,hashlib,importlib.util,json,tempfile,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('upgrade_proof',Path(__file__).with_name('r63_upgrade_proof.py'))
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)

class UpgradeStagingContract(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.exe=self.root/'collector_core.exe';self.exe.write_bytes(b'SYNTHETIC NONEXECUTABLE STAGING FIXTURE')
  self.proof=json.loads(Path(__file__).with_name('r63-upgrade-example.json').read_text())
  self.proof['runtime'].update(executable=str(self.exe),executable_sha256=hashlib.sha256(self.exe.read_bytes()).hexdigest(),frozen=True,windows=True,module_file=str(self.root/'bundle/app/nurture_cleanup_upgrade_selftest.py'),bundle_root=str(self.root/'bundle'))
 def validate(self,p):return mod.validate_upgrade(p,self.exe)
 def test_complete_receipt_with_exact_executable_bytes_passes(self):self.validate(self.proof)
 def test_every_case_is_required_with_exact_outcome(self):
  for name in self.proof['cases']:
   p=copy.deepcopy(self.proof);p['cases'].pop(name)
   with self.assertRaises(RuntimeError):self.validate(p)
   p=copy.deepcopy(self.proof);p['cases'][name]['hold_after']=not p['cases'][name]['hold_after']
   with self.assertRaises(RuntimeError):self.validate(p)
 def test_source_cold_start_or_altered_executable_is_rejected(self):
  for key,value in [('frozen',False),('windows',False),('pid',self.proof['seed_pid']),('executable_sha256','0'*64),('module_file',str(self.root/'elsewhere.py'))]:
   p=copy.deepcopy(self.proof);p['runtime'][key]=value
   with self.assertRaises(RuntimeError):self.validate(p)
  for value in (self.proof['seed_completed_perf_ns'],self.proof['seed_completed_perf_ns']-1):
   p=copy.deepcopy(self.proof);p['opened_perf_ns']=value
   with self.assertRaises(RuntimeError):self.validate(p)
 def test_wall_clock_equality_or_backward_jump_is_diagnostic_only(self):
  for value in (self.proof['seed_completed_ns'],self.proof['seed_completed_ns']-1):
   p=copy.deepcopy(self.proof);p['opened_ns']=value;self.validate(p)
 def test_clock_basis_and_positive_integer_counters_are_required(self):
  for key,value in [('chronology_clock','monotonic'),('seed_completed_perf_ns',0),('opened_perf_ns',True),('seed_completed_perf_ns',1.0)]:
   p=copy.deepcopy(self.proof);p[key]=value
   with self.assertRaises(RuntimeError):self.validate(p)
 def test_missing_external_oracle_history_and_admission_are_rejected(self):
  for key in ('normal_startup_and_shutdown','persisted_before_process','owner_isolation'):
   p=copy.deepcopy(self.proof);p[key]=False
   with self.assertRaises(RuntimeError):self.validate(p)
  for key in self.proof['persisted_state']:
   p=copy.deepcopy(self.proof);p['persisted_state'].pop(key)
   with self.assertRaises(RuntimeError):self.validate(p)
  p=copy.deepcopy(self.proof);p['admission']['closed_legacy']['new_task_after']=False
  with self.assertRaises(RuntimeError):self.validate(p)
if __name__=='__main__':unittest.main()

