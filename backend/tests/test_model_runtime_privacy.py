from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import download_person_models


class FrozenModelRuntimePrivacyTestCase(unittest.TestCase):
    def test_frozen_windows_runtime_disables_telemetry_before_openvino_import(self) -> None:
        with tempfile.TemporaryDirectory(prefix="igac-telemetry-test-") as temporary:
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "platform", "win32"),
                patch.dict(os.environ, {"LOCALAPPDATA": temporary}, clear=False),
            ):
                download_person_models._disable_frozen_openvino_telemetry()

            consent_file = (
                Path(temporary) / "Intel Corporation" / "openvino_telemetry"
            )
            self.assertEqual(b"0", consent_file.read_bytes())
            self.assertEqual([], list(consent_file.parent.glob("*.tmp")))

    def test_frozen_runtime_fails_closed_without_windows_user_storage(self) -> None:
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "platform", "win32"),
            patch.dict(os.environ, {"LOCALAPPDATA": ""}, clear=False),
            self.assertRaises(SystemExit) as raised,
        ):
            download_person_models._disable_frozen_openvino_telemetry()

        self.assertEqual(1, raised.exception.code)

    def test_source_model_check_does_not_modify_telemetry_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="igac-telemetry-test-") as temporary:
            with (
                patch.object(sys, "frozen", False, create=True),
                patch.object(sys, "platform", "win32"),
                patch.dict(os.environ, {"LOCALAPPDATA": temporary}, clear=False),
            ):
                download_person_models._disable_frozen_openvino_telemetry()

            self.assertFalse((Path(temporary) / "Intel Corporation").exists())


if __name__ == "__main__":
    unittest.main()
