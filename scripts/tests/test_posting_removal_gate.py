"""Removed-feature gates reject authentication failures and surviving aliases."""
import importlib.util
from pathlib import Path
import unittest
from urllib.error import HTTPError
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('removal_core_probe', ROOT / 'scripts/verify_frozen_core_service.py')
probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)

class RemovedFeatureGateTests(unittest.TestCase):
    def test_exact_http_endpoint_set_and_session(self):
        seen = []
        def request(path, **options):
            seen.append((path, options))
            raise HTTPError('http://127.0.0.1' + path, 404, 'Not Found', {}, None)
        result = probe.probe_posting_removed(request, session='synthetic')
        self.assertEqual(list(probe.REMOVED_POSTING_ENDPOINTS), result['endpoints'])
        self.assertEqual(404, result['http_status'])
        self.assertEqual(3, len(seen))
        self.assertTrue(all(options['session'] == 'synthetic' for _, options in seen))
    def test_surviving_or_only_unauthorized_route_fails(self):
        for status in (200, 401, 403, 405, 410, 500):
            def request(path, **options):
                raise HTTPError('http://127.0.0.1' + path, status, 'Synthetic', {}, None)
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                probe.probe_posting_removed(request)
            with self.subTest(returned_status=status), self.assertRaises(RuntimeError):
                probe.probe_posting_removed(lambda *args, **kwargs: (status, {}))

if __name__ == '__main__': unittest.main()
