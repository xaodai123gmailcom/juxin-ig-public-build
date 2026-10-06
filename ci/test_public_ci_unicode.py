"""Mocked Unicode CI contracts: no native runtime, product, network, or subprocess."""
import ast
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_ci_unicode as gate


class UnicodeContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root, self.state_root = self.base / 'source', self.base / 'state'
        self.root.mkdir(); self.state_root.mkdir()
        self.state = {'nonce': 'a' * 32, 'source_provenance': {'source_manifest_sha256': 'b' * 64}}
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        for name, value in (('ROOT', self.root), ('state_root', lambda: self.state_root),
                            ('verify_run_state', lambda: self.state)):
            self.stack.enter_context(patch.object(gate, name, value))
        self.stack.enter_context(patch.dict(os.environ, {'SystemRoot': str(self.base / 'Windows')}))
        self.data = {name: ('mock model ' + name).encode() for name in gate.MODEL_FILES}
        model_manifest = {'schema_version': 1, 'files': [
            {'filename': name, 'size': len(data), 'sha384': hashlib.sha384(data).hexdigest()}
            for name, data in sorted(self.data.items())]}
        sources = {'backend/requirements.txt': b'openvino==2025.4.1\n',
            'scripts/verify_openvino_windows.py': b'# mock sealed verifier\n',
            'scripts/install_python_dependencies.py': b'# mock sealed installer\n',
            'scripts/download_person_models.py': b'# mock sealed downloader\n',
            (gate.ASSETS / 'MODEL_SOURCES.json').as_posix(): json.dumps(model_manifest).encode()}
        manifest = {}
        for name, data in sources.items():
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            manifest[name] = hashlib.sha256(data).hexdigest()
        (self.root / 'SOURCE_SHA256.json').write_text(json.dumps(manifest))

    def prepare_source(self):
        identifier, work = gate.create_work('source')
        source = work / gate.UNICODE_SOURCE
        for name, checksum in gate.source_entries().items():
            gate.copy_file(self.root / name, source / name, checksum)
        for name, data in self.data.items():
            (source / gate.ASSETS / name).write_bytes(data)
        (work / 'source-cache').mkdir()
        (source / '.venv').mkdir()
        return identifier, work, source

    def test_sealed_copy_rejects_missing_extra_changed_and_model_bytes(self):
        _, _, source = self.prepare_source()
        gate.verify_source_copy(source, models=True)
        requirements = source / 'backend/requirements.txt'
        original = requirements.read_bytes(); requirements.write_bytes(b'tampered')
        with self.assertRaisesRegex(RuntimeError, 'sealed checkout'):
            gate.verify_source_copy(source, models=True)
        requirements.write_bytes(original)
        extra = source / 'scripts/extra.py'; extra.write_bytes(b'not sealed')
        with self.assertRaisesRegex(RuntimeError, 'unsealed'):
            gate.verify_source_copy(source, models=True)
        extra.unlink()
        model = source / gate.ASSETS / sorted(gate.MODEL_FILES)[0]
        original = model.read_bytes(); model.write_bytes(b'x' * len(original))
        with self.assertRaisesRegex(RuntimeError, 'sealed pin'):
            gate.verify_source_copy(source, models=True)
        model.unlink()
        with self.assertRaisesRegex(RuntimeError, 'unsealed'):
            gate.verify_source_copy(source, models=True)

    def test_copy_never_overwrites_and_binds_both_sides(self):
        source = self.root / 'backend/requirements.txt'
        destination = self.base / 'copy'
        with self.assertRaisesRegex(RuntimeError, 'source bytes'):
            gate.copy_file(source, destination, '0' * 64)
        self.assertFalse(destination.exists())
        gate.copy_file(source, destination, gate.digest(source))
        with self.assertRaises(FileExistsError):
            gate.copy_file(source, destination, gate.digest(source))

    def test_tree_rejects_links_entry_depth_and_byte_overflow(self):
        directory = self.base / 'tree'; directory.mkdir()
        (directory / 'file').write_bytes(b'12')
        for setting, maximum in (('MAX_ENTRIES', 0), ('MAX_FILE_BYTES', 1), ('MAX_TREE_BYTES', 1)):
            with self.subTest(setting=setting), patch.object(gate, setting, maximum), self.assertRaises(RuntimeError):
                gate.files(directory)
        (directory / 'child').mkdir()
        with patch.object(gate, 'MAX_DEPTH', 0), self.assertRaises(RuntimeError):
            gate.files(directory)
        # Windows ordinary CI may not grant symlink creation. Inject the same
        # lstat reparse attribute instead so the rejection always runs.
        real_lstat = Path.lstat
        def linked(path):
            info = real_lstat(path)
            if path == directory / 'file':
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, 'lstat', linked), self.assertRaisesRegex(RuntimeError, 'Linked'):
            gate.files(directory)

    def test_owner_nonce_kind_and_traversal_rejected_before_cleanup(self):
        identifier, work = gate.create_work('source')
        self.assertEqual(gate.owned_work(identifier, 'source'), work)
        for value in ('../escape', '', 'x' * 32):
            with self.assertRaises(RuntimeError):
                gate.owned_work(value, 'source')
        self.state['nonce'] = 'c' * 32
        with patch.object(gate.shutil, 'rmtree') as remove, self.assertRaises(RuntimeError):
            gate.cleanup(identifier, 'source')
        remove.assert_not_called()

    def test_source_chain_uses_new_unicode_venv_strict_caches_and_cleanup(self):
        calls, caches = [], []
        def run(label, command, timeout, environment=None):
            calls.append((label, command, timeout, environment))
            if label == 'unicode-source-create-venv':
                (Path(command[-1]) / 'Scripts').mkdir(parents=True)
            if label == 'unicode-source-pinned-models':
                source = Path(command[5]).parents[1]
                for name, data in self.data.items():
                    (source / gate.ASSETS / name).write_bytes(data)
            if environment:
                cache = Path(environment['IGAC_OPENVINO_CACHE_ROOT'])
                self.assertTrue(str(cache).isascii()); self.assertTrue(cache.is_dir())
                self.assertEqual(environment['IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE'], '1')
                self.assertFalse(str(command[0]).isascii())
                caches.append(str(cache))
        with patch.object(gate, 'run_owned', side_effect=run):
            gate.source_gates(); gate.source_gates()
        self.assertEqual(len(calls), 12)
        self.assertEqual(caches[0], caches[1]); self.assertNotEqual(caches[0], caches[2])
        self.assertEqual([call[0] for call in calls[:6]], ['unicode-source-create-venv',
            'unicode-source-pinned-dependencies', 'unicode-source-pinned-models',
            'unicode-source-model-integrity', 'unicode-source-probe', 'unicode-classifier-probe'])
        self.assertEqual(list(self.state_root.iterdir()), [])
        self.assertTrue(all(call[2] <= 1800 for call in calls))

    def test_source_gate_failure_propagates_and_cleans_only_owner(self):
        neighbor = self.state_root / 'unrelated'; neighbor.mkdir()
        with patch.object(gate, 'run_owned', side_effect=RuntimeError('mock child failed')):
            with self.assertRaisesRegex(RuntimeError, 'mock child failed'):
                gate.source_gates()
        self.assertEqual(list(self.state_root.iterdir()), [neighbor])

    def test_original_dll_venv_origin_cache_and_consent_before_chdir(self):
        identifier, work, source = self.prepare_source()
        dll = source / '.venv/Lib/site-packages/openvino/libs/openvino.dll'
        dll.parent.mkdir(parents=True); dll.write_bytes(b'mocked native file never loaded')
        distribution = SimpleNamespace(version='2025.4.1', locate_file=lambda name: dll)
        environment = {'IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE': '1',
                       'IGAC_OPENVINO_CACHE_ROOT': str(work / 'source-cache')}
        with patch.object(sys, 'prefix', str(source / '.venv')), patch.dict(os.environ, environment), \
                patch.object(gate.importlib.metadata, 'distribution', return_value=distribution), \
                patch.object(gate, 'check_telemetry_consent') as consent, patch.object(gate.os, 'chdir') as chdir, \
                patch.object(sys, 'path', list(sys.path)):
            gate.source_context(identifier)
            consent.assert_called_once(); chdir.assert_called_once_with(source)
            outside = self.base / 'outside.dll'; outside.write_bytes(b'outside')
            distribution.locate_file = lambda name: outside
            with self.assertRaisesRegex(RuntimeError, 'copied Unicode venv'):
                gate.source_context(identifier)
            distribution.locate_file = lambda name: dll
            with patch.dict(os.environ, {'IGAC_OPENVINO_CACHE_ROOT': str(self.base)}), self.assertRaises(RuntimeError):
                gate.source_context(identifier)
            with patch.object(sys, 'prefix', str(self.base)), self.assertRaisesRegex(RuntimeError, 'own Unicode venv'):
                gate.source_context(identifier)
            with patch.dict(sys.modules, {'openvino': SimpleNamespace()}), self.assertRaisesRegex(RuntimeError, 'before any OpenVINO import'):
                gate.source_context(identifier)
            with patch.object(gate, 'check_telemetry_consent', side_effect=RuntimeError('raw0 changed')), \
                    self.assertRaisesRegex(RuntimeError, 'raw0 changed'):
                gate.source_context(identifier)
            self.assertEqual(chdir.call_count, 1)

    def test_source_probe_calls_original_verifier_and_checks_asset_origin_first(self):
        _, work, source = self.prepare_source()
        order = []
        verifier = {'_model_directory': lambda: source / gate.ASSETS,
                    'verify_source': lambda manifest: order.append(('inference', manifest))}
        with patch.object(gate, 'source_context', return_value=(work, source, work / 'source-cache')), \
                patch.object(gate, 'check_telemetry_consent', side_effect=lambda: order.append('consent')), \
                patch.object(gate.runpy, 'run_path', return_value=verifier) as loader, \
                patch.object(gate, 'assert_cache', side_effect=lambda cache: order.append(('cache', cache))):
            gate.source_probe('mock')
            loader.assert_called_once_with(str(source / 'scripts/verify_openvino_windows.py'))
            self.assertEqual(order, ['consent', ('inference', work / 'unicode-native-manifest.json'),
                ('cache', work / 'source-cache'), 'consent'])
            verifier['_model_directory'] = lambda: self.root / gate.ASSETS
            order.clear()
            with self.assertRaisesRegex(RuntimeError, 'copied Unicode model assets'):
                gate.source_probe('mock')
            self.assertEqual(order, ['consent'])

    def test_loaded_native_dll_must_stay_inside_expected_ascii_cache(self):
        cache = self.base / 'cache'; cache.mkdir()
        loaded = cache / 'openvino.dll'; loaded.write_bytes(b'never loaded')
        bootstrap = SimpleNamespace(prepare_openvino_native_runtime=lambda: cache,
            assert_loaded_openvino_from_cache=lambda path: loaded)
        with patch.dict(sys.modules, {'app.openvino_native_bootstrap': bootstrap}):
            gate.assert_cache(cache)
            outside = self.base / 'outside.dll'; outside.write_bytes(b'never loaded')
            bootstrap.assert_loaded_openvino_from_cache = lambda path: outside
            with self.assertRaisesRegex(RuntimeError, 'outside its fresh owned root'):
                gate.assert_cache(cache)

    def test_classifier_failures_and_four_model_gender_branch_are_real_calls(self):
        _, work, source = self.prepare_source()
        expected = (source / gate.ASSETS).resolve()
        result = SimpleNamespace(checked=True, reason='checked')
        models = [object() for _ in range(4)]
        classifier = SimpleNamespace(ASSET_DIRECTORY=expected,
            _model_paths=lambda: tuple(expected / name for name in gate.MODEL_FILES),
            classify=lambda data: result, _get_compiled_models=lambda: tuple(models))
        image = SimpleNamespace(save=lambda target, format: target.write(b'synthetic mock image'))
        gender_calls = []
        modules = {'PIL': SimpleNamespace(Image=SimpleNamespace(new=lambda *a: image)),
            'app.person_recognition': SimpleNamespace(LocalOpenVinoPersonClassifier=lambda: classifier,
                exercise_local_openvino_gender_branch=lambda model: (gender_calls.append(model) or ('female', .5)))}
        with patch.object(gate, 'source_context', return_value=(work, source, work / 'source-cache')), \
                patch.object(gate, 'check_telemetry_consent'), patch.object(gate, 'assert_cache'), patch.dict(sys.modules, modules):
            gate.classifier_probe('mock')
            self.assertEqual(gender_calls, [models[1]])
            for reason in ('model_unavailable', 'model_integrity_failed', 'local_inference_failed'):
                result.reason = reason
                with self.assertRaisesRegex(RuntimeError, 'classifier inference'):
                    gate.classifier_probe('mock')
            result.reason = 'checked'; result.checked = False
            with self.assertRaises(RuntimeError): gate.classifier_probe('mock')
            result.checked = True; models[3] = None
            with self.assertRaisesRegex(RuntimeError, 'Four classifier models'): gate.classifier_probe('mock')
            models[3] = object()
            paths = tuple(expected / name for name in gate.MODEL_FILES)
            for wrong in (paths[:-1], paths + (expected / 'extra.xml',), paths[:-1] + (self.base / 'outside.xml',)):
                classifier._model_paths = lambda: wrong
                with self.assertRaisesRegex(RuntimeError, 'copied Unicode source'): gate.classifier_probe('mock')

    def test_copied_frozen_chain_uses_original_manifest_and_strict_original_probes(self):
        original = self.root / 'dist/collector_core'; (original / '_internal').mkdir(parents=True)
        (original / 'collector_core.exe').write_bytes(b'mocked frozen bytes never executed')
        (original / '_internal/openvino-native-manifest.json').write_bytes(b'{"actual":"manifest"}')
        (self.root / 'build').mkdir()
        (self.root / 'build/openvino-native-manifest.json').write_bytes(b'{"actual":"manifest"}')
        calls = []
        def run(label, command, timeout, environment=None):
            calls.append((label, command, timeout))
            if label == 'unicode-frozen-real-inference':
                executable = Path(command[command.index('-Executable') + 1])
                cache = Path(command[command.index('-ExpectedCacheRoot') + 1])
                self.assertFalse(str(executable).isascii()); self.assertTrue(str(cache).isascii())
                self.assertEqual(list(cache.iterdir()), [])
                self.assertEqual(executable.read_bytes(), (original / 'collector_core.exe').read_bytes())
                self.assertIn('-RequireNonAsciiSource', command)
                self.assertEqual(command[command.index('-TimeoutSeconds') + 1], '180')
        with patch.object(gate, 'validate_source_build') as validate, patch.object(gate, 'run_owned', side_effect=run):
            gate.frozen_gates()
        self.assertEqual(validate.call_count, 2)
        self.assertEqual([call[0] for call in calls], ['unicode-frozen-original-layout',
            'unicode-frozen-copied-layout', 'unicode-frozen-real-inference', 'unicode-frozen-real-service'])
        self.assertEqual(list(self.state_root.iterdir()), [])
        self.assertFalse(any('pyinstaller' in ' '.join(command).lower() or 'build_windows.ps1' in ' '.join(command)
                             for _, command, _ in calls))
        self.assertEqual([call[2] for call in calls], [180, 180, 240, 180])
        neighbor = self.state_root / 'unrelated'; neighbor.mkdir()
        def corrupt_copy(label, command, timeout):
            if label == 'unicode-frozen-real-service':
                Path(command[command.index('--executable') + 1]).write_bytes(b'changed')
        with patch.object(gate, 'validate_source_build'), patch.object(gate, 'run_owned', side_effect=corrupt_copy):
            with self.assertRaisesRegex(RuntimeError, 'changed during Unicode checks'):
                gate.frozen_gates()
        self.assertEqual(list(self.state_root.iterdir()), [neighbor])
        self.assertEqual((original / 'collector_core.exe').read_bytes(), b'mocked frozen bytes never executed')

    def test_main_blocks_before_any_gate_off_authorized_windows(self):
        with patch.object(sys, 'argv', ['helper', 'source']), \
                patch.object(gate, 'verify_run_state', side_effect=RuntimeError('not authorized Windows')), \
                patch.object(gate, 'source_gates') as source, self.assertRaises(RuntimeError):
            gate.main()
        source.assert_not_called()

    def test_original_verifier_contains_four_model_inference_and_gender_parser(self):
        original = Path(gate.__file__).resolve().parents[1]
        tree = ast.parse((original / 'scripts/verify_openvino_windows.py').read_text())
        inference = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_run_real_inference')
        loop = next(node for node in inference.body if isinstance(node, ast.For))
        self.assertEqual(set(ast.literal_eval(loop.iter)), {name + '.xml' for name in gate.MODELS})
        self.assertIn('core.compile_model', ast.unparse(loop)); self.assertIn('compiled([', ast.unparse(loop))
        self.assertIn('exercise_local_openvino_gender_branch', ast.unparse(inference))


if __name__ == '__main__':
    unittest.main()
