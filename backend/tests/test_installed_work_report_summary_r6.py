"""Release proof for exact installed summary totals without timing speed gates."""
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('installed_report_core_probe', ROOT / 'scripts/verify_frozen_core_service.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class InstalledWorkReportSummaryTests(unittest.TestCase):
    def setUp(self):
        self.summary = {'platform': 'instagram', 'start': '2026-10-01T00:00:00+00:00',
                        'end': '2026-10-02T00:00:00+00:00',
                        'totals': {'collection': 441552, 'follow': 0, 'split': 0, 'added': 0, 'confirmed_posting': 0}}
        self.full = {**copy.deepcopy(self.summary), 'rows': [{'profile_id': 'fixture', 'collection': 441552}],
                     'unattributed': 441552}
        self.full['totals'].update(check=0, nurture=0, posting=0, greet=0, approved=0)

    def invoke(self, summaries=None, full=None):
        summaries = copy.deepcopy(summaries if summaries is not None else [self.summary, self.summary])
        full = copy.deepcopy(full if full is not None else self.full)
        calls = []

        def request(path, **kwargs):
            self.assertEqual('/api/reports/query', path)
            self.assertEqual('POST', kwargs['method'])
            self.assertEqual('isolated-session', kwargs['session'])
            self.assertEqual('activity', kwargs['payload']['kind'])
            calls.append(kwargs)
            if kwargs['payload'].get('summary_only'):
                return 200, summaries.pop(0)
            self.assertNotIn('summary_only', kwargs['payload'])
            return 200, full

        result = probe.probe_work_report_summary(request, session='isolated-session', result_count=441552)
        self.assertEqual(3, len(calls))
        return result

    def test_exact_summary_legacy_totals_and_first_repeat_latencies(self):
        result = self.invoke()
        for field in ('verified', 'summary_legacy_totals_equal', 'fixture_totals_verified',
                      'repeated_summary_equal', 'summary_only_wire', 'synthetic'):
            self.assertTrue(result[field])
        self.assertEqual(self.summary['totals'], result['totals'])
        self.assertFalse(result['live_accounts_tested'])
        self.assertFalse(result['user_data_touched'])
        self.assertLess(result['summary_bytes'], result['legacy_bytes'])
        for field in ('summary_first_seconds', 'summary_repeat_seconds', 'legacy_full_seconds'):
            self.assertGreaterEqual(result[field], 0)

    def test_ignored_summary_option_and_omitted_metrics_fail_closed(self):
        variants = [self.full, {k: v for k, v in self.summary.items() if k != 'platform'}]
        missing = copy.deepcopy(self.summary)
        del missing['totals']['split']
        variants.append(missing)
        for response in variants:
            with self.subTest(response=response), self.assertRaisesRegex(RuntimeError, 'totals or wire'):
                self.invoke(summaries=[response, self.summary])

    def test_inflated_truncated_or_boolean_counts_fail_closed(self):
        for metric, value in (('collection', 441551), ('collection', 441553),
                              ('split', 1272), ('follow', True), ('added', 1)):
            response = copy.deepcopy(self.summary)
            response['totals'][metric] = value
            with self.subTest(metric=metric, value=value), self.assertRaises(RuntimeError):
                self.invoke(summaries=[self.summary, response])

    def test_full_legacy_disagreement_fails_closed(self):
        full = copy.deepcopy(self.full)
        full['totals']['collection'] += 1
        with self.assertRaisesRegex(RuntimeError, 'legacy path disagrees'):
            self.invoke(full=full)

    def test_legacy_index_upgrade_probe_requires_positive_exact_totals_and_csv_rows(self):
        expected = {'start': '2026-10-03T00:00:00+08:00', 'end': '2026-10-04T00:00:00+08:00',
                    'totals': {'collection': 3, 'follow': 1, 'split': 3, 'added': 7, 'confirmed_posting': 1}}
        def request(path, **kwargs):
            self.assertEqual('/api/reports/query', path)
            value = {'totals': copy.deepcopy(expected['totals'])}
            if not kwargs['payload']['summary_only']:
                value['rows'] = [copy.deepcopy(expected['totals'])]
            return 200, value
        proof = probe.probe_report_index_totals(request, session='fixture', expected=expected)
        self.assertEqual({'summary_first_seconds', 'summary_repeat_seconds', 'legacy_full_seconds'}, set(proof))
        for mutation in ('zeros', 'missing', 'boolean', 'csv'):
            def changed(path, **kwargs):
                status, value = request(path, **kwargs)
                if mutation == 'zeros':
                    value['totals'] = dict.fromkeys(expected['totals'], 0)
                elif mutation == 'missing':
                    value['totals'].pop('split')
                elif mutation == 'boolean':
                    value['totals']['follow'] = True
                elif not kwargs['payload']['summary_only']:
                    value['rows'][0]['split'] = 0
                return status, value
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                probe.probe_report_index_totals(changed, session='fixture', expected=expected)


if __name__ == '__main__':
    unittest.main()
