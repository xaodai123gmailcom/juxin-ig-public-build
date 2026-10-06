"""Mocked contracts and isolated stdlib import probes; no native runtime/product/network."""
import ast
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
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


class IsolatedLayoutEntry(unittest.TestCase):
    """Exercise the real CI loader with only the original safe import/parser AST.

    The child also forbids all app/OpenVINO/telemetry imports. Backend module
    identities are explicit inert fixtures, never executed application code.
    """
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='igac-isolated-layout-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'sealed Unicode 聚鑫 (4)'
        self.root.mkdir()
        self.original = Path(gate.__file__).resolve().parents[1]
        original = ast.parse((self.original / 'scripts/verify_openvino_windows.py').read_text())
        preamble = next(index for index, node in enumerate(original.body) if isinstance(node, ast.Try))
        prefix = ast.unparse(ast.Module(body=original.body[:preamble + 1], type_ignores=[]))
        parser = ast.unparse(next(node for node in original.body if isinstance(node, ast.FunctionDef) and node.name == 'parse_args'))
        main = ast.unparse(next(node for node in original.body if isinstance(node, ast.FunctionDef) and node.name == 'main'))
        # Match the verifier's own temporary backend insertion. The loader
        # executes only inert, sealed app support fixtures in these subprocesses.
        inert_support = '''
backend_directory = str(Path(__file__).resolve().parents[1] / 'backend')
if backend_directory not in sys.path: sys.path.insert(0, backend_directory)
assert sys.modules['app'].__path__ == []
from app.openvino_native_bootstrap import LAYOUT_IMPORT_SENTINEL
assert LAYOUT_IMPORT_SENTINEL == 'verified inert support'
class NativeRuntimeError(RuntimeError): pass
def verify_source(path): raise AssertionError('Source inference must never be called')
def verify_frozen(manifest, dist):
    print('LAYOUT_ARGS=' + json.dumps({'manifest': str(manifest), 'dist': str(dist),
        'sibling': sys.modules[load_manifest.__module__].__file__,
        'isolated': sys.flags.isolated, 'ignore_environment': sys.flags.ignore_environment,
        'no_user_site': sys.flags.no_user_site,
        'package_path': sys.modules[__package__].__path__}))
'''
        self.verifier = prefix + '\n' + inert_support + '\n' + parser + '\n' + main + '\n'
        self.sources = {'backend/requirements.txt': b'# inert fixture\n',
            'backend/app/__init__.py': b'# inert identity only\n',
            'backend/app/openvino_native_bootstrap.py': b'from .openvino_import_privacy import LAYOUT_IMPORT_SENTINEL\n',
            'backend/app/openvino_import_privacy.py': b"LAYOUT_IMPORT_SENTINEL = 'verified inert support'\n",
            'scripts/download_person_models.py': (self.original / 'scripts/download_person_models.py').read_bytes(),
            'scripts/verify_openvino_windows.py': self.verifier.encode()}
        self.write_sources()
        self.shadow = self.base / 'shadow cwd'; self.shadow.mkdir()
        for name in ('download_person_models.py', 'verify_openvino_windows.py', 'argparse.py', 'json.py'):
            (self.shadow / name).write_text("raise AssertionError('CWD_OR_PYTHONPATH_SHADOW_EXECUTED')\n")
        self.driver = self.base / 'isolated driver.py'
        self.driver.write_text('''import importlib.util, pathlib, sys
from types import ModuleType
class Guard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'app', 'openvino', 'openvino_telemetry'}:
            raise RuntimeError('TEST_FORBIDS_APP_NATIVE_TELEMETRY_IMPORT')
sys.meta_path.insert(0, Guard())
original, root, action, *arguments = sys.argv[1:]
common_spec = importlib.util.spec_from_file_location('public_ci_common', pathlib.Path(original) / 'ci/public_ci_common.py')
common = importlib.util.module_from_spec(common_spec)
sys.modules['public_ci_common'] = common
common_spec.loader.exec_module(common)
common.ROOT = pathlib.Path(root)
common.verify_run_state = lambda: {'nonce': 'a' * 32, 'source_provenance': {'source_manifest_sha256': 'b' * 64}}
common.check_telemetry_consent = lambda: None
def refuse(*args, **kwargs): raise AssertionError('TEST_FORBIDS_PROCESS_OR_PRODUCT_EXECUTION')
common.run_owned = refuse
spec = importlib.util.spec_from_file_location('isolated_gate_fixture', pathlib.Path(original) / 'ci/public_ci_unicode.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
if action == 'preloaded-private': sys.modules['_igac_public_ci_layout'] = ModuleType('untrusted')
if action == 'preloaded-app': sys.modules['app'] = ModuleType('untrusted')
try:
    if action == 'preflight': entry.layout_import_preflight()
    elif action == 'main':
        sys.argv = [str(pathlib.Path(original) / 'ci/public_ci_unicode.py'), *arguments]
        entry.main()
    else: entry.execute_layout_verifier(arguments)
finally:
    if action != 'preloaded-private':
        assert not any(name.startswith('_igac_public_ci_layout') for name in sys.modules), 'Private module cleanup failed'
    if action != 'preloaded-app':
        assert not any(name == 'app' or name.startswith('app.') for name in sys.modules), 'App module cleanup failed'
    assert str(pathlib.Path(root) / 'scripts') not in sys.path, 'Scripts directory exposed'
    assert str(pathlib.Path(root) / 'backend') not in sys.path, 'Verifier backend insertion survived'
    assert str(pathlib.Path.cwd()) not in sys.path and '' not in sys.path, 'CWD exposed'
''', encoding='utf-8')

    def write_sources(self):
        manifest = {}
        for name, data in self.sources.items():
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            manifest[name] = hashlib.sha256(data).hexdigest()
        (self.root / 'SOURCE_SHA256.json').write_text(json.dumps(manifest))

    def command(self, action='execute', arguments=None):
        if arguments is None:
            arguments = ['frozen', '--manifest', str(self.root / 'actual manifest.json'),
                         '--dist', str(self.root / 'copied 冻结 Core (4)')]
        return subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', str(self.driver),
            str(self.original), str(self.root), action, *arguments], cwd=self.shadow,
            env=dict(os.environ, PYTHONPATH=str(self.shadow)), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', timeout=20)

    def test_old_isolated_preamble_fails_new_exact_loader_passes(self):
        # Direct-script form is the real failure topology, stopping at imports.
        old = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8',
            str(self.root / 'scripts/verify_openvino_windows.py')], cwd=self.shadow,
            env=dict(os.environ, PYTHONPATH=str(self.shadow)), capture_output=True,
            text=True, encoding='utf-8', timeout=20)
        self.assertNotEqual(old.returncode, 0)
        self.assertIn("No module named 'download_person_models'", old.stderr)
        new = self.command()
        self.assertEqual(new.returncode, 0, new.stderr)
        observed = json.loads(new.stdout.split('LAYOUT_ARGS=', 1)[1])
        self.assertEqual(observed, {'manifest': str(self.root / 'actual manifest.json'),
            'dist': str(self.root / 'copied 冻结 Core (4)'),
            'sibling': str(self.root / 'scripts/download_person_models.py'),
            'isolated': 1, 'ignore_environment': 1, 'no_user_site': 1, 'package_path': []})

    def test_same_loader_help_exits_before_layout_and_blocks_sdk_or_native_audit(self):
        result = self.command('preflight', [])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--manifest', result.stdout); self.assertNotIn('LAYOUT_ARGS=', result.stdout)
        for payload in ('import openvino', 'import openvino_telemetry', "sys.audit('ctypes.dlopen', 'openvino.dll')"):
            with self.subTest(payload=payload):
                self.sources['scripts/verify_openvino_windows.py'] = (self.verifier + '\n' + payload).encode()
                self.write_sources()
                result = self.command('preflight', [])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Layout import preflight forbids native/telemetry', result.stderr)
                self.assertNotIn('TEST_FORBIDS_APP_NATIVE_TELEMETRY_IMPORT', result.stderr)

    def test_bytecode_poison_cannot_override_verified_source_snapshot(self):
        import importlib._bootstrap_external
        import importlib.util
        for relative in ('scripts/download_person_models.py', 'scripts/verify_openvino_windows.py',
                'backend/app/__init__.py', 'backend/app/openvino_import_privacy.py',
                'backend/app/openvino_native_bootstrap.py'):
            path = self.root / relative
            cached = Path(importlib.util.cache_from_source(str(path)))
            cached.parent.mkdir(exist_ok=True)
            poison = compile("raise AssertionError('POISONED_BYTECODE_EXECUTED')", str(path), 'exec')
            cached.write_bytes(importlib._bootstrap_external._code_to_timestamp_pyc(
                poison, int(path.stat().st_mtime), path.stat().st_size))
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('LAYOUT_ARGS=', result.stdout)

    def test_unsealed_malformed_or_preloaded_modules_fail_before_execution(self):
        for action, expected in (('preloaded-private', 'Private layout package must be fresh'),
                                 ('preloaded-app', 'fresh application module identities')):
            result = self.command(action)
            self.assertNotEqual(result.returncode, 0); self.assertIn(expected, result.stderr)
        target = self.root / 'scripts/verify_openvino_windows.py'
        target.write_bytes(target.read_bytes() + b'\n# unsealed edit\n')
        result = self.command(); self.assertNotEqual(result.returncode, 0)
        self.assertIn('script bytes are unsealed', result.stderr)
        self.sources['scripts/verify_openvino_windows.py'] = b'def malformed(:\n'
        self.sources['scripts/download_person_models.py'] = b"raise AssertionError('SIBLING_EXECUTED_BEFORE_COMPILE')\n"
        self.write_sources()
        result = self.command(); self.assertNotEqual(result.returncode, 0)
        self.assertIn('SyntaxError', result.stderr); self.assertNotIn('SIBLING_EXECUTED_BEFORE_COMPILE', result.stderr)

    def test_exact_cli_and_owned_target_routing_reject_unexpected_inputs(self):
        for arguments in (['source'], ['frozen', '--help', 'extra'],
                ['frozen', '--manifest', 'relative.json', '--dist', str(self.root)]):
            result = self.command(arguments=arguments)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('only exact frozen layout or help arguments', result.stderr)
        for arguments in (['layout-import-preflight', 'extra'], ['original-layout-probe', '../outside'],
                          ['copied-layout-probe', '/arbitrary/path'], ['unknown-mode']):
            result = self.command('main', arguments)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('LAYOUT_ARGS=', result.stdout)
        result = self.command('main', ['layout-import-preflight'])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_support_module_origin_and_module_execution_failure_are_rejected(self):
        self.sources['backend/app/openvino_native_bootstrap.py'] += b"__file__ = 'outside.py'\n"
        self.write_sources()
        result = self.command(); self.assertNotEqual(result.returncode, 0)
        self.assertIn('support module origin is not the sealed backend', result.stderr)
        self.sources['backend/app/openvino_native_bootstrap.py'] = b'from .openvino_import_privacy import LAYOUT_IMPORT_SENTINEL\n'
        self.sources['scripts/verify_openvino_windows.py'] = (self.verifier + "\nraise RuntimeError('MODULE_EXECUTION_FAILED')\n").encode()
        self.write_sources()
        result = self.command(); self.assertNotEqual(result.returncode, 0)
        self.assertIn('MODULE_EXECUTION_FAILED', result.stderr)
        self.assertNotIn('Private module cleanup failed', result.stderr)

    def test_remaining_real_script_help_entries_stay_isolated_without_product_or_sdk(self):
        driver = self.base / 'guarded real help.py'
        driver.write_text('''import runpy, sys
class Guard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'app', 'openvino', 'openvino_telemetry'}:
            raise RuntimeError('FORBIDDEN_APP_OR_SDK_IMPORT')
def audit(event, args):
    if event in {'subprocess.Popen', 'socket.connect'}:
        raise RuntimeError('FORBIDDEN_PROCESS_OR_NETWORK')
    if event == 'ctypes.dlopen' and args and any(name in str(args[0]).lower() for name in ('openvino', 'telemetry', 'tbb')):
        raise RuntimeError('FORBIDDEN_NATIVE_RUNTIME')
sys.meta_path.insert(0, Guard())
sys.addaudithook(audit)
assert sys.flags.isolated and sys.flags.ignore_environment and sys.flags.no_user_site
script = sys.argv[1]
sys.argv = [script, '--help']
runpy.run_path(script, run_name='__main__')
''', encoding='utf-8')
        for name, option in (('install_python_dependencies.py', '--project-root'),
                             ('download_person_models.py', '--check'),
                             ('verify_frozen_core_service.py', '--executable')):
            with self.subTest(script=name):
                result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', str(driver),
                    str(self.original / 'scripts' / name)], cwd=self.shadow,
                    env=dict(os.environ, PYTHONPATH=str(self.shadow)), capture_output=True,
                    text=True, encoding='utf-8', timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(option, result.stdout)


if __name__ == '__main__':
    unittest.main()
