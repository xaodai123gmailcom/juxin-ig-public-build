"""Benchmark preserves every nonposting metric and row after feature removal."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('report_benchmark_r6',ROOT/'scripts/benchmark_work_reports_r6.py')
benchmark=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(benchmark)

class WorkReportBenchmarkParityTests(unittest.TestCase):
    def fixtures(self):
        before={'totals':{'collection':12,'posting':3},'rows':[{'profile_id':'window','window_name':'Window',
            'username':'actor','instagram_user_id':'123','collection':12,'posting':3}]}
        after=copy.deepcopy(before);after['totals'].pop('posting');after['rows'][0].pop('posting')
        return before,after

    def test_nonposting_totals_and_rows_compare_without_retired_fields(self):
        before,after=self.fixtures();benchmark.assert_legacy_report_parity(before,after)
        for part in ('totals','rows'):
            changed=copy.deepcopy(after)
            (changed['totals'] if part=='totals' else changed['rows'][0])['collection']=13
            with self.subTest(part=part),self.assertRaises(AssertionError):benchmark.assert_legacy_report_parity(before,changed)

    def test_retired_receipt_field_must_be_absent_from_totals_and_rows(self):
        before,after=self.fixtures()
        for part in ('totals','rows'):
            changed=copy.deepcopy(after)
            (changed['totals'] if part=='totals' else changed['rows'][0])['confirmed_posting']=1
            with self.subTest(part=part),self.assertRaises(AssertionError):benchmark.assert_legacy_report_parity(before,changed)

    def test_small_reproducible_synthetic_benchmark(self):
        # Optional local preserved baseline supplies an independent pre-fix
        # comparison, while clean CI still runs the exact known fixture oracle.
        legacy=os.environ.get('IGAC_TEST_REPORT_LEGACY_MODULE')
        if legacy:self.assertTrue(Path(legacy).is_file())
        result=benchmark.run(1000,Path(legacy) if legacy else None)
        self.assertTrue(result['posting_metrics_removed'])
        self.assertEqual(100,result['summary_first']['totals']['collection'])
        self.assertEqual(20,result['summary_first']['totals']['split'])
        self.assertEqual('ok',result['integrity'])
        if legacy:self.assertTrue(result['legacy_metric_and_row_parity'])
        print('REPORT_BENCHMARK_PARITY='+json.dumps(result,sort_keys=True))

if __name__=='__main__':unittest.main()
