"""Stage only a current, verified NSIS installer; a portable ZIP is not an EXE."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import runpy
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from public_ci_common import ROOT, EARLY_FILES, state_root, source_identity, bind_native, verify_run_state, validate_source_build, preflight_installed_evidence, require, same_json, read_json, digest as proof_digest

root = ROOT.parent
state=verify_run_state()
preflight_installed_evidence()
identity=source_identity()
validate_source_build(state)
output = root / 'source/installer-output'
name = 'Juxin-IG-Audience-Collector-NewGen-Setup-3.0.4-x64.exe'
installer = output / name
source_proof = json.loads((output / 'build-source.json').read_text(encoding='utf-8-sig'))
browser_proof = json.loads((output / 'embedded-browser-check.json').read_text(encoding='utf-8-sig'))
marker = dict(line.split('=', 1) for line in (output / 'LATEST_SUCCESS.txt').read_text(encoding='utf-8-sig').splitlines() if '=' in line)
policy = json.loads((root / 'source/build/browsers/juxin-runtime-requirement.json').read_text())
if not source_proof.get('verified') or source_proof.get('revision') != 'stability-r94':
    raise RuntimeError('Source verification is incomplete')
if not browser_proof.get('verified') or not browser_proof.get('pythonWorker') or browser_proof.get('taskColdStart') is not True:
    raise RuntimeError('Embedded browser and Python verification is incomplete')
digest = hashlib.file_digest(installer.open('rb'), 'sha256').hexdigest()
if (marker.get('TYPE') != 'INSTALLER' or marker.get('VERSION') != '3.0.4'
        or Path(marker.get('PATH', '')).resolve() != installer.resolve()
        or marker.get('SHA256') != digest or marker.get('BROWSER_MODE') != 'installed-chrome'
        or policy.get('mode') != 'installed-chrome-required'):
    raise RuntimeError('Installer identity, checksum or browser policy mismatch')
if not (output / 'installed-verification.json').is_file():
    raise RuntimeError('Final installer has not been installed and tested')
installed = json.loads((output / 'installed-verification.json').read_text(encoding='utf-8-sig'))
if (installed.get('verified') is not True
        or installed.get('nurture_cleanup_upgrade', {}).get('verified') is not True
        or installed.get('snapshot_scale', {}).get('pure_ig_upgrade', {}).get('verified') is not True
        or installed.get('snapshot_scale', {}).get('work_report_summary', {}).get('verified') is not True
        or installed.get('snapshot_scale', {}).get('compact_wire', {}).get('verified') is not True
        or installed.get('snapshot_scale', {}).get('normal_restart', {}).get('verified') is not True
        or len(installed.get('snapshot_scale', {}).get('performance_indexes_verified', [])) != 3
        or installed.get('posting_workflow', {}).get('verified') is not True
        or installed.get('standalone_nurture', {}).get('verified') is not True
        or installed.get('standalone_nurture', {}).get('cases', {}).get('verified_playback', {}).get('verified') is not True
        or installed.get('standalone_nurture', {}).get('cases', {}).get('completed_cleanup_fence', {}).get('verified') is not True
        or installed.get('standalone_nurture', {}).get('cases', {}).get('completed_history', {}).get('canonical_singular_and_plural_routes') is not True
        or installed.get('standalone_nurture', {}).get('cases', {}).get('legacy_plan_fence', {}).get('legacy_wall_time_not_reinterpreted') is not True
        or installed.get('collection_completion', {}).get('single_gap_recheck', {}).get('verified') is not True
        or installed.get('collection_completion', {}).get('manual_parent_recheck', {}).get('verified') is not True
        or installed.get('collection_completion', {}).get('final_seed_completion', {}).get('verified') is not True
        or installed.get('collection_completion', {}).get('final_seed_completion', {}).get('authoritative_factory_reconnect') is not True
        or installed.get('collection_completion', {}).get('verified') is not True or installed.get('installer_sha256') != digest
        or installed.get('snapshot_scale', {}).get('completed_card_dismissal', {}).get('verified') is not True
        or installed.get('snapshot_scale', {}).get('completed_card_dismissal', {}).get('persistence_after_restart') is not True
        or installed.get('snapshot_scale', {}).get('completed_card_dismissal', {}).get('retained_data') is not True
        or installed.get('snapshot_scale', {}).get('completed_card_dismissal', {}).get('platform_isolation') is not True
        or installed.get('pure_instagram_verified') is not True
        or installed.get('removed_platform_inputs_rejected') is not True
        or installed.get('snapshot_scale_verified') is not True
        or installed.get('snapshot_scale', {}).get('legacy_platform_counter_upgrade', {}).get('verified') is not True
        or installed.get('snapshot_scale', {}).get('legacy_platform_counter_upgrade', {}).get('legacy_review_columns_restored') is not True
        or set(installed.get('snapshot_scale', {}).get('platform_snapshot_seconds', {})) != {'instagram'}):
    raise RuntimeError('Installed verification does not match the exact installer')
upgrade_core = Path(installed['installed_root'])/'resources/backend/collector_core/collector_core.exe'
runpy.run_path(str(ROOT/'ci/r63_upgrade_proof.py'))['validate_upgrade'](installed['nurture_cleanup_upgrade'],upgrade_core)
scale_report = json.loads((output/'installed-scale-verification.json').read_text(encoding='utf-8-sig'))
if not same_json(scale_report.get('nurture_cleanup_upgrade'),installed['nurture_cleanup_upgrade']):
    raise RuntimeError('Installed upgrade receipt differs between final reports')
upgrade = installed.get('report_index_upgrade', {})
if (any(upgrade.get(key) is not True for key in ('verified', 'legacy_index_preserved', 'target_period_index_created', 'inventory_dedup_hashes_preserved', 'window_leases_preserved', 'repeated_startup_idempotent', 'synthetic'))
        or upgrade.get('user_data_touched') is not False
        or upgrade.get('restart_count') != 2
        or upgrade.get('five_card_totals') != {'collection': 3, 'follow': 1, 'split': 3, 'added': 7, 'confirmed_posting': 1}
        or not upgrade.get('retained_rows')):
    raise RuntimeError('Installed conflicting-index upgrade and data preservation proof is invalid')
reels_path = output / 'parent-reels-fixtures/parent-reels-proof-r98.json'
reels = json.loads(reels_path.read_text(encoding='utf-8'))
if (reels.get('synthetic_offline') is not True or not reels.get('browser_version')
        or reels.get('stats', {}).get('likes') != 1
        or any(reels.get('stats', {}).get(key) != 0 for key in ('unlikes', 'comments', 'follows'))
        or reels.get('stats', {}).get('trusted') != [True]):
    raise RuntimeError('Required native parent Reels synthetic browser proof is missing or invalid')
early_root = state_root() / 'early'
early = json.loads((early_root / 'r64-early-verification.json').read_text(encoding='utf-8'))
early_hashes=read_json(state_root()/'early-result.json')['hashes']
require(set(early_hashes)==set(EARLY_FILES) and all(proof_digest(early_root/name)==early_hashes[name] for name in EARLY_FILES), 'Retained early raw evidence changed after that gate')
for field,name in (('native_posting_viewport','r62-posting-viewport-native.json'),('nurture_cleanup_native','r63-nurture-cleanup-native.json'),('crop_native','r64-crop-icon-native.json'),('recovery_ui_native','r64-recovery-ui-native-proof.json'),('final_seed_browser','final-seed-fixtures/final-seed-browser-r62.json')):
    require(same_json(early.get(field),read_json(early_root/name)), 'Retained early standalone and aggregate receipts differ')
for name in ('r62-posting-viewport-native.png','final-seed-fixtures/final-seed-browser-r62.png'):
    require((early_root/name).read_bytes().startswith(b'\x89PNG\r\n\x1a\n'), 'Retained early native capture is invalid')
early_seed=early['final_seed_browser']
require(all(early_seed.get(key) is True for key in ('verified','synthetic_offline','source_preserved','all_owned_children_closed')) and early_seed.get('live_accounts_tested') is False and early_seed.get('external_requests')==0 and early_seed.get('retired_children')==3 and early_seed.get('replacement_children')==3 and bool(early_seed.get('browser_version')), 'Retained early final-source browser proof is incomplete')
if (early.get('verified') is not True or early.get('synthetic_offline') is not True
        or early.get('source_commit') != os.environ['GITHUB_SHA']
        or early.get('source_manifest_sha256') != hashlib.sha256((root / 'source/SOURCE_SHA256.json').read_bytes()).hexdigest()
        or early.get('native_posting_viewport', {}).get('verified') is not True
        or early.get('native_posting_viewport', {}).get('synthetic_offline') is not True
        or early.get('native_posting_viewport', {}).get('cleanup_verified') is not True
        or early.get('native_posting_viewport', {}).get('source_commit') != os.environ['GITHUB_SHA']
        or early.get('native_posting_viewport', {}).get('external_actions') != []
        or 'test_posting_crop_transition_r62.py' not in early.get('groups', [])
        or 'test_posting_dom.py' not in early.get('groups', [])
        or 'test_*r31.py' not in early.get('groups', [])
        or 'test_standalone_nurture_browser_r6.py' not in early.get('groups', [])
        or 'test_final_seed_browser_r62.py' not in early.get('groups', [])
        or early.get('final_seed_browser', {}).get('verified') is not True
        or 'test_nurture_completion_cleanup_r62.py' not in early.get('groups', [])
        or 'test_installed_standalone_nurture_r6.py' not in early.get('groups', [])
        or 'test_live_parent_recheck_r6.py' not in early.get('groups', [])
        or 'test_installed_collection_completion_r97.py' not in early.get('groups', [])):
    raise RuntimeError('Mandatory R6.3 browser/native proof is incomplete or has the wrong source identity')
if early.get('native_fixtures') != ['desktop/tests/nurture-cleanup-native-r63.cjs','desktop/tests/nurture-reels-r6.integration.cjs','desktop/tests/posting-viewport-native-r62.cjs','desktop/tests/posting-r6.integration.cjs','desktop/tests/crop-icon-r64.cjs','desktop/tests/recovery-ui-native-r64.cjs']:
    raise RuntimeError('Required current-source native fixture completion is missing or unexpected')
final_seed_path = output / 'final-seed-fixtures/final-seed-browser-r62.json'
final_seed = json.loads(final_seed_path.read_text(encoding='utf-8'))
if (any(final_seed.get(key) is not True for key in ('verified','synthetic_offline','source_preserved','all_owned_children_closed'))
        or final_seed.get('live_accounts_tested') is not False or final_seed.get('external_requests') != 0
        or final_seed.get('retired_children') != 3 or final_seed.get('replacement_children') != 3
        or not final_seed.get('browser_version')):
    raise RuntimeError('Required native final-source proof is missing or invalid')
final_seed_png = final_seed_path.with_suffix('.png')
if not final_seed_png.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'):
    raise RuntimeError('Required native final-source screenshot is missing')
cleanup_native_path = output / 'r63-nurture-cleanup-native.json'
cleanup_native = json.loads(cleanup_native_path.read_text(encoding='utf-8'))
validate_cleanup_native = runpy.run_path(str(ROOT / 'ci/r63_native_proof.py'))['validate_native']
validate_cleanup_native(cleanup_native,root/'source',os.environ['GITHUB_SHA'])
validate_cleanup_native(early.get('nurture_cleanup_native'),root/'source',os.environ['GITHUB_SHA'],early_root)
for required_group in ('test_installed_nurture_cleanup_upgrade_r63.py','test_nurture_cleanup*.py','test_nurture_closed_profile_guard.py','test_nurture_missing_lease_surface.py'):
    if required_group not in early.get('groups',[]):raise RuntimeError('Missing required cleanup early group: '+required_group)
# R6.4 gates are additive: legacy closure and installed-upgrade receipts above remain mandatory.
validate_crop=runpy.run_path(str(ROOT/'ci/r64_crop_proof.py'))['validate_crop']
crop_native=json.loads((output/'r64-crop-icon-native.json').read_text(encoding='utf-8'))
validate_crop(crop_native,root/'source',os.environ['GITHUB_SHA'],output)
validate_crop(early.get('crop_native'),root/'source',os.environ['GITHUB_SHA'],early_root)
for required_group in ('test_installed_recovery_r64.py','test_combined_recovery_r64.py','test_hidden_collection_blocker_r63.py','test_posting_withdraw*_r63.py','test_posting_durable_preflight_r63.py','test_crop_icon_r64.py','test_crop_guard_r64.py'):
    if required_group not in early.get('groups',[]):raise RuntimeError('Missing required R6.4 early group: '+required_group)
validate_recovery_ui=runpy.run_path(str(ROOT/'ci/r64_recovery_ui_proof.py'))['validate_recovery_ui']
recovery_ui=json.loads((output/'r64-recovery-ui-native-proof.json').read_text(encoding='utf-8'))
validate_recovery_ui(recovery_ui,root/'source',os.environ['GITHUB_SHA'],output)
validate_recovery_ui(early.get('recovery_ui_native'),root/'source',os.environ['GITHUB_SHA'],early_root)
recovery_api=json.loads((output/'installed-recovery-r64.json').read_text(encoding='utf-8-sig'))
validate_recovery_api=runpy.run_path(str(root/'source/scripts/verify_installed_recovery_r64.py'))['validate_proof']
product=json.loads((root/'source/package.json').read_text(encoding='utf-8'))['build']['productName']
validate_recovery_api(recovery_api,executable=Path(installed['installed_root'])/(product+'.exe'),core_executable=upgrade_core)
if not same_json(installed.get('recovery_api'),recovery_api) or recovery_api.get('core_upgrade_manifest_sha256')!=installed['nurture_cleanup_upgrade']['manifest_sha256']:
    raise RuntimeError('Actual installed R6.4 API receipt differs from selected installed Core reports')
for screenshot_name in ('r6-collection-counters.png', 'r6-work-report-summary.png', 'r6-work-report-data-overview.png', 'r6-shell-wolf-accounts.png', 'r6-shell-wolf-collection.png', 'r6-shell-refresh-failure.png', 'r6-nurture-settings.png', 'r6-nurture-history.png', 'r6-nurture-narrow.png', 'r6-nurture-read-error.png', 'r6-posting-actual.png', 'r6-posting-stress.png', 'r62-posting-retry-actual.png'):
    screenshot = output / screenshot_name
    if not screenshot.is_file() or not screenshot.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'):
        raise RuntimeError('Required R6 native UI screenshot is missing: ' + screenshot_name)
for proof_name in ('r6-posting-stress.json', 'r6-posting-stress-bounds.json'):
    proof = output / proof_name
    value = json.loads(proof.read_text(encoding='utf-8'))
    if not value:
        raise RuntimeError('Required posting geometry proof is empty: ' + proof_name)




require(same_json(early.get('source_provenance'),identity) and same_json(early.get('run'),state['run']), 'Early evidence is from another source/run')
for bound in (source_proof, cleanup_native, crop_native, recovery_ui, early['native_posting_viewport'], early['nurture_cleanup_native'], early['crop_native'], early['recovery_ui_native']):
    bind_native(bound,identity)
require(early['groups'] == runpy.run_path(str(ROOT/'ci/public_ci_groups.py'))['PATTERNS'], 'Early backend coverage changed')
verify_run_state()

print('PUBLIC_INSTALLED_ACCEPTANCE=PASS')
