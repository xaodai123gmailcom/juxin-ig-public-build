"""Only explicit runtime environment config supplies the optional private key."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app.config import Settings


class PexelsConfigurationTests(unittest.TestCase):
    def test_default_unconfigured_no_bundled_or_shared_key(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ,
                {'IGAC_STARTUP_TOKEN': 'x' * 32, 'IGAC_DATA_DIR': temporary,
                 'HOME': temporary, 'USERPROFILE': temporary, 'LOCALAPPDATA': temporary}, clear=True):
            self.assertIsNone(Settings.from_env().pexels_api_key)

    def test_explicit_process_key_loaded_but_never_in_settings_repr(self):
        key = 'fixture-private-pexels-key-not-real'
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ,
                {'IGAC_STARTUP_TOKEN': 'x' * 32, 'IGAC_DATA_DIR': temporary,
                 'HOME': temporary, 'USERPROFILE': temporary, 'LOCALAPPDATA': temporary,
                 'IGAC_PEXELS_API_KEY': key}, clear=True):
            settings = Settings.from_env()
            self.assertEqual(key, settings.pexels_api_key)
            self.assertNotIn(key, repr(settings))
            self.assertEqual([], list(Path(temporary).iterdir()), 'reading runtime key must not write config files')

    def test_existing_positional_settings_constructor_keeps_its_contract(self):
        settings = Settings('x' * 32, Path('/synthetic/db'), Path('/synthetic'),
                            'http://127.0.0.1:54345', None, 24, '127.0.0.1', 8765)
        self.assertEqual(24, settings.session_hours)
        self.assertEqual(8765, settings.bind_port)
        self.assertIsNone(settings.pexels_api_key)

    def test_empty_runtime_key_is_unconfigured(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ,
                {'IGAC_STARTUP_TOKEN': 'x' * 32, 'IGAC_DATA_DIR': temporary,
                 'HOME': temporary, 'USERPROFILE': temporary, 'LOCALAPPDATA': temporary,
                 'IGAC_PEXELS_API_KEY': ''}, clear=True):
            self.assertIsNone(Settings.from_env().pexels_api_key)


if __name__ == '__main__':
    unittest.main()
