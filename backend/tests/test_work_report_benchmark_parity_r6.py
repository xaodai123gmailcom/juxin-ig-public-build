"""Benchmark preserves every historical metric and row beside new receipt count."""
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
        after=copy.deepcopy(before);after['totals']['confirmed_posting']=0;after['rows'][0]['confirmed_posting']=0
        return before,after

    def test_historical_totals_and_rows_compare_without_new_zero_receipt_field(self):
        before,after=self.fixtures();benchmark.assert_legacy_report_parity(before,after)
        for part in ('totals','rows'):
            changed=copy.deepcopy(after)
            (changed['totals'] if part=='totals' else changed['rows'][0])['posting']=4
            with self.subTest(part=part),self.assertRaises(AssertionError):benchmark.assert_legacy_report_parity(before,changed)

    def test_new_receipt_count_must_be_zero_in_totals_and_each_fixture_row(self):
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
        self.assertTrue(result['confirmed_posting_zero'])
        self.assertEqual(100,result['summary_first']['totals']['collection'])
        self.assertEqual(20,result['summary_first']['totals']['split'])
        self.assertEqual('ok',result['integrity'])
        if legacy:self.assertTrue(result['legacy_metric_and_row_parity'])
        print('REPORT_BENCHMARK_PARITY='+json.dumps(result,sort_keys=True))

if __name__=='__main__':unittest.main()
