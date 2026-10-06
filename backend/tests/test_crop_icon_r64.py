"""Offline Chromium fixtures for the screenshot's crop-ratio control.

The pixels establish the position/icon, not the live DOM. These fixtures cover
labelled and unlabelled DOM variants; no real account or posting is exercised.
"""
import unittest
from unittest.mock import AsyncMock
from app.errors import ValidationError
from app.instagram_crop import CROP_BUTTON, select_original
import test_posting_dom as fixtures

CROP_ICON='<svg viewBox="0 0 24 24"><path d="M10 20H4v-6m16-4V4h-6" stroke="white" fill="none" stroke-width="2"/></svg>'
ZOOM_ICON='<svg viewBox="0 0 24 24"><circle cx="10" cy="10" r="6"/><path d="M14 14l7 7M7 10h6M10 7v6"/></svg>'
CAROUSEL_ICON='<svg viewBox="0 0 24 24"><rect x="4" y="6" width="12" height="12" rx="3"/><path d="M8 3h9a4 4 0 0 1 4 4v9"/></svg>'


def fixture_html(variant='unlabelled'):
    import json
    attributes={'unlabelled':'', 'title':'title="选择裁剪"', 'aria':'aria-label="选择裁剪尺寸"',
                'roleless':'', 'labelled_precedence':'aria-label="选择裁剪"', 'disabled':'aria-disabled="true"', 'wrong_label':'aria-label="放大"', 'referenced_label':'aria-labelledby="zoom-label"'}.get(variant,'')
    tag='div' if variant=='roleless' else 'button'
    icon=CROP_ICON if variant!='wrong_icon' else ZOOM_ICON
    control=f'<{tag} id="ratio" {attributes} style="position:absolute;left:16px;bottom:16px;width:32px;height:32px;padding:4px;cursor:pointer">{icon}</{tag}>'
    markup='''<header style="height:42px;display:flex;align-items:center;justify-content:center"><h2>裁剪</h2><div id="continue" tabindex="0" style="position:absolute;right:16px;top:12px;cursor:pointer">继续</div></header><div id="media" style="position:relative;width:500px;height:500px"><canvas width="500" height="500" style="width:500px;height:500px"></canvas>'''+control+'''<button id="zoom" aria-label="放大" style="position:absolute;left:60px;bottom:16px;width:32px;height:32px;padding:4px">'''+ZOOM_ICON+'''</button><button id="carousel" aria-label="Next" style="position:absolute;right:16px;bottom:16px;width:32px;height:32px;padding:4px">'''+CAROUSEL_ICON+'''</button><div id="ratios" hidden style="position:absolute;left:16px;bottom:58px;background:#333;padding:8px;z-index:2"><button id="original">原版</button><button id="square">1:1</button></div></div>'''
    script='''
    window.cropEvents=[];window.wrongClicks=0;
    const baseEdit=edit;edit=()=>{cropEvents.push('edit-stage');baseEdit();};
    crop=()=>{
      composer.style.cssText='display:block;position:fixed;left:382px;top:88px;width:500px;padding:0';
      composer.innerHTML=MARKUP;
      document.querySelector('#ratio').onclick=()=>{cropEvents.push('menu');document.querySelector('#ratios').hidden=false;};
      document.querySelector('#original').onclick=()=>{cropEvents.push('original');document.querySelector('#ratios').hidden=true;};
      document.querySelector('#continue').onclick=()=>{cropEvents.push('next');edit();};
      document.querySelector('#zoom').onclick=document.querySelector('#carousel').onclick=document.querySelector('#square').onclick=()=>wrongClicks++;
    };
    '''.replace('MARKUP',json.dumps(markup))
    if variant=='referenced_label':
        script+="const baseCrop=crop;crop=()=>{baseCrop();composer.insertAdjacentHTML('beforeend','<span hidden id=\"zoom-label\">放大</span>')};"
    if variant in ('duplicate','labelled_precedence'):
        script+="const baseCrop=crop;crop=()=>{baseCrop();const duplicate=document.querySelector('#ratio').cloneNode(true);duplicate.id='duplicate';duplicate.removeAttribute('aria-label');duplicate.style.left='100px';document.querySelector('#media').append(duplicate);};"
    if variant=='outside_media':
        script+="const baseCrop=crop;crop=()=>{baseCrop();document.querySelector('#ratio').style.bottom='470px';};"
    if variant=='occluded':
        script+="const baseCrop=crop;crop=()=>{baseCrop();document.querySelector('#media').insertAdjacentHTML('beforeend','<div style=\"position:absolute;left:0;bottom:0;width:130px;height:70px;z-index:5\">overlay</div>');};"
    if variant=='transformed':
        script+="const baseCrop=crop;crop=()=>{baseCrop();document.querySelector('#ratio path').setAttribute('transform','rotate(90 12 12)');};"
    if variant=='invisible_paths':
        script+="const baseCrop=crop;crop=()=>{baseCrop();document.querySelector('#ratio path').style.visibility='hidden';};"
    if variant=='definitions_only':
        script+="const baseCrop=crop;crop=()=>{baseCrop();const svg=document.querySelector('#ratio svg');svg.innerHTML='<defs>'+svg.innerHTML+'</defs>';};"
    if variant in ('opacity_control','opacity_svg','opacity_media'):
        selector={'opacity_control':'#ratio','opacity_svg':'#ratio svg','opacity_media':'#media'}[variant]
        script+="const baseCrop=crop;crop=()=>{baseCrop();document.querySelector("+json.dumps(selector)+").style.opacity='0';};"
    if variant=='opacity_shape_group':
        script+="const baseCrop=crop;crop=()=>{baseCrop();const svg=document.querySelector('#ratio svg');svg.innerHTML='<g opacity=\"0\">'+svg.innerHTML+'</g>';};"
    if variant=='scaled':
        script+="const baseCrop=crop;crop=()=>{baseCrop();composer.style.transform='scale(.8)';composer.style.transformOrigin='top left';};"
    html=fixtures.HTML.replace('const composer=', 'let composer=').replace('</body>','<script>'+script+'</script></body>')
    return html


class CropIconTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.PostingDOMTests.asyncSetUp
    asyncTearDown=fixtures.PostingDOMTests.asyncTearDown
    load_fixture=fixtures.PostingDOMTests.load_fixture

    async def prepare(self, variant='unlabelled'):
        html=fixture_html(variant)
        await self.load_fixture(html)
        root=await self.pub.create()
        original_wait=self.pub.wait_for
        async def bounded(find,message,timeout=None):return await original_wait(find,message,timeout=.65)
        self.pub.wait_for=bounded
        return root,[{'id':'photo','path':str(self.asset),'media_type':'photo'}]

    async def test_unlabelled_corner_icon_selects_original_then_advances_once(self):
        root,assets=await self.prepare()
        _,box=await self.pub.upload(root,assets)
        self.assertTrue(await box.is_visible())
        self.assertEqual(['menu','original','next','edit-stage'],await self.pub.page.evaluate('cropEvents'))
        self.assertEqual([0,0],await self.pub.page.evaluate('[wrongClicks,shares]'))
        self.assertEqual('crop_corner_icon',self.pub.entry_diagnostics['crop']['control_probe']['method'])
        self.assertFalse(self.pub.submitted)

    async def test_title_and_roleless_controls_are_supported(self):
        for variant in ('title','aria','roleless','labelled_precedence','scaled'):
            with self.subTest(variant=variant):
                root,assets=await self.prepare(variant)
                await self.pub.upload(root,assets)
                self.assertEqual(['menu','original','next','edit-stage'],await self.pub.page.evaluate('cropEvents'))
                self.assertEqual([0,0],await self.pub.page.evaluate('[wrongClicks,shares]'))

    async def test_missing_ambiguous_disabled_or_unrelated_icons_never_click(self):
        for variant in ('duplicate','disabled','wrong_label','referenced_label','wrong_icon','outside_media','occluded','transformed','invisible_paths','definitions_only','opacity_control','opacity_svg','opacity_media','opacity_shape_group'):
            with self.subTest(variant=variant):
                root,assets=await self.prepare(variant)
                with self.assertRaisesRegex(ValidationError,'比例按钮'):
                    await self.pub.upload(root,assets)
                self.assertEqual([],await self.pub.page.evaluate('cropEvents'))
                self.assertEqual([0,0],await self.pub.page.evaluate('[wrongClicks,shares]'))
                self.assertFalse(self.pub.submitted)

    async def test_corner_icon_outside_pinned_composer_is_ignored(self):
        root,assets=await self.prepare()
        await self.pub.page.evaluate("""() => {const outside=document.createElement('button');outside.innerHTML=ICON;outside.onclick=()=>wrongClicks++;document.body.append(outside);} """.replace('ICON',repr(CROP_ICON)))
        await self.pub.upload(root,assets)
        self.assertEqual(0,await self.pub.page.evaluate('wrongClicks'))

    async def test_menu_click_without_original_option_does_not_retry_or_advance(self):
        root,assets=await self.prepare()
        await self.pub.page.evaluate("const baseCrop=crop;crop=()=>{baseCrop();document.querySelector('#original').remove()}")
        with self.assertRaisesRegex(ValidationError,'原版'):
            await self.pub.upload(root,assets)
        self.assertEqual(['menu'],await self.pub.page.evaluate('cropEvents'))
        self.assertEqual([0,0],await self.pub.page.evaluate('[wrongClicks,shares]'))

    async def test_account_or_lease_change_before_crop_click_stops(self):
        root,assets=await self.prepare()
        async def progress(message):
            if '正在打开裁剪比例' in message:self.studio.guard=AsyncMock(side_effect=ValidationError('账号或窗口占用已变化'))
        self.studio.progress=progress
        with self.assertRaisesRegex(ValidationError,'账号或窗口占用已变化'):
            await self.pub.upload(root,assets)
        self.assertEqual([],await self.pub.page.evaluate('cropEvents'))
        self.assertEqual([0,0],await self.pub.page.evaluate('[wrongClicks,shares]'))

    async def test_account_or_lease_change_before_original_click_stops(self):
        root,assets=await self.prepare('aria')
        async def progress(message):
            if '正在选择原版比例' in message:self.studio.guard=AsyncMock(side_effect=ValidationError('账号或窗口占用已变化'))
        self.studio.progress=progress
        with self.assertRaisesRegex(ValidationError,'账号或窗口占用已变化'):
            await self.pub.upload(root,assets)
        self.assertEqual(['menu'],await self.pub.page.evaluate('cropEvents'))
        self.assertEqual([0,0],await self.pub.page.evaluate('[wrongClicks,shares]'))


if __name__=='__main__':unittest.main()
