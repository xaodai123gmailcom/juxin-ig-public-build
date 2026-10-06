"""Reject missing, stale, non-Windows or incomplete native cleanup evidence."""
import hashlib,struct
from pathlib import Path

SCENARIOS = frozenset('absentAcknowledgement unpublishedOpeningRefused openProfileRefused ownerMismatchOpenRefused malformedIdentityRefused retiringProfileRefused inventoryInvisibleRetirementRefused pendingCdpCleanupRefused resetInProgressRefused nativeCloseFailureRetained cookieFlushFailureRetained storageFlushFailureRetained transportDisposeFailureRetained unauthenticatedProviderRefused unknownProviderMethodRefused providerUnavailableRefused normalOpenAfterAbsence surfaceGrantEnforced'.split())

def validate_native(proof, source_root, source_commit, proof_root=None):
    if not isinstance(proof,dict) or proof.get('schema') != 1 or proof.get('gate') != 'r63-nurture-cleanup-native':
        raise RuntimeError('Missing native cleanup proof schema')
    for flag in ('verified','required_mode','native_runtime','cleanup_verified','synthetic_offline'):
        if proof.get(flag) is not True:raise RuntimeError('Missing required native cleanup flag: '+flag)
    if proof.get('platform') != 'win32' or proof.get('source_commit') != source_commit:
        raise RuntimeError('Native cleanup platform or source identity mismatch')
    if not proof.get('electron') or not proof.get('chromium') or proof.get('external_actions') != []:
        raise RuntimeError('Native cleanup runtime or no-action evidence missing')
    scenarios=proof.get('scenarios',{})
    if set(scenarios) != SCENARIOS or any(v is not True for v in scenarios.values()):
        raise RuntimeError('Native cleanup scenario coverage incomplete')
    observations=proof.get('observations',[])
    if not isinstance(observations,list) or any(not isinstance(x,dict) for x in observations) or {x.get('scenario') for x in observations} != SCENARIOS:
        raise RuntimeError('Native cleanup observations incomplete')
    sessions=proof.get('offline_sessions',[])
    if not isinstance(sessions,list) or not sessions or any(not isinstance(x,dict) or x.get('external_requests') != [] for x in sessions):
        raise RuntimeError('Native cleanup offline sessions missing or external requests attempted')
    files={'host_source':'desktop/src/embedded-browser.ts','host_compiled':'dist-electron/embedded-browser.js','fixture':'desktop/tests/nurture-cleanup-native-r63.cjs'}
    for key,name in files.items():
        data=(Path(source_root)/name).read_bytes()
        if proof.get('sha256',{}).get(key) != hashlib.sha256(data).hexdigest():
            raise RuntimeError('Native cleanup code binding mismatch: '+key)
    capture=proof.get('screenshot_capture',{})
    shot=proof.get('screenshot',{})
    if capture.get('completed') is not True or not isinstance(capture.get('attempts'),list) or not capture['attempts']:
        raise RuntimeError('Native reopened-page capture incomplete')
    attempt=capture['attempts'][-1]
    if not isinstance(attempt,dict):raise RuntimeError('Native capture attempt is invalid')
    native=attempt.get('native',{});renderer=attempt.get('renderer',{})
    if (native.get('windowVisible') is not True or native.get('minimized') is not False or native.get('paneVisible') is not True
            or native.get('pageBounds') != {'x':0,'y':0,'width':900,'height':700}
            or native.get('paneBounds',{}).get('width') != 900 or native.get('paneBounds',{}).get('height') != 700
            or renderer.get('painted') is not True or renderer.get('ready') != 'complete' or renderer.get('visibility') != 'visible'
            or renderer.get('width') != 900 or renderer.get('height') != 700 or 'captureError' in attempt):
        raise RuntimeError('Native capture lacks a successfully painted visible surface')
    if shot.get('file') != 'r63-nurture-cleanup-native.png' or shot.get('viewport') != {'width':900,'height':700} or shot.get('publish_disabled') is not True:
        raise RuntimeError('Native reopened-page capture identity mismatch')
    image=(Path(proof_root) if proof_root is not None else Path(source_root)/'installer-output')/shot['file']
    data=image.read_bytes()
    if len(data)<33 or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        raise RuntimeError('Native reopened-page PNG invalid')
    width,height=struct.unpack('>II',data[16:24])
    if width<900 or height<700 or shot.get('pixel_size') != {'width':width,'height':height} or shot.get('bytes') != len(data) or shot.get('sha256') != hashlib.sha256(data).hexdigest():
        raise RuntimeError('Native reopened-page PNG hash, size or dimensions mismatch')
    return proof

