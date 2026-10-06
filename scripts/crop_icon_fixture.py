#!/usr/bin/env python3
"""Export production probes and local fixture HTML for the offline Electron gate."""
import ast
import json
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'backend/tests')]
from app.instagram_crop import CROP_BUTTON
from app.instagram_publisher import FORWARD_HEADER
from test_crop_icon_r64 import fixture_html

variants=('unlabelled','title','aria','roleless','labelled_precedence','scaled','duplicate','disabled','wrong_label',
          'referenced_label','wrong_icon','outside_media','occluded','transformed','invisible_paths','definitions_only',
          'opacity_control','opacity_svg','opacity_media','opacity_shape_group')
probe=CROP_BUTTON
if len(sys.argv)>1:
    with zipfile.ZipFile(sys.argv[1]) as archive:
        module=ast.parse(archive.read('backend/app/instagram_crop.py').decode())
    probe=next(ast.literal_eval(node.value) for node in module.body if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='CROP_BUTTON' for t in node.targets))
print(json.dumps({'probe':probe,'forward':FORWARD_HEADER,'baseline':len(sys.argv)>1,'fixtures':{v:fixture_html(v) for v in variants}},ensure_ascii=False))
