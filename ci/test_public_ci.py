"""Static and mocked public orchestration tests; no product or model is executed."""
import ast
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import traceback
from types import SimpleNamespace, TracebackType
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import public_ci as ci
import public_ci_common as common


def identity():
    return dict(schema=2, mode='flat-git-ci', representation='public-sanitized-source',
        repository=dict(common.REPOSITORY), source_commit='a'*40,
        source_manifest_sha256='b'*64, source_marker_sha256='c'*64,
        git_tree='d'*40, git_blob_paths_sha256='e'*64,
        tracked_files_verified=20, effective_files_verified=18)


class PublicContracts(unittest.TestCase):
    def test_retired_routes_require_exact_frozen_http_rejection(self):
        proof={'verified':True,'http_status':404,'endpoints':[
            {'path':'/api/posting/snapshot','method':'GET'},
            {'path':'/api/posting/command','method':'POST'},
            {'path':'/api/internal/integrations/pexels','method':'POST'}]}
        self.assertIs(common.validate_posting_removal(proof),proof)
        for key in proof:
            bad=copy.deepcopy(proof);bad.pop(key)
            with self.subTest(missing=key),self.assertRaises(RuntimeError):
                common.validate_posting_removal(bad)
        mutations=[dict(proof,verified=1),dict(proof,http_status=200),
            dict(proof,endpoints=proof['endpoints'][:-1]),
            dict(proof,endpoints=list(reversed(proof['endpoints']))),
            dict(proof,unexpected='alias')]
        for bad in mutations:
            with self.assertRaises(RuntimeError):common.validate_posting_removal(bad)
        for endpoint in range(3):
            bad=copy.deepcopy(proof);bad['endpoints'][endpoint]['method']='DELETE'
            with self.assertRaises(RuntimeError):common.validate_posting_removal(bad)

    def test_installed_failure_reads_only_exact_child_log_and_remains_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'scripts/verify_installed_recovery_r64.py'
            source.parent.mkdir()
            source.write_text('raise RuntimeError("private-message-token")\n', encoding='utf-8')
            common.write_json(root / 'SOURCE_SHA256.json', {'scripts/verify_installed_recovery_r64.py':'a'*64})
            log = root / 'installer-output/installed-recovery-r64-stdout-full.log'
            log.parent.mkdir()
            try:
                exec(compile(source.read_text(), str(source), 'exec'), {})
            except RuntimeError:
                with log.open('w', encoding='utf-8') as stream:
                    traceback.print_exc(file=stream)
            failure = RuntimeError('outer process rejected')
            with patch.object(ci, 'ROOT', root), patch.object(common, 'ROOT', root), \
                 patch.object(ci, 'state_root', return_value=root), patch.object(common, 'state_root', return_value=root), \
                 patch.object(ci, 'verify_run_state', return_value={'nonce':'a'*32}), \
                 patch.object(ci, 'read_json', return_value={'status':'passed'}), \
                 patch.object(ci, 'validate_source_build'), patch.object(ci, 'powershell', return_value=['powershell']), \
                 patch.object(ci, 'run_owned', side_effect=failure) as run, \
                 patch.object(common, 'bounded_log_tail', wraps=common.bounded_log_tail) as tail, \
                 patch.object(ci.runpy, 'run_path') as validate, patch.object(ci, 'installed_hashes') as hashes:
                with self.assertRaises(RuntimeError) as caught:
                    ci.installed()
                self.assertIs(caught.exception, failure)
                run.assert_called_once_with('actual-installed-product-acceptance', ['powershell'], 3600)
                tail.assert_called_once_with(log)
                validate.assert_not_called(); hashes.assert_not_called()
                summary = common.failure_diagnostics()
            record = summary['records'][0]
            self.assertEqual(record['gate'], 'installed-recovery-child')
            self.assertEqual(record['outcome'], 'validation-failed')
            self.assertIsNone(record['exit_code'])
            self.assertTrue(record['diagnostic_parse_succeeded'])
            self.assertEqual(record['source_locations'], [{'file':'scripts/verify_installed_recovery_r64.py','line':1}])
            self.assertEqual(record['observed_exception_categories'], ['RuntimeError'])
            for private in (str(root), 'private-message-token', 'outer process rejected', 'Traceback'):
                self.assertNotIn(private, json.dumps(summary))

    def test_installed_diagnostic_missing_log_or_parser_error_never_masks_gate_failure(self):
        for missing in (True, False):
            with self.subTest(missing_log=missing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                common.write_json(root / 'SOURCE_SHA256.json', {})
                failure = RuntimeError('original installed failure')
                with patch.object(ci, 'ROOT', root), patch.object(common, 'ROOT', root), \
                     patch.object(ci, 'state_root', return_value=root), patch.object(common, 'state_root', return_value=root), \
                     patch.object(ci, 'verify_run_state', return_value={'nonce':'a'*32}), \
                     patch.object(ci, 'read_json', return_value={'status':'passed'}), \
                     patch.object(ci, 'validate_source_build'), patch.object(ci, 'powershell', return_value=['powershell']), \
                     patch.object(ci, 'run_owned', side_effect=failure), \
                     patch.object(common, 'parse_diagnostic_tails', side_effect=RuntimeError('private parser error')):
                    if not missing:
                        log = root / 'installer-output/installed-recovery-r64-stdout-full.log'
                        log.parent.mkdir(); log.write_text('RuntimeError: private-child-message')
                    with self.assertRaises(RuntimeError) as caught:
                        ci.installed()
                    self.assertIs(caught.exception, failure)
                    record = common.failure_diagnostics()['records'][0]
                self.assertFalse(record['diagnostic_parse_succeeded'])
                self.assertTrue(all(record[field] == [] for field in common.DIAGNOSTIC_LIST_FIELDS))

    def test_successful_installed_gate_still_validates_and_does_not_capture_failure(self):
        with patch.object(ci, 'verify_run_state', return_value={'nonce':'a'*32}), \
             patch.object(ci, 'state_root', return_value=Path('/synthetic-run')), \
             patch.object(ci, 'read_json', return_value={'status':'passed'}), \
             patch.object(ci, 'validate_source_build'), patch.object(ci, 'powershell', return_value=['powershell']), \
             patch.object(ci, 'run_owned') as run, patch.object(ci.runpy, 'run_path') as validate, \
             patch.object(ci, 'installed_hashes', return_value={'synthetic':'hash'}) as hashes, \
             patch.object(ci, 'save_failure_diagnostic') as diagnostic:
            self.assertEqual(ci.installed(), {'synthetic':'hash'})
            run.assert_called_once_with('actual-installed-product-acceptance', ['powershell'], 3600)
            validate.assert_called_once_with(str(ci.ROOT / 'ci/public_ci_validate_installed.py'))
            hashes.assert_called_once_with(); diagnostic.assert_not_called()

    def test_exact_actions_and_read_only_repository_guard(self):
        workflow=(HERE.parent/'.github/workflows/public-windows-verify.yml').read_text()
        self.assertIn('contents: read',workflow)
        for text in ('1406784621','337452708',common.REPOSITORY['full_name'],
                     'github.event.repository.private == false','runs-on: windows-2022'):
            self.assertIn(text,workflow)
        pins=re.findall(r'uses: ([^\s]+)',workflow)
        self.assertEqual(pins,[
            'actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683',
            'actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065',
            'actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020',
            'actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683',
            'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02'])
        self.assertEqual(2,workflow.count('ref: ${{ github.sha }}'))
        self.assertEqual(2,workflow.count('persist-credentials: false'))
        for forbidden in ('continue-on-error','pull_request_target','secrets.','github.token','contents: write'):
            self.assertNotIn(forbidden,workflow)

    def test_only_successful_export_can_upload_three_exact_summary_paths(self):
        workflow=(HERE.parent/'.github/workflows/public-windows-verify.yml').read_text()
        upload=workflow.split('- name: Export only the three reviewed JSON summaries')[1]
        self.assertIn("if: always() && steps.export_proof.outcome == 'success'",upload)
        self.assertIn('if-no-files-found: error',upload)
        paths=re.findall(r'^            (\$\{\{ runner.temp \}\}/[^\n]+)$',upload,re.M)
        self.assertEqual([Path(p).name for p in paths],list(common.PUBLIC_FILES))
        self.assertTrue(all('*' not in p and not p.endswith(('.exe','.zip','.log','.png')) for p in paths))
        self.assertNotIn('installer-output/',upload)

    def test_real_git_source_binding_suite_is_an_early_required_contract(self):
        tree=ast.parse((HERE/'public_ci.py').read_text())
        contracts=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='contracts')
        statement=next(n for n in contracts.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
            and isinstance(n.value.func,ast.Name) and n.value.func.id=='run_owned'
            and n.value.args and isinstance(n.value.args[0],ast.Constant)
            and n.value.args[0].value=='contract-flat-git-source-binding')
        calls=[]
        scope={'ROOT':Path('/synthetic'),'sys':SimpleNamespace(executable='verified-python'),
               'run_owned':lambda *args:calls.append(args)}
        module=ast.Module(body=[statement],type_ignores=[])
        exec(compile(module,'<mock source binding contract>','exec'),scope)
        self.assertEqual(calls,[('contract-flat-git-source-binding', ['verified-python','-I','-B','-X','utf8',
            str(Path('/synthetic/scripts/tests/test_ci_source_binding.py')),'-v'],180)])
        scope['run_owned']=lambda *args: (_ for _ in ()).throw(RuntimeError('mock source binding failure'))
        with self.assertRaisesRegex(RuntimeError,'mock source binding failure'):
            exec(compile(module,'<mock source binding contract>','exec'),scope)
        workflow=(HERE.parent/'.github/workflows/public-windows-verify.yml').read_text()
        self.assertLess(workflow.index('ci/public_ci.py contracts'),workflow.index('ci/public_ci.py early'))

    def test_unicode_layout_import_preflight_is_isolated_and_before_expensive_work(self):
        tree=ast.parse((HERE/'public_ci.py').read_text())
        contracts=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='contracts')
        statements=[n for n in contracts.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
            and isinstance(n.value.func,ast.Name) and n.value.func.id=='run_owned'
            and n.value.args and isinstance(n.value.args[0],ast.Constant)]
        by_label={n.value.args[0].value:n for n in statements}
        statement=by_label['unicode-layout-import-preflight']
        self.assertLess(contracts.body.index(by_label['contract-flat-git-source-binding']),
                        contracts.body.index(statement))
        self.assertLess(contracts.body.index(statement),
                        contracts.body.index(by_label['contract-owned-process-wait-diagnostics']))
        calls=[]
        scope={'ROOT':Path('/synthetic'),'sys':SimpleNamespace(executable='verified-python'),
               'run_owned':lambda *args:calls.append(args)}
        module=ast.Module(body=[statement],type_ignores=[])
        exec(compile(module,'<mock isolated layout preflight>','exec'),scope)
        self.assertEqual(calls,[('unicode-layout-import-preflight',
            ['verified-python','-I','-B','-X','utf8',str(Path('/synthetic/ci/public_ci_unicode.py')),
             'layout-import-preflight'],90)])
        scope['run_owned']=lambda *args: (_ for _ in ()).throw(RuntimeError('mock isolated import failure'))
        with self.assertRaisesRegex(RuntimeError,'mock isolated import failure'):
            exec(compile(module,'<mock isolated layout preflight>','exec'),scope)
        workflow=(HERE.parent/'.github/workflows/public-windows-verify.yml').read_text()
        self.assertLess(workflow.index('ci/public_ci.py contracts'),workflow.index('ci/public_ci.py early'))
        self.assertLess(workflow.index('ci/public_ci.py early'),workflow.index('ci/public_ci.py build'))

    def test_original_full_build_and_separate_installed_gate_are_mandatory(self):
        text=(HERE/'public_ci.py').read_text()
        workflow=(HERE.parent/'.github/workflows/public-windows-verify.yml').read_text()
        self.assertLess(workflow.index('ci/public_ci.py early'),workflow.index('fresh build tree'))
        self.assertLess(workflow.index('fresh build tree'),workflow.index('ci/public_ci.py build'))
        self.assertLess(workflow.index('ci/public_ci.py build'),workflow.index('ci/public_ci.py installed'))
        self.assertIn("powershell('scripts/build_windows.ps1')",text)
        self.assertIn("['-BrowserMode', 'installed-chrome']",text)
        self.assertNotIn('PortableOnly',text)
        for name in ('test_cloud_configuration_public.py','test_python_environment.py','test_timezone_data_r57.py'):
            self.assertIn(name,text)
        for text in (text,(HERE/'public_ci_verify_installed.ps1').read_text()):
            for forbidden in ('ExecutionPolicy','taskkill','RunAs','Set-ExecutionPolicy','opt_out('):
                self.assertNotIn(forbidden,text)

    def test_wait_diagnostic_mock_suite_is_a_required_contract(self):
        calls=[]
        expected=('contract-owned-process-wait-diagnostics',
            ['base-python','-I','-B','-X','utf8',
             str(Path('/synthetic/scripts/tests/test_owned_process_wait_diagnostics.py')),'-v'],180)
        with patch.object(ci,'ROOT',Path('/synthetic')),\
             patch.object(ci,'sys',SimpleNamespace(executable='base-python')),\
             patch.object(ci,'powershell',side_effect=lambda script:['powershell',script]),\
             patch.object(ci,'run_owned',side_effect=lambda *args:calls.append(args)):
            ci.contracts()
            self.assertEqual([call for call in calls if call[0]==expected[0]],[expected])
            failure=RuntimeError('mock wait diagnostic contract failure')
            def fail(*args):
                if args[0]==expected[0]:raise failure
            with patch.object(ci,'run_owned',side_effect=fail),self.assertRaises(RuntimeError) as caught:
                ci.contracts()
            self.assertIs(caught.exception,failure)

    def test_owned_descendant_prechecks_use_both_interpreters_before_dependencies(self):
        calls=[];root=Path('/synthetic')
        native=str(root/'scripts/tests/test_owned_process_windows_r64.py')
        method='NativeWindowsOwnership.test_parent_exit_and_inherited_output_descendant_cleanup'
        experiment='NativeWindowsOwnership.test_verified_parent_exit_and_inherited_output_descendant_cleanup'
        expected=[
            ('early-existing-prerequisites',['powershell','ci/public_ci_runner_prerequisites.ps1'],90),
            ('early-create-venv',['base-python','-I','-X','utf8','-m','venv','.venv'],180),
            ('early-owned-verified-descendant-base',['base-python','-I','-X','utf8',native,experiment,'-v'],60),
            ('early-owned-descendant-base',['base-python','-I','-X','utf8',native,method,'-v'],60),
            ('early-owned-verified-descendant-venv',[str(root/'.venv/Scripts/python.exe'),'-I','-X','utf8',native,experiment,'-v'],60),
            ('early-owned-descendant-venv',[str(root/'.venv/Scripts/python.exe'),'-I','-X','utf8',native,method,'-v'],60)]
        with patch.object(ci,'ROOT',root),patch.object(ci,'sys',SimpleNamespace(executable='base-python')),\
             patch.object(ci,'powershell',side_effect=lambda script:['powershell',script]),\
             patch.object(ci,'state_root',return_value=root/'state'),patch.object(ci,'digest',return_value='a'*64),\
             patch.object(ci,'run_owned',side_effect=lambda *args:calls.append(args)):
            ci.early()
            self.assertEqual(calls[:6],expected)
            self.assertEqual(calls[6][0],'early-python-dependencies')
            self.assertEqual(calls[7],('early-service-startup-regressions',
                [str(root/'.venv/Scripts/python.exe'),'-I','-X','utf8',
                 'scripts/run_backend_tests.py','-p','test_frozen_service_r94.py',
                 '--case-timeout','180','-v'],600))
            self.assertEqual(calls[8][0],'unicode-source-runtime')
            startup_calls=calls[:8]
            calls.clear();failure=RuntimeError('mock real startup regression')
            def fail_startup(*args):
                calls.append(args)
                if args[0]=='early-service-startup-regressions':raise failure
            with patch.object(ci,'run_owned',side_effect=fail_startup),self.assertRaises(RuntimeError) as caught:
                ci.early()
            self.assertIs(caught.exception,failure)
            self.assertEqual(calls,startup_calls)
            for failed in expected[1:]:
                with self.subTest(failed=failed[0]):
                    calls.clear();failure=RuntimeError('mock owned descendant failure')
                    def fail(*args):
                        calls.append(args)
                        if args[0]==failed[0]:raise failure
                    with patch.object(ci,'run_owned',side_effect=fail),self.assertRaises(RuntimeError) as caught:
                        ci.early()
                    self.assertIs(caught.exception,failure)
                    self.assertEqual(calls,expected[:expected.index(failed)+1])
        package=json.loads((HERE.parent/'package.json').read_text())
        self.assertIn('python scripts/tests/test_owned_process_r64.py && '
                      'python scripts/tests/test_owned_process_windows_r64.py && ',package['scripts']['test:source'])

    def test_retained_navigation_contract_is_additive_early_and_fail_closed(self):
        root=Path('/synthetic');calls=[]
        expected=('early-retained-navigation-contract',
                  ['node','--test','desktop/tests/navigation-contract-r65.test.cjs'],180)
        with patch.object(ci,'ROOT',root),patch.object(ci,'sys',SimpleNamespace(executable='base-python')),\
             patch.object(ci,'powershell',side_effect=lambda script:['powershell',script]),\
             patch.object(ci,'state_root',return_value=root/'state'),patch.object(ci,'digest',return_value='a'*64),\
             patch.object(ci,'run_owned',side_effect=lambda *args:calls.append(args)):
            ci.early()
            self.assertEqual([call for call in calls if call[0]==expected[0]],[expected])
            labels=[call[0] for call in calls]
            self.assertEqual(labels.index(expected[0]),labels.index('early-node-dependencies')+1)
            self.assertLess(labels.index(expected[0]),labels.index('early-build-ui'))
            report=('early-retained-report-regressions',
                    [str(root/'.venv/Scripts/python.exe'),'-I','-X','utf8',
                     'scripts/run_backend_tests.py','-p','test_continuation.py',
                     '--case-timeout','180','-v'],600)
            self.assertEqual([call for call in calls if call[0]==report[0]],[report])
            self.assertEqual(labels.index(report[0]),labels.index(expected[0])+1)
            self.assertLess(labels.index(report[0]),labels.index('early-build-ui'))
            performance=('early-window-snapshot-performance',
                    [str(root/'.venv/Scripts/python.exe'),'-I','-X','utf8',
                     'scripts/run_backend_tests.py','-p','test_window_performance_r33.py',
                     '--case-timeout','180','-v'],600)
            self.assertEqual([call for call in calls if call[0]==performance[0]],[performance])
            self.assertEqual(labels.index(performance[0]),labels.index(report[0])+1)
            self.assertLess(labels.index(performance[0]),labels.index('early-build-ui'))
            self.assertIn('early-all-required-regressions',labels)
            all_calls=list(calls)
            for failed in (expected,report,performance):
                calls.clear();failure=RuntimeError('mock retained product mismatch')
                def fail(*args):
                    calls.append(args)
                    if args[0]==failed[0]:raise failure
                with patch.object(ci,'run_owned',side_effect=fail),self.assertRaises(RuntimeError) as caught:
                    ci.early()
                self.assertIs(caught.exception,failure)
                self.assertEqual(calls,all_calls[:all_calls.index(failed)+1])

    def test_installed_checks_preserve_scale_upgrade_and_actual_api(self):
        text=(HERE/'public_ci_verify_installed.ps1').read_text()
        for token in ('--pure-ig','--snapshot-scale','--collection-completion','--standalone-nurture',
                      '--nurture-cleanup-upgrade','441552','602831','posting_removed','$removed.http_status -ne 404',
                      'performance_indexes_verified.Count -ne 3','compact_wire.canonical_rows_equal',
                      'normal_restart.retained_data','pure_ig_upgrade.ig_identity_hash_preserved',
                      'completed_card_dismissal','repeated_startup_idempotent','four_card_totals',
                      'scripts\\verify_installed_recovery_r64.py',"'120'",'-TimeoutSeconds 180',
                      "'^INSTALLED_RECOVERY_R64=PASS '"):
            self.assertIn(token,text)
        self.assertLess(text.index('--nurture-cleanup-upgrade'),text.index('scripts\\verify_installed_recovery_r64.py'))
        stage=(HERE/'public_ci_validate_installed.py').read_text()
        for token in ('r63_upgrade_proof.py','r63_native_proof.py',
                      'r64_recovery_ui_proof.py','verify_installed_recovery_r64.py','validate_posting_removal',
                      "same_json(installed.get('recovery_api'),recovery_api)",'core_upgrade_manifest_sha256'):
            self.assertLess(stage.index(token),stage.index("print('PUBLIC_INSTALLED_ACCEPTANCE=PASS')"))

    def test_exact_nonposting_early_fixtures_and_all_retained_groups(self):
        text=(HERE/'public_ci_early.py').read_text();tree=ast.parse(text)
        loop=next(n for n in tree.body if isinstance(n,ast.For) and isinstance(n.target,ast.Name) and n.target.id=='fixture')
        fixtures=['desktop/tests/nurture-cleanup-native-r63.cjs','desktop/tests/nurture-reels-r6.integration.cjs',
            'desktop/tests/recovery-ui-native-r64.cjs']
        self.assertEqual(list(ast.literal_eval(loop.iter)),fixtures)
        groups=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='patterns' for t in n.targets))
        import public_ci_groups
        self.assertEqual(groups,public_ci_groups.PATTERNS);self.assertEqual(len(groups),27)
        for name in ('IGAC_REQUIRE_COLLECTION_BROWSER','IGAC_REQUIRE_NURTURE_BROWSER','IGAC_REQUIRE_PROFILE_BROWSER',
                     'IGAC_REQUIRE_STANDALONE_NURTURE_BROWSER','IGAC_REQUIRE_FINAL_SEED_BROWSER',
                     'JUXIN_REQUIRE_NURTURE_CLEANUP_NATIVE','JUXIN_REQUIRE_RECOVERY_UI_NATIVE'):
            self.assertIn(name+"='1'",text)
        self.assertIn("case_timeout='90' if pattern=='test_final_seed_browser_r62.py' else '180'",text)
        self.assertLess(text.index('if failures:'),text.index('cleanup_native=json.loads'))

    def test_saturation_precheck_uses_original_command_and_required_browser(self):
        tree=ast.parse((HERE/'public_ci_early.py').read_text())
        call=next(n for n in tree.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
            and isinstance(n.value.func,ast.Name) and n.value.func.id=='run_owned'
            and n.value.args and isinstance(n.value.args[0],ast.Constant)
            and n.value.args[0].value=='early-saturation-focused-precheck')
        native=next(n for n in tree.body if isinstance(n,ast.For)
            and isinstance(n.target,ast.Name) and n.target.id=='fixture')
        self.assertLess(tree.body.index(call),tree.body.index(native))
        calls=[];env={'IGAC_TEST_CHROMIUM_EXECUTABLE':'verified-chrome'}
        scope={'python':'verified-python','env':env,'run_owned':lambda *args:calls.append(args)}
        module=ast.Module(body=[call],type_ignores=[])
        exec(compile(module,'<mock saturation precheck>','exec'),scope)
        self.assertEqual(calls,[('early-saturation-focused-precheck', ['verified-python','-X','utf8',
            'scripts/run_backend_tests.py','-p','test_saturation_acceptance_r99.py','-v'],1800,
            dict(env,IGAC_REQUIRE_SATURATION_BROWSER='1'))])
        self.assertNotIn('IGAC_REQUIRE_SATURATION_BROWSER',env)
        scope['run_owned']=lambda *args: (_ for _ in ()).throw(RuntimeError('mock saturation failure'))
        with self.assertRaisesRegex(RuntimeError,'mock saturation failure'):
            exec(compile(module,'<mock saturation precheck>','exec'),scope)

    def test_removed_posting_and_crop_receipts_cannot_become_required_gates(self):
        retired = ('r64-crop-icon-native.json', 'r62-posting-viewport-native.json',
                   'r6-posting-stress.json', 'r6-posting-stress-bounds.json')
        for name in ('public_ci_early.py', 'public_ci_validate_installed.py', 'public_ci_common.py'):
            text=(HERE/name).read_text()
            for receipt in retired:
                self.assertNotIn(receipt,text)
        self.assertFalse((HERE/'r64_crop_proof.py').exists())
        self.assertFalse((HERE/'test-r64-crop-proof.py').exists())

    def test_native_loop_records_only_success_and_rejects_subsets_or_reordering(self):
        tree=ast.parse((HERE/'public_ci_early.py').read_text())
        loop=next(n for n in tree.body if isinstance(n,ast.For) and isinstance(n.target,ast.Name) and n.target.id=='fixture')
        expected=list(ast.literal_eval(loop.iter))
        for failed in (None,*expected):
            calls=[]
            scope={'native_fixtures':[],'root':Path('/synthetic'),'electron_cli':'never-executed',
                'print':lambda *a,**k:None,'run_required':lambda label,command,timeout:(calls.append((label,timeout)) or label!=failed)}
            exec(compile(ast.Module(body=[loop],type_ignores=[]),'<mock native fixture loop>','exec'),scope)
            self.assertEqual(scope['native_fixtures'],[name for name in expected if name!=failed])
            self.assertTrue(all(timeout==180 for _,timeout in calls))
        stage=ast.parse((HERE/'public_ci_validate_installed.py').read_text())
        check=next(n for n in stage.body if isinstance(n,ast.If) and 'native_fixtures' in ast.unparse(n.test))
        for value in (expected[:-1],expected+[expected[0]],list(reversed(expected)),None):
            with self.assertRaises(RuntimeError):
                exec(compile(ast.Module(body=[check],type_ignores=[]),'<mock early receipt>','exec'),{'early':{'native_fixtures':value}})
        exec(compile(ast.Module(body=[check],type_ignores=[]),'<mock early receipt>','exec'),{'early':{'native_fixtures':expected}})

    def test_early_failure_cannot_become_success(self):
        tree=ast.parse((HERE/'public_ci_early.py').read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_required')
        failures=[]
        scope={'run_owned':lambda *a: (_ for _ in ()).throw(RuntimeError('mock failure')),
            'native_fixtures':[],'failures':failures,'hashlib':hashlib,'env':{},'print':lambda *a,**k:None}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'<mock required gate>','exec'),scope)
        self.assertFalse(scope['run_required']('fixture',['never-execute'],180))
        self.assertEqual(failures,[{'check':'fixture','failed':True}])

    def test_native_full_source_binding_rejects_each_stale_field(self):
        source=identity()
        proof={'source_commit':source['source_commit'],'github_sha':source['source_commit'],'source_provenance':source}
        common.bind_native(proof,source)
        for key in proof:
            bad=copy.deepcopy(proof);bad.pop(key)
            with self.assertRaises(RuntimeError):common.bind_native(bad,source)
        bad=copy.deepcopy(proof);bad['source_provenance']['git_tree']='f'*40
        with self.assertRaises(RuntimeError):common.bind_native(bad,source)
        bad=copy.deepcopy(proof);bad['source_provenance']['schema']=2.0
        with self.assertRaises(RuntimeError):common.bind_native(bad,source)

    def test_raw_ascii_zero_is_exact_and_read_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'consent';path.write_bytes(b'1\n')
            with patch.object(common,'telemetry_file',return_value=path):
                common.establish_telemetry_consent();self.assertEqual(path.read_bytes(),b'0')
                common.check_telemetry_consent();path.write_bytes(b'0\n')
                with self.assertRaises(RuntimeError):common.check_telemetry_consent()

    def test_no_credentials_or_private_registry_are_forwarded(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(common,'state_root',return_value=Path(directory)):
            source={'PATH':'ordinary','GITHUB_TOKEN':'sensitive','GH_TOKEN':'sensitive','ACTIONS_RUNTIME_TOKEN':'sensitive',
                    'API_KEY':'sensitive','PIP_EXTRA_INDEX_URL':'https://private.invalid','PIP_CONFIG_FILE':'private',
                    'NPM_CONFIG_REGISTRY':'https://private.invalid','AWS_SECRET_ACCESS_KEY':'sensitive'}
            output=common.dependency_environment(source)
            self.assertFalse(any('sensitive' in value or 'private.invalid' in value for value in output.values()))
            self.assertEqual(output['PIP_INDEX_URL'],'https://pypi.org/simple')
            self.assertEqual(output['PIP_CONFIG_FILE'],os.devnull)
            self.assertEqual(output['NPM_CONFIG_REGISTRY'],'https://registry.npmjs.org/')
            self.assertEqual(output['TEMP'],output['TMP'])
            for key in ('NPM_CONFIG_USERCONFIG','NPM_CONFIG_GLOBALCONFIG'):
                self.assertEqual(Path(output[key]).read_bytes(),b'')

    def test_linked_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'target';target.write_bytes(b'fixture')
            link=Path(directory)/'link'
            try:link.symlink_to(target)
            except OSError:self.skipTest('OS does not permit unprivileged symlinks')
            with self.assertRaises(RuntimeError):common.digest(link)

    def test_unknown_fields_cannot_escape_identity_allowlist(self):
        source=identity();source['raw_log']='sensitive';source['archive_origin']={'private':'sensitive'}
        summary=ci.public_identity(source)
        self.assertNotIn('raw_log',summary);self.assertNotIn('archive_origin',summary)
        self.assertNotIn('sensitive',json.dumps(summary))

    def test_failed_and_not_run_stages_export_no_success_or_binary_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state={'run':{'fixture':True},'source_provenance':identity(),'nonce':'f'*32,'started_ns':1}
            statuses={'contracts':'passed','early':'failed','build':'not-run','installed':'not-run'}
            with patch.object(ci,'state_root',return_value=root),patch.object(ci,'verify_run_state',return_value=state),\
                 patch.object(ci,'phase_status',side_effect=lambda s,n:statuses[n]),patch.object(ci,'source_identity',return_value=identity()),\
                 patch.object(ci,'failure_diagnostics',return_value=[]):
                ci.export()
            self.assertEqual(set(p.name for p in (root/'public').iterdir()),set(common.PUBLIC_FILES))
            summary=json.loads((root/'public/run-summary.json').read_text())
            self.assertFalse(summary['all_required_stages_passed'])
            for name in ('source-build-proof.json','installed-acceptance-proof.json'):
                proof=json.loads((root/'public'/name).read_text());self.assertEqual(proof['status'],'not-run')
                self.assertEqual(proof['hashes'],{});self.assertFalse(proof['binary_exported'])

    def test_stale_stage_nonce_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);common.write_json(root/'build-result.json',{'nonce':'old','stage':'build','status':'passed'})
            with patch.object(ci,'state_root',return_value=root),self.assertRaises(RuntimeError):
                ci.phase_status({'nonce':'new'},'build')

    def test_selected_installer_identity_sidecar_freshness_and_source_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);output=root/'installer-output';output.mkdir()
            source=identity();state={'source_provenance':source}
            binary=output/'Juxin-IG-Audience-Collector-NewGen-Setup-3.0.4-x64.exe'
            # Deliberately non-executable mock bytes. This test never launches them.
            binary.write_bytes(b'SYNTHETIC-NONEXECUTABLE')
            sidecar=Path(str(binary)+'.sha256');sha=common.digest(binary)
            sidecar.write_text(sha+' *'+binary.name+'\n')
            proof=dict(verified=True,revision='stability-r94',version='3.0.4',
                source_commit=source['source_commit'],github_sha=source['source_commit'],
                source_provenance=source,manifestSha256=source['source_manifest_sha256'])
            common.write_json(output/'build-source.json',proof)
            common.write_json(root/'build/browsers/juxin-runtime-requirement.json',{'mode':'installed-chrome-required'})
            common.write_json(root/'build-start.json',{'started_ns':1})
            marker='\n'.join(('TYPE=INSTALLER','VERSION=3.0.4','PATH='+str(binary),
                'SHA256='+sha,'SHA256_PATH='+str(sidecar),'BROWSER_MODE=installed-chrome'))
            (output/'LATEST_SUCCESS.txt').write_text(marker)
            with patch.object(common,'ROOT',root),patch.object(common,'source_identity',return_value=source),\
                 patch.object(common,'state_root',return_value=root):
                self.assertEqual(common.validate_source_build(state)['installer_sha256'],sha)
                binary.write_bytes(b'changed')
                with self.assertRaises(RuntimeError):common.validate_source_build(state)
                binary.write_bytes(b'SYNTHETIC-NONEXECUTABLE')
                sidecar.write_text('wrong')
                with self.assertRaises(RuntimeError):common.validate_source_build(state)
                sidecar.write_text(sha+' *'+binary.name+'\n')
                extra=output/'Juxin-IG-Audience-Collector-NewGen-Setup-3.0.4-other.exe';extra.write_bytes(b'fixture')
                with self.assertRaises(RuntimeError):common.validate_source_build(state)
                extra.unlink()
                os.utime(binary,ns=(0,0))
                with self.assertRaises(RuntimeError):common.validate_source_build(state)

    def test_failed_owned_supervision_never_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);target=root/'fixture';target.write_bytes(b'never executable')
            captured=[]
            runner=SimpleNamespace(validate_runtime=lambda:None,
                direct_child_command=lambda command,env:command,Request=lambda **kw:SimpleNamespace(**kw),
                enforce_deadline=lambda *a:None,
                supervise=lambda request:(captured.append(request) or {'targetExitCode':0,'outcome':'execution-timeout'}),
                terminal_receipt=lambda receipt:False)
            with patch.object(common,'state_root',return_value=root),patch.object(common,'load_source_module',return_value=runner),\
                 patch.object(common,'check_telemetry_consent'),patch.dict(os.environ,{'GITHUB_TOKEN':'fixture-secret'}):
                with self.assertRaises(RuntimeError):common.run_owned('mock-owned-failure',[str(target)],1)
            self.assertEqual(captured[0].environment['GITHUB_TOKEN'],None)
            self.assertEqual(captured[0].timeoutSeconds,1)

    def test_failure_diagnostics_are_symbols_and_sealed_relative_locations_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'backend/tests/test_fixture.py';path.parent.mkdir(parents=True)
            path.write_text('def test_known_failure(self):\n    pass\n')
            tails=[f'test_known_failure secret-token=very-private\n  File "{path}", line 2, in test_known_failure\n',
                   'test_unrecognized_personal_name\n File "/home/private/profile.json", line 12\n']
            value=common.parse_diagnostic_tails(tails,{'backend/tests/test_fixture.py':'a'*64},root)
            self.assertEqual(value,{'test_ids':['test_known_failure'],
                'failed_test_ids':[],'observed_exception_categories':[],
                'source_locations':[{'file':'backend/tests/test_fixture.py','line':2}],
                **{field+'_omitted':0 for field in common.DIAGNOSTIC_LIST_FIELDS}})
            encoded=json.dumps(value)
            for secret in ('very-private','profile.json',str(root),'test_unrecognized'):
                self.assertNotIn(secret,encoded)

    def test_worst_case_failure_diagnostics_fit_json_byte_budget(self):
        record={'gate':'x'*96,'outcome':'target-exited-nonzero','exit_code':-2147483648,
            'diagnostic_parse_succeeded':True,'test_ids':['test_'+('a'*155)]*20,
            'failed_test_ids':['test_'+('z'*155)]*20,
            'observed_exception_categories':sorted(common.DIAGNOSTIC_EXCEPTION_CATEGORIES),
            'source_locations':[{'file':('a'*3000)+'.py','line':9999999}]*20,
            **{field+'_omitted':0 for field in common.DIAGNOSTIC_LIST_FIELDS}}
        record['test_ids_omitted']=7;record['failed_test_ids_omitted']=11;record['source_locations_omitted']=3
        summary=common.bound_diagnostic_records([copy.deepcopy(record) for _ in range(32)])
        self.assertLessEqual(len(json.dumps(summary,ensure_ascii=True,sort_keys=True,indent=2).encode()),24576)
        self.assertGreater(summary['records_omitted'],0)
        first=summary['records'][0]
        self.assertEqual(summary['detail_items_omitted'],32*sum(first[field+'_omitted'] for field in common.DIAGNOSTIC_LIST_FIELDS))
        self.assertTrue(summary['records'])
        self.assertEqual(first['exit_code'],-2147483648)
        self.assertTrue(first['failed_test_ids']);self.assertFalse(first['test_ids'])
        for item in summary['records']:
            self.assertLessEqual(len(json.dumps(item,ensure_ascii=True,sort_keys=True,indent=2).encode()),4096)
            for field in common.DIAGNOSTIC_LIST_FIELDS:
                self.assertEqual(len(item[field])+item[field+'_omitted'],len(record[field])+record[field+'_omitted'])
        self.assertEqual(len(record['test_ids']),20)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state={'run':{'fixture':True},'source_provenance':identity(),'nonce':'f'*32,'started_ns':1}
            with patch.object(ci,'state_root',return_value=root),patch.object(ci,'verify_run_state',return_value=state),\
                 patch.object(ci,'phase_status',return_value='failed'),patch.object(ci,'source_identity',return_value=identity()),\
                 patch.object(ci,'failure_diagnostics',return_value=summary):
                ci.export()
            self.assertEqual(set(path.name for path in (root/'public').iterdir()),set(common.PUBLIC_FILES))
            self.assertTrue(all(path.stat().st_size<=65536 for path in (root/'public').iterdir()))

    def test_prerequisites_are_read_only_and_match_runtime_and_chrome(self):
        text=(HERE/'public_ci_runner_prerequisites.ps1').read_text()
        for value in ('14.44.35112.0','MSVCP140.dll','VCRUNTIME140.dll','VCRUNTIME140_1.dll',
                      'Get-AuthenticodeSignature','Microsoft Corporation','Google (LLC|Inc)',
                      'ReparsePoint','Google\\Chrome\\Application\\chrome.exe'):
            self.assertIn(value,text)
        for forbidden in ('Start-Process','Invoke-WebRequest','RunAs','ForceRepair','ExecutionPolicy',
                          'Set-ItemProperty','Set-MpPreference'):
            self.assertNotIn(forbidden,text)
        main=(HERE/'public_ci.py').read_text()
        self.assertEqual(main.count("powershell('ci/public_ci_runner_prerequisites.ps1')"),3)

    def test_retained_early_and_installed_receipts_are_type_strict(self):
        stage=(HERE/'public_ci_validate_installed.py').read_text()
        for value in ('set(early_hashes)==set(EARLY_FILES)',
                      "same_json(scale_report.get('nurture_cleanup_upgrade'),installed['nurture_cleanup_upgrade'])",
                      "same_json(installed.get('recovery_api'),recovery_api)",
                      'same_json(early.get(field),read_json(early_root/name))',
                      "early_seed.get('live_accounts_tested') is False",'preflight_installed_evidence()'):
            self.assertIn(value,stage)

    def test_legacy_full_build_watchdogs_and_late_groups_are_preserved(self):
        script=(HERE.parent/'scripts/build_windows.ps1').read_text(encoding='utf-8-sig')
        start='& .venv\\Scripts\\python.exe -X utf8 scripts\\run_backend_tests.py -p "test_follow_monitor.py"'
        end='& .venv\\Scripts\\python.exe scripts\\verify_backend_smoke.py'
        tail=script[script.index(start):];tail=tail[:tail.index(end)]
        patterns=re.findall(r'run_backend_tests\.py(?:",\s*"-p",\s*"| -p ")([^"]+)"',tail)
        self.assertEqual(29,len(patterns));self.assertEqual('test_follow_monitor.py',patterns[0])
        self.assertEqual('test_account_updates.py',patterns[-1]);self.assertIn('test_final_seed_browser_r62.py',patterns)
        self.assertIn('$env:IGAC_TEST_CASE_TIMEOUT_SECONDS = "180"',script)
        core=(HERE.parent/'scripts/verify_r18_core.py').read_text()
        for value in ('from run_backend_tests import TimedTestResult',
                      'faulthandler.dump_traceback_later(TimedTestResult.case_timeout, exit=True)',
                      'unittest.TextTestRunner(verbosity=2, resultclass=TimedTestResult).run(suite)',
                      'raise SystemExit(not result.wasSuccessful())',
                      'test_incomplete_relationship_list_retries_without_network_reconnect'):
            self.assertIn(value,core)
        runner=(HERE.parent/'scripts/run_backend_tests.py').read_text()
        self.assertIn('default=os.environ.get("IGAC_TEST_CASE_TIMEOUT_SECONDS")',runner)

    def test_registered_native_renderer_gates_stay_mandatory(self):
        package=json.loads((HERE.parent/'package.json').read_text())
        for name in ('renderer/tests/hidden-collection-blocker-r63.test.mjs',):
            self.assertIn(name,package['scripts']['test:renderer'])
        self.assertIn('desktop/tests/recovery-ui-native-r64.test.cjs',package['scripts']['test:desktop'])
        script=(HERE.parent/'scripts/build_windows.ps1').read_text(encoding='utf-8-sig')
        self.assertLess(script.index('@("run", "build:electron")'),script.index('"desktop\\tests\\recovery-ui-native-r64.cjs"'))
        self.assertIn('JUXIN_REQUIRE_RECOVERY_UI_NATIVE = "1"',script)
        self.assertIn('if ($R64NativeExitCode -ne 0) { throw',script)
        source=(HERE.parent/'scripts/verify_build_source.mjs').read_text()
        for path in ('backend/app/browser_admission.py','backend/app/nurture_collection_blocker.py',
                     'backend/tests/test_combined_recovery_r64.py',
                     'scripts/verify_installed_recovery_r64.py','desktop/tests/recovery-ui-native-r64.cjs',
                     'renderer/tests/fixtures/recovery-ui-r64.tsx'):
            self.assertIn(path,source)


    def test_failed_test_headers_are_distinct_from_passing_context_and_private_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'test_fixture.py'
            names=['test_passed','test_failed','test_error','test_context']
            path.write_text(''.join(f'def {name}(self): pass\n' for name in names))
            tails=['\n'.join(('test_passed (fixture.Case.test_passed) ... ok',
                'test_context (fixture.Case.test_context) ... FAIL',
                'INFO FAIL: test_context (fixture.Case.test_context)',
                '  FAIL: test_context (fixture.Case.test_context)',
                '\x1b[31mFAIL: test_failed (fixture.Case.test_failed)\x1b[0m',
                'ERROR: test_error (fixture.Case.test_error) (secret="private-subtest")',
                'FAIL: test_failed_private_secret (fixture.Case.test_failed_private_secret)',
                'AssertionError: secret-token=private-assertion-value',
                'asyncio.exceptions.CancelledError: private-cancellation',
                'RuntimeErrorPrivateSecret: private-class-name',
                'INFO TimeoutError: prefixed-context',
                'test_context handled ValueError: private-handled-value'))]
            value=common.parse_diagnostic_tails(tails,{'test_fixture.py':'a'*64},root)
            self.assertEqual(value['test_ids'],sorted(names))
            self.assertEqual(value['failed_test_ids'],['test_error','test_failed'])
            self.assertEqual(value['observed_exception_categories'],['AssertionError','CancelledError'])
            self.assertTrue(all(value[field+'_omitted']==0 for field in common.DIAGNOSTIC_LIST_FIELDS))
            encoded=json.dumps(value)
            for secret in ('private','secret','fixture.Case',str(root),'RuntimeErrorPrivateSecret'):
                self.assertNotIn(secret,encoded)
            context=common.parse_diagnostic_tails(['test_context ... FAIL\nAssertionError: handled'],{'test_fixture.py':'a'*64},root)
            self.assertEqual(context['failed_test_ids'],[])
            self.assertEqual(context['observed_exception_categories'],['AssertionError'])

    def test_diagnostic_parser_reports_each_unique_item_lost_to_twenty_item_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'test_fixture.py'
            names=[f'test_case_{index:02d}' for index in range(47)]
            path.write_text(''.join(f'def {name}(self): pass\n' for name in names))
            lines=[f'{name} ... ok' for name in names[:23]]
            lines += [f'FAIL: {name} (fixture.Case.{name})' for name in names[23:]]
            lines += [f'  File "test_fixture.py", line {index+1}' for index in range(29)]
            lines += [f'{name}: private-value' for name in common.DIAGNOSTIC_EXCEPTION_CATEGORIES]
            value=common.parse_diagnostic_tails(['\n'.join(lines)]*2,{'test_fixture.py':'a'*64},root)
            self.assertEqual(value['test_ids'],names[:20]);self.assertEqual(value['test_ids_omitted'],27)
            self.assertEqual(value['failed_test_ids'],names[23:43]);self.assertEqual(value['failed_test_ids_omitted'],4)
            self.assertEqual(len(value['source_locations']),20);self.assertEqual(value['source_locations_omitted'],9)
            self.assertEqual(value['observed_exception_categories'],sorted(common.DIAGNOSTIC_EXCEPTION_CATEGORIES))
            self.assertEqual(value['observed_exception_categories_omitted'],0)
            record=dict(value,gate='mock-gate',outcome='target-exited-nonzero',exit_code=1,diagnostic_parse_succeeded=True)
            summary=common.bound_diagnostic_records([record])
            self.assertEqual(summary['detail_items_omitted'],40)

    def test_failure_diagnostic_export_rejects_unsealed_types_arrays_enums_and_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'test_fixture.py'
            names=[f'test_case_{index:02d}' for index in range(24)]
            path.write_text(''.join(f'def {name}(self): pass\n' for name in names))
            common.write_json(root/'SOURCE_SHA256.json',{'test_fixture.py':'a'*64})
            details=common.parse_diagnostic_tails(['\n'.join(
                [f'FAIL: {name} (fixture.Case.{name})' for name in names]
                +['ValueError: private-value','AssertionError: private-value',
                  '  File "test_fixture.py", line 1','  File "test_fixture.py", line 2'])],{'test_fixture.py':'a'*64},root)
            record=dict(details,gate='mock-gate',outcome='target-exited-nonzero',exit_code=1,diagnostic_parse_succeeded=True)
            diagnostic=root/'failures'/('a'*32+'.json');common.write_json(diagnostic,record)
            mutations=[('raw_stderr','private-value'),('gate',[]),('outcome',{}),('exit_code',True),
                ('diagnostic_parse_succeeded',1),('diagnostic_parse_succeeded',False),
                ('failed_test_ids',['test_private_secret']),('failed_test_ids',names[:21]),
                ('failed_test_ids',names[:2][::-1]),('failed_test_ids',[names[0],names[0]]),
                ('failed_test_ids',[None]),('observed_exception_categories',['PrivateSecretError']),
                ('observed_exception_categories',['ValueError','AssertionError']),
                ('observed_exception_categories',['AssertionError','AssertionError']),
                ('source_locations',[{'file':'/home/private/profile.py','line':1}]),
                ('source_locations',[{'file':'test_fixture.py','line':True}]),
                ('source_locations',[{'file':'test_fixture.py','line':1,'raw':'private-value'}]),
                ('source_locations',details['source_locations'][::-1]),
                ('source_locations',[details['source_locations'][0]]*2),
                ('source_locations',[None])]
            for field in common.DIAGNOSTIC_LIST_FIELDS:
                mutations.extend((field,value) for value in ('private-value',{},None))
                mutations.extend((field+'_omitted',value) for value in (True,-1,1.5,'1',65537))
            mutations += [('failed_test_ids_omitted',5),('source_locations_omitted',1),
                ('observed_exception_categories_omitted',1)]
            with patch.object(common,'ROOT',root),patch.object(common,'state_root',return_value=root):
                self.assertEqual(common.failure_diagnostics()['records'],[record])
                for field,value in mutations:
                    with self.subTest(field=field,value=value):
                        bad=dict(record);bad[field]=value;diagnostic.write_text(json.dumps(bad))
                        with self.assertRaises(RuntimeError):common.failure_diagnostics()
                missing=dict(record);missing.pop('failed_test_ids');diagnostic.write_text(json.dumps(missing))
                with self.assertRaises(RuntimeError):common.failure_diagnostics()
                diagnostic.write_text('[]')
                with self.assertRaises(RuntimeError):common.failure_diagnostics()

    def test_diagnostic_parse_failure_saves_only_empty_typed_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);common.write_json(root/'SOURCE_SHA256.json',{})
            with patch.object(common,'ROOT',root),patch.object(common,'state_root',return_value=root),\
                 patch.object(common,'bounded_log_tail',side_effect=RuntimeError('private-parser-error')):
                common.save_failure_diagnostic('mock-failure',paths=(root/'private.log',))
                summary=common.failure_diagnostics()
            self.assertEqual(len(summary['records']),1)
            record=summary['records'][0];self.assertFalse(record['diagnostic_parse_succeeded'])
            for field in common.DIAGNOSTIC_LIST_FIELDS:
                self.assertEqual(record[field],[]);self.assertEqual(record[field+'_omitted'],0)
            self.assertNotIn('private',json.dumps(summary))

    def test_wait_diagnostic_categories_reject_private_module_and_name_lookalikes(self):
        lines=[]
        for name in common.DIAGNOSTIC_WAIT_EXCEPTION_CATEGORIES:
            lines.extend(('private_module.'+name+': private-value',
                'private_module.scripts.tests.test_owned_process_windows_r64.'+name+': private-value',
                'test_owned_process_windows_r64_private.'+name+': private-value',
                'test_owned_process_windows_r64.'+name+'Private: private-value',
                name+'Private: private-value', 'Private'+name+': private-value',
                'INFO '+name+': private-value',name+':private-value'))
        parsed=common.parse_diagnostic_tails(['\n'.join(lines)],{},HERE.parent)
        self.assertEqual(parsed['observed_exception_categories'],[])
        for module in ('','test_owned_process_windows_r64.','scripts.tests.test_owned_process_windows_r64.'):
            parsed=common.parse_diagnostic_tails(['\n'.join(
                module+name+': private-handle=987654321 private-error=123456789 private-pid=876543210 C:\\private-path'
                for name in common.DIAGNOSTIC_WAIT_EXCEPTION_CATEGORIES)],{},HERE.parent)
            self.assertEqual(parsed['observed_exception_categories'],sorted(common.DIAGNOSTIC_WAIT_EXCEPTION_CATEGORIES))
            encoded=json.dumps(parsed)
            for private in ('private','987654321','123456789','876543210'):
                self.assertNotIn(private,encoded)


class ParentFailureDiagnostics(unittest.TestCase):
    def capture(self, source, filename, scope=None):
        try:
            exec(compile(source, str(filename), 'exec'), scope or {})
        except BaseException:
            return sys.exc_info()
        self.fail('Fixture did not raise')

    def test_actual_parent_source_and_installed_rejection_keep_failure_and_sealed_lines(self):
        manifest={'ci/public_ci.py':'a'*64,'ci/public_ci_common.py':'b'*64}
        require_node=next(node for node in ast.parse((HERE/'public_ci_common.py').read_text()).body
            if isinstance(node,ast.FunctionDef) and node.name=='require')
        raise_line=next(node.lineno for node in ast.walk(require_node) if isinstance(node,ast.Raise))
        for stage in ('build','installed'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as directory:
                state=Path(directory)
                with patch.object(ci,'verify_run_state',return_value={'nonce':'a'*32}),\
                     patch.object(ci,'state_root',return_value=state),\
                     patch.object(common,'state_root',return_value=state),\
                     patch.object(common,'read_json',return_value=manifest),\
                     patch.object(common,'source_identity',return_value={'fresh':True}),\
                     patch.object(ci,'read_json',return_value={'installed_root':str(state/'private-installation')}),\
                     patch.dict(os.environ,{'LOCALAPPDATA':str(state/'private-profile')}):
                    action=(lambda:common.validate_source_build({'source_provenance':{}})) if stage=='build' else ci.installed_hashes
                    with self.assertRaises(RuntimeError):
                        ci.run_stage(stage,action)
                result=json.loads((state/(stage+'-result.json')).read_text())
                self.assertEqual(result['status'],'failed');self.assertEqual(result['hashes'],{})
                files=list((state/'failures').iterdir());self.assertEqual(len(files),1)
                record=json.loads(files[0].read_text())
                self.assertTrue(record['diagnostic_parse_succeeded'])
                self.assertEqual(record['gate'],stage+'-validation')
                self.assertEqual(record['observed_exception_categories'],['RuntimeError'])
                self.assertIn({'file':'ci/public_ci_common.py','line':raise_line},record['source_locations'])
                self.assertTrue(all(row['file'] in manifest for row in record['source_locations']))
                encoded=json.dumps(record)
                for private in (str(state),str(HERE.parent),'private','message','args','function'):
                    self.assertNotIn(private,encoded)

    def test_exception_text_nested_tokens_and_outside_frames_are_never_parsed(self):
        root=Path('/synthetic');filename=root/'ci/validator.py'
        text='test_private_name\nValueError: nested-token\n  File "ci/other.py", line 987654'
        info=self.capture('def private_function():\n    private_local="private-value"\n    raise RuntimeError(payload) from ValueError("private-cause")\nprivate_function()',
            filename,{'payload':text})
        value=common.parse_diagnostic_tails([],{'ci/validator.py':'a'*64,'ci/other.py':'b'*64},root,info)
        self.assertEqual(value['observed_exception_categories'],['RuntimeError'])
        self.assertEqual(value['test_ids'],[])
        self.assertEqual(value['source_locations'],[{'file':'ci/validator.py','line':3},{'file':'ci/validator.py','line':4}])
        for private in ('private','nested-token','987654','ValueError',str(root),'test_public_ci.py'):
            self.assertNotIn(private,json.dumps(value))
        for filename in ('/private/validator.py','/synthetic/../private/validator.py','ci/validator.py'):
            info=self.capture('raise ValueError("private-path")',filename)
            value=common.parse_diagnostic_tails([],{'ci/validator.py':'a'*64},root,info)
            self.assertEqual(value['source_locations'],[])
            self.assertEqual(value['observed_exception_categories'],['ValueError'])
        info=self.capture('raise ExceptionGroup("private-group", [ValueError("private-child")])',
            root/'ci/validator.py')
        value=common.parse_diagnostic_tails([],{'ci/validator.py':'a'*64},root,info)
        self.assertEqual(value['observed_exception_categories'],[])
        self.assertEqual(value['source_locations'],[{'file':'ci/validator.py','line':1}])
        self.assertNotIn('private',json.dumps(value))

    def test_custom_type_names_messages_repr_and_attributes_are_not_observed(self):
        def forbidden(*args):
            raise AssertionError('Private exception introspection attempted')
        class PrivateMeta(type):
            __getattribute__=forbidden
            __hash__=forbidden
            __eq__=forbidden
        custom=PrivateMeta('RuntimeError',(RuntimeError,),{
            '__str__':forbidden,'__repr__':forbidden,'__getattribute__':forbidden})
        info=self.capture('raise problem', '/synthetic/ci/validator.py',{'problem':custom('private-args')})
        value=common.parse_diagnostic_tails([],{'ci/validator.py':'a'*64},Path('/synthetic'),info)
        self.assertEqual(value['observed_exception_categories'],[])
        self.assertEqual(value['source_locations'],[{'file':'ci/validator.py','line':1}])
        self.assertNotIn('private',json.dumps(value))

    def test_child_and_parent_items_union_before_caps_with_exact_unique_omissions(self):
        root=Path('/synthetic');manifest={'ci/validator.py':'a'*64}
        kind,problem,trace=self.capture('raise RuntimeError("private")',root/'ci/validator.py')
        while trace.tb_next is not None:trace=trace.tb_next
        chain=None
        for line in list(range(20,46))*2:
            chain=TracebackType(chain,trace.tb_frame,trace.tb_lasti,line)
        tails=['\n'.join([f'  File "ci/validator.py", line {line}' for line in range(1,31)]
            +['ValueError: private-child','RuntimeError: duplicate'])]*2
        value=common.parse_diagnostic_tails(tails,manifest,root,(kind,problem,chain))
        self.assertEqual(value['source_locations'],[{'file':'ci/validator.py','line':line} for line in range(1,21)])
        self.assertEqual(value['source_locations_omitted'],25)
        self.assertEqual(value['observed_exception_categories'],['RuntimeError','ValueError'])
        self.assertEqual(value['observed_exception_categories_omitted'],0)
        record=dict(value,gate='build-validation',outcome='validation-failed',exit_code=None,diagnostic_parse_succeeded=True)
        summary=common.bound_diagnostic_records([record])
        self.assertEqual(summary['detail_items_omitted'],25)

    def test_no_active_exception_does_not_invent_cleanup_evidence(self):
        info=self.capture('raise RuntimeError("private")','/synthetic/ci/validator.py')
        root=Path('/synthetic');manifest={'ci/validator.py':'a'*64}
        try:
            raise info[1]
        except RuntimeError:
            for context in (None,(None,None,None)):
                value=common.parse_diagnostic_tails([],manifest,root,context)
                self.assertTrue(all(value[field]==[] and value[field+'_omitted']==0 for field in common.DIAGNOSTIC_LIST_FIELDS))

    def test_oversized_capture_fails_closed_and_preserves_separate_child_record(self):
        kind,problem,trace=self.capture('raise RuntimeError("private")','/synthetic/ci/validator.py')
        while trace.tb_next is not None:trace=trace.tb_next
        chain=None
        for unused in range(65537):chain=TracebackType(chain,trace.tb_frame,trace.tb_lasti,1)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);common.write_json(root/'SOURCE_SHA256.json',{})
            with patch.object(common,'ROOT',root),patch.object(common,'state_root',return_value=root),\
                 patch.object(common,'bounded_log_tail',return_value='AssertionError: private-child'):
                common.save_failure_diagnostic('child-gate',{'outcome':'target-exited-nonzero','targetExitCode':1},(root/'child.log',))
                common.save_failure_diagnostic('build-validation',exception_info=(kind,problem,chain))
                summary=common.failure_diagnostics()
            records={record['gate']:record for record in summary['records']}
            self.assertEqual(records['child-gate']['observed_exception_categories'],['AssertionError'])
            self.assertFalse(records['build-validation']['diagnostic_parse_succeeded'])
            for field in common.DIAGNOSTIC_LIST_FIELDS:
                self.assertEqual(records['build-validation'][field],[])
                self.assertEqual(records['build-validation'][field+'_omitted'],0)
            self.assertNotIn('private',json.dumps(summary))

    def test_combined_location_ceiling_leaves_room_for_existing_byte_pruning(self):
        root=Path('/synthetic');manifest={'ci/validator.py':'a'*64}
        kind,problem,trace=self.capture('raise RuntimeError("private")',root/'ci/validator.py')
        while trace.tb_next is not None:trace=trace.tb_next
        chain=None
        for line in range(1,65537):chain=TracebackType(chain,trace.tb_frame,trace.tb_lasti,line)
        value=common.parse_diagnostic_tails([],manifest,root,(kind,problem,chain))
        self.assertEqual(value['source_locations_omitted'],65516)
        with self.assertRaises(RuntimeError):
            common.parse_diagnostic_tails(['  File "ci/validator.py", line 65537'],manifest,root,(kind,problem,chain))

    def test_capture_or_write_failure_cannot_change_parent_failure_or_main_exit(self):
        for target in ('parse_diagnostic_tails','write_json'):
            with self.subTest(target=target),tempfile.TemporaryDirectory() as directory:
                state=Path(directory);failure=RuntimeError('private-original-failure')
                def reject():raise failure
                with patch.object(ci,'verify_run_state',return_value={'nonce':'a'*32}),\
                     patch.object(ci,'state_root',return_value=state),\
                     patch.object(common,'state_root',return_value=state),\
                     patch.object(common,'read_json',return_value={}),\
                     patch.object(common,target,side_effect=RuntimeError('private-diagnostic-failure')):
                    with self.assertRaises(RuntimeError) as caught:ci.run_stage('build',reject)
                self.assertIs(caught.exception,failure)
                self.assertEqual(json.loads((state/'build-result.json').read_text())['status'],'failed')
        output=io.StringIO()
        with patch.object(ci.sys,'argv',['public_ci.py','build']),\
             patch.object(ci,'run_stage',side_effect=RuntimeError('private-main-failure')),\
             patch('sys.stdout',output):
            self.assertEqual(ci.main(),1)
        self.assertEqual(output.getvalue(),'PUBLIC_CI_STAGE=build FAILED\n')

    def test_successful_stage_does_not_capture_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory,\
             patch.object(ci,'verify_run_state',return_value={'nonce':'a'*32}),\
             patch.object(ci,'state_root',return_value=Path(directory)),\
             patch.object(ci,'save_failure_diagnostic') as save:
            ci.run_stage('build',lambda:{'proof':'synthetic'})
            save.assert_not_called()
            result=json.loads((Path(directory)/'build-result.json').read_text())
            self.assertEqual(result['status'],'passed');self.assertEqual(result['hashes'],{'proof':'synthetic'})


if __name__=='__main__':
    unittest.main()
