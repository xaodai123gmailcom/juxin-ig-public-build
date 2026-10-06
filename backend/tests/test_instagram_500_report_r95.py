"""Offline campaign's acceptance oracle must reject incomplete/tampered proof."""
import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path, PureWindowsPath, PurePosixPath
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('ig500_verifier',ROOT/'scripts/verify_instagram_500_report.py')
verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)

class Instagram500ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        files={}
        for p in ['backend/app/a.py','scripts/simulate_instagram_500.py','scripts/ig500_geometry.cjs','scripts/verify_instagram_500_report.py']:
            path=self.root/p;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('fixture code')
            files[p]=hashlib.sha256(path.read_bytes()).hexdigest()
        code={'files':files,'sha256':verifier.digest(files)}
        rows=[]
        for i in range(2):
            expected=verifier.digest(sorted(f'r{i:04d}_member_{n:04d}' for n in range(1200)))
            rows.append({'run_id':f'ig500-{i:04d}','seed':951000+i,'input_count':1200,
                **{k:expected for k in ['input_sha256','actual_sha256','extracted_sha256','restart_actual_sha256']},
                'actual_count':1200,'extracted_count':1200,'status':'passed','task_status':'completed',
                **{k:[] for k in ['missing','extraneous','extracted_missing','extracted_extraneous']},
                **{k:0 for k in ['duplicate_results','duplicate_profile_reads','leases','foreign_key_errors']},
                'review_count':1200,'global_identity_count':1201,'queue':{'total':1200,'recorded':1200,'pending':0,'deduped':0},
                'checkpoint':{'cursor':{'candidate_spool_complete':True,'candidate_spool_natural_end':True}},
                'scenario':['plain','delayed_repaint'][i],'exact_profile_fields_verified':1200,'split_completions':1,'children_created':1,'children_requested':1,'delayed_reads':3,
                'code_sha256':code['sha256'],'elapsed_seconds':1,'geometry_frames':200,'scroll_moves':199,'dom_reads':400})
        self.report={'production_code':code,'code_unchanged_during_campaign':True,'runs_requested':2,'runs_completed':2,'passed':2,'failed':0,'records':rows}
    def verify(self):return verifier.verify_report(self.report,self.root,required_runs=2)
    def test_complete_independent_oracle_is_accepted(self):self.assertEqual(2400,self.verify()['verified_ground_truth_identities'])
    def test_missing_extra_duplicate_and_incomplete_cases_are_rejected(self):
        original=copy.deepcopy(self.report)
        for key,value in [('actual_count',1199),('missing',['lost']),('extraneous',['recommendation']),('duplicate_results',1),('actual_sha256','bad'),('restart_actual_sha256','bad'),('status','error'),('input_count',999)]:
            with self.subTest(key=key):
                self.report=copy.deepcopy(original);self.report['records'][0][key]=value
                with self.assertRaises(ValueError):self.verify()
    def test_native_end_pending_and_duplicate_run_are_rejected(self):
        self.report['records'][0]['checkpoint']['cursor']['candidate_spool_natural_end']=False
        with self.assertRaises(ValueError):self.verify()
        self.setUp();self.report['records'][0]['queue']['pending']=1
        with self.assertRaises(ValueError):self.verify()
        self.setUp();self.report['records'][1]=copy.deepcopy(self.report['records'][0])
        with self.assertRaises(ValueError):self.verify()
    def test_changed_or_unlisted_production_code_is_rejected(self):
        (self.root/'backend/app/a.py').write_text('changed')
        with self.assertRaises(ValueError):self.verify()
        self.setUp();(self.root/'backend/app/new_helper.py').write_text('new')
        with self.assertRaises(ValueError):self.verify()
    def test_missing_harness_and_source_changed_flag_are_rejected(self):
        del self.report['production_code']['files']['scripts/ig500_geometry.cjs']
        with self.assertRaises(ValueError):self.verify()
        self.setUp();del self.report['production_code']['files']['scripts/verify_instagram_500_report.py']
        self.report['production_code']['sha256']=verifier.digest(self.report['production_code']['files'])
        with self.assertRaisesRegex(ValueError,'harness/verifier'):self.verify()
        self.setUp();self.report['code_unchanged_during_campaign']=False
        with self.assertRaises(ValueError):self.verify()

    def test_actual_producer_normalizes_windows_and_posix_manifest_keys(self):
        producer_spec=importlib.util.spec_from_file_location('ig500_producer',ROOT/'scripts/simulate_instagram_500.py')
        producer=importlib.util.module_from_spec(producer_spec);producer_spec.loader.exec_module(producer)
        for root,path in [(PureWindowsPath('C:/build/source'),PureWindowsPath('C:/build/source/backend/app/example.py')),
                          (PurePosixPath('/build/source'),PurePosixPath('/build/source/backend/app/example.py'))]:
            self.assertEqual('backend/app/example.py',producer.source_relative_key(path,root))
        manifest=producer.code_digest()
        self.assertTrue(all('\\' not in key for key in manifest['files']))
        self.assertIn('backend/app/playwright_worker.py',manifest['files'])
        self.assertIn('scripts/verify_instagram_500_report.py',manifest['files'])
