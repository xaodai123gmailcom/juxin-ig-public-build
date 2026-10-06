"""Offline source/mutation checks: no application, dependency or native imports."""
import ast
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_build_contract import (assert_public_build_chain, assert_recovery_gate,
    assert_ui_dependencies, assert_runtime_imports, assert_browser_prerequisites,
    assert_native_logging_wiring, assert_unicode_runtime_wiring, function, read_sources)

ROOT = Path(__file__).resolve().parents[1]


class PublicBuildContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = read_sources(ROOT)

    def chain(self, **overrides):
        sources = dict(self.sources, **overrides)
        assert_public_build_chain(sources['workflow'], sources['wrapper'], sources['common'])

    def test_actual_public_and_local_mandatory_chain(self):
        self.chain()
        assert_recovery_gate(self.sources['build'])
        assert_ui_dependencies(self.sources['wrapper'], self.sources['build'], self.sources['install'])
        assert_runtime_imports(self.sources['build'])
        assert_browser_prerequisites(self.sources['early'], self.sources['build'])
        assert_native_logging_wiring(self.sources['wrapper'], self.sources['build'], self.sources['pwsh'])
        assert_unicode_runtime_wiring(self.sources['wrapper'], self.sources['unicode'], self.sources['verifier'])

    def test_missing_optional_or_ignored_workflow_build_is_rejected(self):
        source = self.sources['workflow']
        call = '        run: python -I -X utf8 ci/public_ci.py build'
        for replacement in ('        run: echo skipped',
                            '        # run: python -I -X utf8 ci/public_ci.py build',
                            '        if: false\n' + call,
                            '        continue-on-error: true\n' + call,
                            call + ' || exit /b 0'):
            with self.subTest(replacement=replacement), self.assertRaises(AssertionError):
                self.chain(workflow=source.replace(call, replacement, 1))
        with self.assertRaises(AssertionError):
            self.chain(workflow=source.replace('github.event.repository.private == false',
                                              'github.event.repository.private == false && false', 1))

    def test_missing_conditional_or_wrong_wrapper_build_is_rejected(self):
        tree = ast.parse(self.sources['wrapper'])
        build = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'build')
        index = next(index for index, node in enumerate(build.body)
                     if isinstance(node, ast.Expr) and 'full-original-windows-build' in ast.unparse(node))
        original = build.body[index]
        mutations = [ast.Pass(), ast.If(test=ast.Constant(False), body=[original], orelse=[]),
                     ast.Try(body=[original], handlers=[ast.ExceptHandler(type=None, name=None, body=[ast.Pass()])],
                             orelse=[], finalbody=[])]
        for mutation in mutations:
            with self.subTest(mutation=type(mutation).__name__), self.assertRaises(AssertionError):
                build.body[index] = mutation
                self.chain(wrapper=ast.unparse(ast.fix_missing_locations(tree)))
        build.body[index] = original
        for before, after in (("powershell('scripts/build_windows.ps1')", "powershell('scripts/install_windows.ps1')"),
                              ("['-BrowserMode', 'installed-chrome']", "['-PortableOnly']"),
                              ("['status'] == 'passed'", "['status'] != 'passed'"),
                              ("str(ROOT / script)", "str(ROOT / 'scripts/install_windows.ps1')")):
            with self.subTest(after=after), self.assertRaises(AssertionError):
                self.chain(wrapper=self.sources['wrapper'].replace(before, after, 1))

    def test_owned_build_nonzero_or_incomplete_status_cannot_pass(self):
        for before, after in (("receipt['targetExitCode'] == 0", "True"),
                              ("receipt['outcome'] == 'completed'", "True"),
                              ('runner.terminal_receipt(receipt)', 'True')):
            with self.subTest(before=before), self.assertRaises(AssertionError):
                self.chain(common=self.sources['common'].replace(before, after))

    def test_local_recovery_cannot_be_removed_skipped_or_weakened(self):
        source = self.sources['build']
        start = source.index('$BuildRecoveryExitCode =')
        stop = source.index('\n\n$BrowserDownloadExitCode', start)
        gate = source[start:stop]
        mutations = [source[:start] + source[stop:],
                     source.replace(gate, 'if ($false) {\n' + gate + '\n}', 1),
                     source.replace('test_build_recovery_r94.py', 'test_other.py', 1),
                     source.replace('if ($BuildRecoveryExitCode -ne 0)', 'if ($BuildRecoveryExitCode -eq 0)', 1),
                     source.replace('    throw "Verify build retry', '    Write-Warning "Verify build retry', 1),
                     source.replace(gate, '', 1).replace('$InstallerSucceeded = $false', gate + '\n$InstallerSucceeded = $false', 1)]
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=index), self.assertRaises(AssertionError):
                assert_recovery_gate(mutation)

    def test_runtime_imports_and_prerequisite_order_cannot_be_dropped(self):
        for token in ('"--collect-all", "playwright"', '"--hidden-import", "websockets.sync.client"'):
            with self.subTest(token=token), self.assertRaises(AssertionError):
                assert_runtime_imports(self.sources['build'].replace(token, '', 1))
            with self.subTest(commented=token), self.assertRaises(AssertionError):
                assert_runtime_imports(self.sources['build'].replace(token, '# ' + token, 1))
        with self.assertRaises(AssertionError):
            assert_ui_dependencies(self.sources['wrapper'].replace("'--include=dev'", "'--omit=dev'", 1),
                                   self.sources['build'], self.sources['install'])
        with self.assertRaises(AssertionError):
            assert_browser_prerequisites(self.sources['early'].replace("'early-browser-selection'", "'skipped-selection'", 1),
                                         self.sources['build'])

    def test_wrapper_executes_required_build_and_propagates_failure(self):
        # Execute only the inspected build function AST with all I/O replaced.
        # In particular, importing public_ci/common would not be necessary here.
        definition = function(self.sources['wrapper'], 'build')
        code = compile(ast.Module(body=[definition], type_ignores=[]), '<mocked public build>', 'exec')
        for failure in (None, 'full-original-windows-build', 'unicode-copied-frozen-runtime'):
            calls = []
            validated = []
            events = []
            class AbsentPath:
                def __truediv__(self, value):
                    return self
                def exists(self):
                    return False
            def required(condition, message):
                if not condition:
                    raise RuntimeError(message)
            def run_owned(label, command, timeout):
                calls.append((label, command, timeout))
                events.append(label)
                if label == failure:
                    raise RuntimeError('injected mandatory build failure')
            state = {'nonce': 'synthetic'}
            def validate(value):
                validated.append(value)
                events.append('validate_source_build')
                return {'verified': True}
            scope = {'verify_run_state': lambda: state, 'ROOT': AbsentPath(), 'state_root': AbsentPath,
                     'read_json': lambda path: {'status': 'passed'}, 'require': required,
                     'run_owned': run_owned, 'powershell': lambda script: ['mock-powershell', script], 'sys': sys,
                     'validate_source_build': validate}
            exec(code, scope)
            if failure:
                with self.assertRaisesRegex(RuntimeError, 'injected mandatory build failure'):
                    scope['build']()
                self.assertEqual([] if failure == 'full-original-windows-build' else [state], validated)
            else:
                self.assertEqual({'verified': True}, scope['build']())
                self.assertEqual([state, state], validated)
            self.assertEqual(('full-original-windows-build', ['mock-powershell', 'scripts/build_windows.ps1',
                             '-BrowserMode', 'installed-chrome'], 10800), calls[1])
            expected = ['build-existing-prerequisites', 'full-original-windows-build', 'validate_source_build',
                        'unicode-copied-frozen-runtime', 'validate_source_build']
            self.assertEqual(expected if failure is None else expected[:expected.index(failure) + 1], events)

    def test_restored_native_logging_gate_cannot_be_skipped(self):
        wrapper, build, pwsh = (self.sources[name] for name in ('wrapper', 'build', 'pwsh'))
        with self.assertRaises(AssertionError):
            assert_native_logging_wiring(wrapper.replace("'powershell7-native-command-logging'", "'removed-logging'", 1), build, pwsh)
        for before, after in (('$PSNativeCommandUseErrorActionPreference = $true', '$PSNativeCommandUseErrorActionPreference = $false'),
                              ('& (Join-Path', '# & (Join-Path'),
                              ('if (-not $PSNativeCommandUseErrorActionPreference)', 'if ($false)')):
            with self.subTest(before=before), self.assertRaises(AssertionError):
                assert_native_logging_wiring(wrapper, build, pwsh.replace(before, after, 1))

    def test_restored_unicode_boundaries_cannot_be_removed_or_weakened(self):
        wrapper, unicode, verifier = (self.sources[name] for name in ('wrapper', 'unicode', 'verifier'))
        for label in ('unicode-source-runtime', 'unicode-copied-frozen-runtime'):
            with self.subTest(label=label), self.assertRaises(AssertionError):
                assert_unicode_runtime_wiring(wrapper.replace(label, 'removed-' + label, 1), unicode, verifier)
        for before, after in (("('source-probe', 'classifier-probe')", "('source-probe',)"),
                              ('face, gender, person, attribute =', '_, gender ='),
                              ("'IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE': '1'", "'IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE': '0'"),
                              ('not str(original_dll).isascii()', 'str(original_dll).isascii()'),
                              ('result.checked and result.reason', 'result.reason'),
                              ("'-RequireNonAsciiSource', ", ''),
                              ("'-ExpectedCacheRoot', str(cache)", "'-ExpectedCacheRoot', str(work)"),
                              ("'unicode-frozen-real-service'", "'removed-frozen-service'")):
            with self.subTest(before=before), self.assertRaises(AssertionError):
                assert_unicode_runtime_wiring(wrapper, unicode.replace(before, after, 1), verifier)
        with self.assertRaises(AssertionError):
            assert_unicode_runtime_wiring(wrapper, unicode, verifier.replace('"person-detection-retail-0013.xml",', ''))

    def test_original_wiring_methods_without_native_or_fixture_imports(self):
        # The complete modules include real pip/npm/model/process fixtures.
        # Load their source-only wiring classes rather than importing them.
        suite = unittest.TestSuite()
        for path, name in (('scripts/tests/test_build_recovery_r94.py', 'BuildRecoveryWiringTests'),
                           ('scripts/tests/test_release_packaging_r94.py', 'ReleaseWiringTests'),
                           ('backend/tests/test_openvino_windows_packaging.py', 'OpenVinoWindowsPackagingTests')):
            tree = ast.parse((ROOT / path).read_text(encoding='utf-8-sig'))
            definition = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
            if name == 'OpenVinoWindowsPackagingTests':
                selected = {'test_windows_build_uses_safe_native_logging_for_both_packagers',
                            'test_windows_native_logging_behavior_probe_covers_success_failure_and_log_error',
                            'test_public_workflow_uses_safe_native_logging_in_both_powershell_hosts',
                            'test_unicode_classifier_smoke_unpacks_all_four_compiled_models'}
                definition.body = [node for node in definition.body if isinstance(node, ast.FunctionDef) and node.name in selected]
                self.assertEqual(len(selected), len(definition.body))
            scope = dict(globals())
            scope['PROJECT_ROOT'] = ROOT
            exec(compile(ast.Module(body=[definition], type_ignores=[]), path, 'exec'), scope)
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(scope[name]))
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(11, result.testsRun)
        self.assertEqual([], result.errors + result.failures + result.skipped)


if __name__ == '__main__':
    unittest.main()
