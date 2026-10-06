"""Reject incomplete, stale or non-Windows offline native crop receipts."""
import ast,hashlib
from pathlib import Path
SOURCE_FILES=('backend/app/instagram_crop.py','backend/app/instagram_crop_dom.py','backend/app/instagram_publisher.py','backend/tests/test_crop_icon_r64.py','scripts/crop_icon_fixture.py','desktop/tests/crop-icon-r64.cjs')
VARIANTS=('unlabelled','title','aria','roleless','labelled_precedence','scaled','duplicate','disabled','wrong_label','referenced_label','wrong_icon','outside_media','occluded','transformed','invisible_paths','definitions_only','opacity_control','opacity_svg','opacity_media','opacity_shape_group')
ALLOWED=set(VARIANTS[:6])
def validate_crop(proof,source,commit,artifacts):
    source,artifacts=Path(source),Path(artifacts)
    if not isinstance(proof,dict) or type(proof.get('schema')) is not int or proof.get('schema')!=1 or any(proof.get(k) is not True for k in ('verified','offline','synthetic_offline')) or proof.get('live_accounts_tested') is not False:
        raise RuntimeError('Crop native proof isolation incomplete')
    if proof.get('platform')!='win32' or proof.get('source_commit')!=commit or proof.get('baseline') is not False or not proof.get('electron') or not proof.get('chromium'):
        raise RuntimeError('Crop native runtime or source identity mismatch')
    expected={name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in SOURCE_FILES}
    if proof.get('source_hashes')!=expected:raise RuntimeError('Crop native source hashes mismatch')
    module=ast.parse((source/'backend/app/instagram_crop_dom.py').read_text(encoding='utf-8'))
    probe=next(ast.literal_eval(n.value) for n in module.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='CROP_BUTTON' for t in n.targets))
    if proof.get('probe_sha256')!=hashlib.sha256(probe.encode()).hexdigest():raise RuntimeError('Crop probe identity mismatch')
    if proof.get('external_requests')!=[] or proof.get('emulated_viewport')!={'width':1274,'height':717}:raise RuntimeError('Crop external activity or fixture dimensions changed')
    cases=proof.get('scenarios',[])
    if [c.get('variant') for c in cases]!=list(VARIANTS):raise RuntimeError('Crop scenarios incomplete or unexpected')
    for case in cases:
        name=case['variant'];allowed=name in ALLOWED;p=case.get('probe',{})
        if case.get('clicked') is not allowed or case.get('trusted_clicks')!=([True,True,True] if allowed else []) or any(x is not True for x in case.get('trusted_clicks',[])):raise RuntimeError('Crop click count or trust mismatch: '+name)
        count=2 if name=='duplicate' else 1 if allowed else 0
        method=('crop_label' if name in {'title','aria','labelled_precedence'} else 'crop_corner_icon') if allowed else None
        if type(p.get('count')) is not int or p['count']!=count or p.get('method')!=method:raise RuntimeError('Crop discovery outcome mismatch: '+name)
    capture=proof.get('capture',{})
    if capture.get('file')!='r64-crop-icon-native.png':raise RuntimeError('Crop capture identity mismatch')
    data=(artifacts/capture['file']).read_bytes()
    if not data.startswith(b'\x89PNG\r\n\x1a\n') or capture.get('sha256')!=hashlib.sha256(data).hexdigest():raise RuntimeError('Crop capture missing or changed')
    return proof

