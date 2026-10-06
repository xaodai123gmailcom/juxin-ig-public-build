"""Static and mocked public orchestration tests; no product or model is executed."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
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

    def test_installed_checks_preserve_scale_upgrade_and_actual_api(self):
        text=(HERE/'public_ci_verify_installed.ps1').read_text()
        for token in ('--pure-ig','--snapshot-scale','--collection-completion','--standalone-nurture',
                      '--posting-workflow','--nurture-cleanup-upgrade','441552','602831',
                      'performance_indexes_verified.Count -ne 3','compact_wire.canonical_rows_equal',
                      'normal_restart.retained_data','pure_ig_upgrade.ig_identity_hash_preserved',
                      'completed_card_dismissal','repeated_startup_idempotent','five_card_totals',
                      'scripts\\verify_installed_recovery_r64.py',"'120'",'-TimeoutSeconds 180',
                      "'^INSTALLED_RECOVERY_R64=PASS '"):
            self.assertIn(token,text)
        self.assertLess(text.index('--nurture-cleanup-upgrade'),text.index('scripts\\verify_installed_recovery_r64.py'))
        stage=(HERE/'public_ci_validate_installed.py').read_text()
        for token in ('r63_upgrade_proof.py','r63_native_proof.py','r64_crop_proof.py',
                      'r64_recovery_ui_proof.py','verify_installed_recovery_r64.py',
                      "same_json(installed.get('recovery_api'),recovery_api)",'core_upgrade_manifest_sha256'):
            self.assertLess(stage.index(token),stage.index("print('PUBLIC_INSTALLED_ACCEPTANCE=PASS')"))

    def test_exact_six_early_fixtures_and_all_thirty_six_groups(self):
        text=(HERE/'public_ci_early.py').read_text();tree=ast.parse(text)
        loop=next(n for n in tree.body if isinstance(n,ast.For) and isinstance(n.target,ast.Name) and n.target.id=='fixture')
        fixtures=['desktop/tests/nurture-cleanup-native-r63.cjs','desktop/tests/nurture-reels-r6.integration.cjs',
            'desktop/tests/posting-viewport-native-r62.cjs','desktop/tests/posting-r6.integration.cjs',
            'desktop/tests/crop-icon-r64.cjs','desktop/tests/recovery-ui-native-r64.cjs']
        self.assertEqual(list(ast.literal_eval(loop.iter)),fixtures)
        groups=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='patterns' for t in n.targets))
        import public_ci_groups
        self.assertEqual(groups,public_ci_groups.PATTERNS);self.assertEqual(len(groups),36)
        for name in ('IGAC_REQUIRE_POSTING_BROWSER','IGAC_REQUIRE_PROFILE_BROWSER',
                     'IGAC_REQUIRE_STANDALONE_NURTURE_BROWSER','IGAC_REQUIRE_FINAL_SEED_BROWSER',
                     'JUXIN_REQUIRE_NURTURE_CLEANUP_NATIVE','JUXIN_REQUIRE_RECOVERY_UI_NATIVE'):
            self.assertIn(name+"='1'",text)
        self.assertIn("case_timeout='90' if pattern=='test_final_seed_browser_r62.py' else '180'",text)
        self.assertLess(text.index('if failures:'),text.index('native=json.loads'))

    def test_crop_precheck_runs_first_and_failure_cannot_reach_full_suite(self):
        tree=ast.parse((HERE/'public_ci_early.py').read_text())
        call=next(n for n in tree.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
            and isinstance(n.value.func,ast.Name) and n.value.func.id=='run_owned'
            and n.value.args and isinstance(n.value.args[0],ast.Constant)
            and n.value.args[0].value=='early-crop-focused-precheck')
        native=next(n for n in tree.body if isinstance(n,ast.For)
            and isinstance(n.target,ast.Name) and n.target.id=='fixture')
        self.assertLess(tree.body.index(call),tree.body.index(native))
        calls=[];env={'IGAC_REQUIRE_POSTING_BROWSER':'1'}
        module=ast.Module(body=[call],type_ignores=[])
        scope={'python':'verified-python','env':env,'run_owned':lambda *args:calls.append(args)}
        exec(compile(module,'<mock crop precheck>','exec'),scope)
        self.assertEqual(calls,[('early-crop-focused-precheck', ['verified-python','-X','utf8',
            'scripts/run_backend_tests.py','-p','test_crop_icon_r64.py','--case-timeout','180','-v'],1800,env)])
        scope['run_owned']=lambda *args: (_ for _ in ()).throw(RuntimeError('mock crop failure'))
        with self.assertRaisesRegex(RuntimeError,'mock crop failure'):
            exec(compile(module,'<mock crop precheck>','exec'),scope)

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
                'source_locations':[{'file':'backend/tests/test_fixture.py','line':2}]})
            encoded=json.dumps(value)
            for secret in ('very-private','profile.json',str(root),'test_unrecognized'):
                self.assertNotIn(secret,encoded)

    def test_worst_case_failure_diagnostics_fit_json_byte_budget(self):
        record={'gate':'x'*96,'outcome':'target-exited-nonzero','exit_code':-2147483648,
            'diagnostic_parse_succeeded':True,'test_ids':['test_'+('a'*155)]*20,
            'source_locations':[{'file':('a'*3000)+'.py','line':9999999}]*20}
        summary=common.bound_diagnostic_records([copy.deepcopy(record) for _ in range(32)])
        self.assertLessEqual(len(json.dumps(summary,ensure_ascii=True,sort_keys=True).encode()),24576)
        self.assertGreater(summary['records_omitted'],0)
        self.assertGreater(summary['detail_items_omitted'],0)
        self.assertTrue(summary['records'])
        self.assertEqual(summary['records'][0]['exit_code'],-2147483648)

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
        self.assertEqual(40,len(patterns));self.assertEqual('test_follow_monitor.py',patterns[0])
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
        for name in ('renderer/tests/posting-withdraw-r63.test.mjs','renderer/tests/hidden-collection-blocker-r63.test.mjs'):
            self.assertIn(name,package['scripts']['test:renderer'])
        self.assertIn('desktop/tests/recovery-ui-native-r64.test.cjs',package['scripts']['test:desktop'])
        script=(HERE.parent/'scripts/build_windows.ps1').read_text(encoding='utf-8-sig')
        self.assertLess(script.index('@("run", "build:electron")'),script.index('"desktop\\tests\\crop-icon-r64.cjs"'))
        self.assertIn('JUXIN_REQUIRE_RECOVERY_UI_NATIVE = "1"',script)
        self.assertIn('if ($R64NativeExitCode -ne 0) { throw',script)
        source=(HERE.parent/'scripts/verify_build_source.mjs').read_text()
        for path in ('backend/app/browser_admission.py','backend/app/nurture_collection_blocker.py',
                     'backend/app/instagram_crop_dom.py','backend/tests/test_combined_recovery_r64.py',
                     'scripts/verify_installed_recovery_r64.py','desktop/tests/recovery-ui-native-r64.cjs',
                     'renderer/tests/fixtures/recovery-ui-r64.tsx'):
            self.assertIn(path,source)


if __name__=='__main__':
    unittest.main()
