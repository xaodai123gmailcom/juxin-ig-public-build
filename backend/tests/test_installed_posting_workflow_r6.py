"""Strict installed posting proof gates; no live account or provider access."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('installed_posting_core_probe',ROOT/'scripts/verify_frozen_core_service.py')
probe=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(probe)

class InstalledPostingWorkflowTests(unittest.TestCase):
    def command(self,body):
        if os.environ.get('IGAC_LOCAL_TEST_ISOLATION')=='1':
            guard=ROOT.parent/'local-test-isolation'
            self.assertTrue((guard/'guard.py').is_file())
            body=f'import os,sys; os.environ["IGAC_LOCAL_TEST_ISOLATION"]="1"; sys.path.insert(0,{str(guard)!r}); import guard; guard.install(); '+body
        return [sys.executable,'-c',body]

    def test_source_cli_real_manager_service_database_and_proof_mutation_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);sentinel=directory/'user.sqlite3'
            sentinel.write_bytes(b'User database must remain untouched')
            with patch.dict(os.environ,{'IGAC_DB_PATH':str(sentinel),'IGAC_DATA_DIR':str(directory),
                    'IGAC_PEXELS_API_KEY':'poison-not-a-real-key','IGAC_PARENT_PID':'poison','IGAC_PORT':'poison'}):
                try:
                    proof=probe.probe_posting_workflow(self.command(f'import sys; sys.path.insert(0,{str(ROOT/"backend")!r}); from app.__main__ import main; main()'),directory/'proof.log')
                except Exception:
                    print((directory/'proof.log').read_text(encoding='utf-8'));raise
            self.assertEqual(b'User database must remain untouched',sentinel.read_bytes())
        print('SOURCE_POSTING_WORKFLOW_PROOF='+json.dumps(proof,sort_keys=True))
        self.assertEqual(5,len(proof['cases']))
        for field in proof:
            missing=copy.deepcopy(proof);del missing[field]
            with self.subTest(root=field),self.assertRaises(RuntimeError):probe.validate_posting_workflow_proof(missing)
        for name in proof['cases']:
            missing=copy.deepcopy(proof);del missing['cases'][name]
            with self.subTest(case=name),self.assertRaises(RuntimeError):probe.validate_posting_workflow_proof(missing)
            for field in proof['cases'][name]:
                missing=copy.deepcopy(proof);del missing['cases'][name][field]
                with self.subTest(case=name,field=field),self.assertRaises(RuntimeError):probe.validate_posting_workflow_proof(missing)
        for name,field,value in [('confirmed_success','receipts',2),('confirmed_success','synthetic_publish_calls',True),
                ('unknown_restart','receipts',1),('negative_close','negative_close_retains_lease_card_and_executor',False),
                ('global_material_dedup','global_normalized_sha256_fence',False)]:
            forged=copy.deepcopy(proof);forged['cases'][name][field]=value
            with self.subTest(forged=(name,field,value)),self.assertRaises(RuntimeError):probe.validate_posting_workflow_proof(forged)

    def test_report_uses_exact_executable_and_separate_posting_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);report=directory/'installed.json';evidence={'verified':True,'synthetic':True}
            with patch.object(probe,'probe_core',return_value={'verified':True}),patch.object(probe,'probe_posting_workflow',return_value=evidence) as posting,patch.object(sys,'argv',['verify','--executable',sys.executable,'--log',str(directory/'core.log'),'--posting-workflow','--report',str(report)]):
                self.assertEqual(0,probe.main())
            self.assertEqual(evidence,json.loads(report.read_text())['posting_workflow'])
            self.assertEqual([str(Path(sys.executable).resolve())],posting.call_args.args[0])

    def test_missing_forged_duplicate_or_nonzero_proof_rejected(self):
        for body in ('print("no proof")','print("POSTING_WORKFLOW_SELFTEST=PASS {}")','raise SystemExit(5)',
                'print("POSTING_WORKFLOW_SELFTEST=PASS {}\\nPOSTING_WORKFLOW_SELFTEST=PASS {}")'):
            with self.subTest(body=body),tempfile.TemporaryDirectory() as temporary,self.assertRaises(RuntimeError):
                probe.probe_posting_workflow(self.command(body),Path(temporary)/'proof.log')

    def test_timeout_fails_release(self):
        with tempfile.TemporaryDirectory() as temporary,self.assertRaisesRegex(RuntimeError,'timed out'):
            probe.probe_posting_workflow(self.command('import time;time.sleep(30)'),Path(temporary)/'proof.log',timeout=.05)

    def test_invalid_deadline_rejected(self):
        for timeout in (0,-1,float('nan'),float('inf')):
            with self.subTest(timeout=timeout),self.assertRaises(ValueError):
                probe.probe_posting_workflow([],Path('unused.log'),timeout=timeout)

if __name__=='__main__':unittest.main()
