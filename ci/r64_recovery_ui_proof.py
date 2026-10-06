"""Release-stage oracle for native production-renderer recovery evidence."""
import hashlib,re
from pathlib import Path
SCENARIOS=('queuedGuards','withdrawPreservesMaterial','withdrawClearsAssignment','repeatedWithdrawCollapsed','staleReviewCannotStart','cleanupRefusesPausedBlocker','hiddenBlockerExactIdentity','repeatedLookupCollapsed','cancelStopNoMutation','liveBlockerStopDisabled','staleStopRejected','stopPreservesHoldAndHistory','stopFeedbackVisible','separateCleanupIntent','cleanedWindowRebind','reviewCancelNoStart','singleReviewedStartIntent')
SOURCE_REQUIRED=('renderer/src/App.tsx','renderer/src/posting-workspace.tsx','renderer/src/nurture-collection-blocker.tsx','renderer/src/standalone-nurture-workspace.tsx','renderer/src/core-client.ts','renderer/tests/fixtures/recovery-ui-r64.tsx','desktop/tests/recovery-ui-native-r64.cjs','desktop/tests/renderer-fixture.cjs','desktop/tests/visible-fixture.cjs')
CAPTURES=('r64-recovery-ui-withdrawn-review.png','r64-recovery-ui-hidden-blocker.png','r64-recovery-ui-stopped-feedback.png','r64-recovery-ui-fresh-review.png')
def require(ok,message):
    if not ok:raise RuntimeError(message)
def validate_recovery_ui(proof,source,commit,artifacts):
    source,artifacts=Path(source),Path(artifacts)
    require(isinstance(proof,dict) and type(proof.get('schema')) is int and proof['schema']==1 and proof.get('gate')=='r64-recovery-ui-native','Native recovery schema is invalid')
    require(all(proof.get(k) is True for k in ('verified','synthetic_offline','native_runtime','required_mode','cleanup_verified')),'Required native recovery proof incomplete')
    require(proof.get('platform')=='win32' and proof.get('windows_release_status')=='passed' and proof.get('source_commit')==commit and proof.get('github_sha')==commit,'Native recovery Windows/source identity mismatch')
    require(proof.get('electron') and proof.get('chromium'),'Native recovery runtime missing')
    require(proof.get('headless') is False and proof.get('native_window')=={'visible':True,'offscreen':False,'content_size':[1440,1050]},'Native recovery is not a visible Windows surface')
    scope=proof.get('scope',{})
    require(scope=={'production_app':True,'production_handlers':True,'native_input':True,'installed_core':False,'user_database':False,'live_instagram':False,'share':False,'os_dialog_automation':False} and all(type(v) is bool for v in scope.values()),'Native recovery scope evidence changed')
    require(proof.get('confirmation_mode')=='production-window.confirm/controlled-response','Native confirmation seam missing')
    require(all(proof.get(k)==[] for k in ('external_requests','external_actions','renderer_errors','unexpected_endpoints')),'Native recovery external action or renderer error')
    require(proof.get('scenarios')==dict.fromkeys(SCENARIOS,True) and all(v is True for v in proof['scenarios'].values()),'Native recovery scenarios incomplete')
    inputs=proof.get('native_input',[])
    require(len(inputs)>=15 and all(e.get('trusted') is True for e in inputs),'Recovery used non-native input')
    hashes=proof.get('source_sha256',{})
    require(set(SOURCE_REQUIRED)<=set(hashes),'Native recovery source inventory incomplete')
    for name,digest in hashes.items():
        p=Path(name)
        require(not p.is_absolute() and '..' not in p.parts and (source/p).is_file(),'Native recovery unsafe source path')
        require(digest==hashlib.sha256((source/p).read_bytes()).hexdigest(),'Native recovery source hash mismatch: '+name)
    starts=proof.get('start_intents',[]);stops=proof.get('stop_intents',[])
    require(len(starts)==1 and len(stops)==2 and type(proof.get('successful_stops')) is int and proof['successful_stops']==1 and type(proof.get('cleanup_successes')) is int and proof['cleanup_successes']==1,'Native recovery repeated mutation mismatch')
    start=starts[0].get('body',{})
    require(start.get('action')=='start' and start.get('job_ids')==['queued-r64'] and len(start.get('reviewed',[]))==1,'Native recovery selected wrong start target')
    reviewed=start['reviewed'][0]
    require(reviewed.get('id')=='queued-r64' and reviewed.get('asset_id')=='asset-r64' and reviewed.get('profile_id')=='w1' and reviewed.get('expected_username')=='fixture.one' and type(reviewed.get('queue_revision')) is int and reviewed['queue_revision']==12,'Native recovery stale review was accepted')
    expected_stop={'action':'stop_cleanup_collection','job_id':'held-r64','task_id':'86feb8fc-f2a6-404f-9459-11dd3f884103','version':7}
    require(all(s.get('body')==expected_stop and type(s['body'].get('version')) is int for s in stops),'Native recovery selected wrong stop target/version')
    shots=proof.get('screenshots',[])
    require([s.get('file') for s in shots]==list(CAPTURES),'Native recovery captures incomplete')
    for shot in shots:
        require(shot.get('native_visible') is True and shot.get('renderer_visibility')=='visible' and shot.get('size')=={'width':1440,'height':1050},'Native recovery capture was hidden or resized')
        data=(artifacts/shot['file']).read_bytes()
        require(data.startswith(b'\x89PNG\r\n\x1a\n') and len(data)>33 and type(shot.get('bytes')) is int and shot['bytes']==len(data) and shot.get('sha256')==hashlib.sha256(data).hexdigest(),'Native recovery capture changed')
    timing=proof.get('timing',{})
    require(type(timing.get('elapsed_ms')) is int and type(timing.get('overall_limit_ms')) is int and 0<=timing['elapsed_ms']<timing['overall_limit_ms']<=150000,'Native recovery exceeded deadline')
    return proof

