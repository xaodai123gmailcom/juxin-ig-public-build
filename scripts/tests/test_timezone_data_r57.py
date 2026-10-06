"""Exercise the actual build preflight in independent Python processes."""
from pathlib import Path
import subprocess
import sys
import unittest


PROBE = Path(__file__).resolve().parents[1] / "verify_timezone_data.py"


class TimezoneDataR57Tests(unittest.TestCase):
    def test_package_fallback_verifies_both_dst_transitions(self):
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(PROBE)],
                                encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("TIMEZONE_DATA_CHECK=PASS", result.stdout)
        self.assertIn('"source": "tzdata"', result.stdout)
        self.assertIn('"fall": 25', result.stdout)
        self.assertIn('"spring": 23', result.stdout)

    def test_missing_package_fails_even_if_host_has_zone_data(self):
        # -S prevents site-packages loading; a host IANA database cannot hide
        # the missing declared dependency. No installed package is altered.
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-S", str(PROBE)],
                                encoding="utf-8", capture_output=True, timeout=15)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("TIMEZONE_DATA_CHECK=FAIL", result.stderr)
        self.assertIn("tzdata", result.stderr)
        self.assertIn("backend/requirements.txt", result.stderr)
        self.assertNotIn("TIMEZONE_DATA_CHECK=PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
