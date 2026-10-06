"""Mandatory offline Windows browser/native preflight for the exact R6.4 source."""
import hashlib,json,os,subprocess,runpy,sys,shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_ci_common import ROOT, EARLY_FILES, run_owned, source_identity, bind_native, state_root, verify_run_state, require, preflight_early_evidence, digest
from pathlib import Path
root=ROOT
verify_run_state()
identity=source_identity()
os.chdir(root)
python=str((root/'.venv/Scripts/python.exe').resolve())
output=root/'installer-output';output.mkdir(exist_ok=True)
manifest=root/'SOURCE_SHA256.json'
hashes=json.loads(manifest.read_text(encoding='utf-8'))
for name,expected in hashes.items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==expected,name
candidate=output/'r64-early-chrome.json'
run_owned('early-browser-selection',[python,'-X','utf8','scripts/browser_build_policy.py','--candidate-output',str(candidate)],180)
browser=json.loads(candidate.read_text(encoding='utf-8'));chrome=browser['executable'];assert Path(chrome).is_file()
env=os.environ.copy();env.update(PYTHON=python,IGAC_CROP_FIXTURE_REPORT=str(output/'r64-crop-icon-native.json'),IGAC_CROP_FIXTURE_SCREENSHOT=str(output/'r64-crop-icon-native.png'),JUXIN_REQUIRE_RECOVERY_UI_NATIVE='1',IGAC_TEST_CHROMIUM_EXECUTABLE=chrome,IGAC_POSTING_TEST_BROWSER=chrome,IGAC_REQUIRE_POSTING_BROWSER='1',IGAC_REQUIRE_PROFILE_BROWSER='1',IGAC_REQUIRE_STANDALONE_NURTURE_BROWSER='1',IGAC_REQUIRE_FINAL_SEED_BROWSER='1',JUXIN_REQUIRE_NURTURE_CLEANUP_NATIVE='1',IGAC_FINAL_SEED_FIXTURE_ARTIFACT_DIR=str(output/'final-seed-fixtures'))
# Exercise the previously failing offline fixtures before the long suite.
# Both groups remain mandatory in their original release gates as well.
run_owned('early-saturation-focused-precheck', [python, '-X', 'utf8', 'scripts/run_backend_tests.py',
    '-p', 'test_saturation_acceptance_r99.py', '-v'], 1800,
    dict(env, IGAC_REQUIRE_SATURATION_BROWSER='1'))
run_owned('early-crop-focused-precheck', [python, '-X', 'utf8', 'scripts/run_backend_tests.py',
    '-p', 'test_crop_icon_r64.py', '--case-timeout', '180', '-v'], 1800, env)
failures=[]
def run_required(label,command,timeout=None):
    try:
        run_owned('early-required-' + str(len(native_fixtures) + len(failures)) + '-' + hashlib.sha256(label.encode()).hexdigest()[:12], command, timeout or 1800, env)
        return True
    except RuntimeError:
        failures.append({'check':label,'failed':True})
        print('R64_EARLY_REQUIRED_FAILURE=required-check',flush=True)
        return False
electron_cli=str((root/'node_modules/electron/cli.js').resolve());assert Path(electron_cli).is_file()
for stale in ('r63-nurture-cleanup-native.json','r63-nurture-cleanup-native.failure.json','r63-nurture-cleanup-native.png'):
    (output/stale).unlink(missing_ok=True)
native_fixtures=[]
for fixture in ('desktop/tests/nurture-cleanup-native-r63.cjs','desktop/tests/nurture-reels-r6.integration.cjs','desktop/tests/posting-viewport-native-r62.cjs','desktop/tests/posting-r6.integration.cjs','desktop/tests/crop-icon-r64.cjs','desktop/tests/recovery-ui-native-r64.cjs'):
    print('R64_EARLY_NATIVE_FIXTURE='+fixture,flush=True)
    if run_required(fixture,['node',electron_cli,str(root/fixture)],timeout=180):
        native_fixtures.append(fixture)
patterns=[
    'test_installed_recovery_r64.py',
    'test_combined_recovery_r64.py','test_hidden_collection_blocker_r63.py',
    'test_posting_withdraw*_r63.py','test_posting_durable_preflight_r63.py',
    'test_crop_icon_r64.py','test_crop_guard_r64.py',
    'test_installed_nurture_cleanup_upgrade_r63.py',
    'test_nurture_cleanup*.py','test_nurture_closed_profile_guard.py','test_nurture_missing_lease_surface.py',
    'test_ig_only_runtime.py','test_window_reuse_r93.py','test_*r31.py',
    'test_final_seed_browser_r62.py','test_final_seed_completion_r62.py','test_screening_factory_recovery_r62.py',
    'test_standalone_nurture_browser_r6.py','test_standalone_reels_routes_r62.py',
    'test_standalone_watch_advance_r62.py','test_nurture_completion_cleanup_r62.py',
    'test_installed_standalone_nurture_r6.py',
    'test_posting_crop_transition_r62.py','test_original_crop_readiness_r62.py',
    'test_posting_editing_state_r62.py','test_posting_viewport_labels.py',
    'test_posting_viewport_lifecycle_r62.py','test_posting_dom.py',
    'test_posting_transition_r80.py','test_instagram_identity*r62.py',
    'test_posting_startup_r62.py','test_posting_retry_r61.py',
    'test_live_parent_recheck_r6.py','test_installed_collection_completion_r97.py',
    'test_collector_parent_handoff_r6.py','test_single_gap_recheck_r6.py',
]
completed=[]
for pattern in patterns:
    print('R64_EARLY_REQUIRED_GROUP='+pattern,flush=True)
    case_timeout='90' if pattern=='test_final_seed_browser_r62.py' else '180'
    if run_required(pattern,[python,'-X','utf8','scripts/run_backend_tests.py','-p',pattern,'--case-timeout',case_timeout,'-v']):
        completed.append(pattern)
if failures:
    failure_proof={'source_provenance':identity,'run':verify_run_state()['run'],'verified':False,'source_commit':os.environ['GITHUB_SHA'],'source_manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest(),'completed_groups':completed,'failures':failures}
    (output/'r64-early-failures.json').write_text(json.dumps(failure_proof,indent=2)+'\n',encoding='utf-8')
    raise RuntimeError('Required R6.4 preflight checks failed: '+', '.join(item['check'] for item in failures))
preflight_early_evidence()
native=json.loads((output/'r62-posting-viewport-native.json').read_text(encoding='utf-8'))
assert native.get('verified') is True and native.get('synthetic_offline') is True
final_seed=json.loads((output/'final-seed-fixtures/final-seed-browser-r62.json').read_text(encoding='utf-8'))
assert final_seed.get('verified') is True and final_seed.get('synthetic_offline') is True
assert final_seed.get('source_preserved') is True and final_seed.get('all_owned_children_closed') is True
assert final_seed.get('retired_children')==3 and final_seed.get('replacement_children')==3 and final_seed.get('external_requests')==0
cleanup_native=json.loads((output/'r63-nurture-cleanup-native.json').read_text(encoding='utf-8'))
runpy.run_path(str(root/'ci/r63_native_proof.py'))['validate_native'](cleanup_native,root,os.environ['GITHUB_SHA'])
crop_native=json.loads((output/'r64-crop-icon-native.json').read_text(encoding='utf-8'))
runpy.run_path(str(root/'ci/r64_crop_proof.py'))['validate_crop'](crop_native,root,os.environ['GITHUB_SHA'],output)
recovery_ui=json.loads((output/'r64-recovery-ui-native-proof.json').read_text(encoding='utf-8'))
runpy.run_path(str(root/'ci/r64_recovery_ui_proof.py'))['validate_recovery_ui'](recovery_ui,root,os.environ['GITHUB_SHA'],output)
for bound in (native,cleanup_native,crop_native,recovery_ui):
    bind_native(bound,identity)
proof={'source_provenance':identity,'run':verify_run_state()['run'],'recovery_ui_native':recovery_ui,'crop_native':crop_native,'native_fixtures':native_fixtures,'nurture_cleanup_native':cleanup_native,'verified':True,'synthetic_offline':True,'browser_version':browser['version'],'source_commit':os.environ['GITHUB_SHA'],'source_manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest(),'groups':completed,'native_posting_viewport':native,'final_seed_browser':final_seed,'full_release_gates_still_required':True}
(output/'r64-early-verification.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('R64_EARLY_VERIFICATION=PASS',flush=True)


# Retain only the fixed proof inputs for a subsequent clean checkout in this run.
# These raw receipts and screenshots are never public artifacts.
early_root=state_root()/'early'
early_root.mkdir(exist_ok=False)
preflight_early_evidence(include_aggregate=True)
for name in EARLY_FILES:
    target=early_root/name
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(output/name,target)
    require(digest(output/name)==digest(target), 'Early evidence changed while retaining it')
verify_run_state()
