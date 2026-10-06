"""Location popup layouts matching the reported desktop screenshot, no live account."""
import json,unittest
from pathlib import Path
import test_posting_dom as fixtures
HTML=fixtures.HTML
SCREENSHOT_SCRIPT=fixtures.SCREENSHOT_SCRIPT
from app.instagram_publisher import InstagramPublisher
from app.errors import ValidationError

POPUP_SCRIPT=r'''<script>
window.rowClicks=[];
function places(){
 document.querySelector('#places')?.remove();if(window.noResults)return;
 const field=document.querySelector('input[placeholder="添加地点"]'),r=field.getBoundingClientRect();
 const popup=document.createElement('div');popup.id='places';
 popup.style.cssText=`position:fixed;left:${r.left}px;top:${r.top-206}px;width:${r.width+10}px;height:200px;overflow:auto;z-index:99;background:white;color:black;text-align:left;border-radius:8px;box-shadow:0 3px 10px #999;`;
 const scroller=document.createElement('div');
 ['San Francisco, California','旧金山','San Francisco, California'].forEach((name,index)=>{
  const row=document.createElement('div');row.style.cssText='height:60px;display:flex;align-items:center;padding:0 16px;box-sizing:border-box;cursor:default';
  row.innerHTML='<span>'+name+'</span>';
  row.onmousedown=()=>{
   window.rowClicks.push(index);report('location-click',{name});if(window.rejectSelection)return;
   if(window.pickChip){const old=field.parentElement;const fresh=document.createElement('div');fresh.style.cssText='margin-top:100px;height:24px;text-align:left';fresh.innerHTML='<button style="padding:0;background:none">'+name+'</button>';old.replaceWith(fresh);}
   else {field.value=name;field.readOnly=true;}
   window.place=name;popup.remove();
  };
  scroller.append(row);
 });
 popup.append(scroller);
 if(window.portal)document.body.append(popup);else composer.append(popup);
}
</script>'''

class LocationPopupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.PostingDOMTests.asyncSetUp
    asyncTearDown=fixtures.PostingDOMTests.asyncTearDown
    load_fixture=fixtures.PostingDOMTests.load_fixture

    def assert_original_before_location(self):
        kinds=[event['kind'] for event in self.events]
        self.assertEqual(1,kinds.count('crop-menu'))
        self.assertEqual(1,kinds.count('crop-original'))
        self.assertEqual(1,kinds.count('continue-crop'))
        self.assertNotIn('wrong-ratio',kinds)
        self.assertLess(kinds.index('crop-menu'),kinds.index('crop-original'))
        self.assertLess(kinds.index('crop-original'),kinds.index('continue-crop'))
        self.assertLess(kinds.index('continue-crop'),kinds.index('location-click'))

    async def setup_popup(self,portal=True,chip=False,reject=False,missing_ratio=False):
        self.events=[]
        await self.context.expose_binding('fixtureEvent',lambda source,event:self.events.append(event))
        settings=f'<script>window.portal={json.dumps(portal)};window.pickChip={json.dumps(chip)};window.rejectSelection={json.dumps(reject)};</script>'
        if missing_ratio:
            settings+='''<script>
            const drawCrop=window.crop;
            window.crop=()=>{drawCrop();document.querySelector('button[aria-label="选择裁剪"]').remove();};
            </script>'''
        html=HTML.replace('const composer=','let composer=').replace('</body>',SCREENSHOT_SCRIPT+POPUP_SCRIPT+settings+'</body>')
        await self.load_fixture(html)
        self.pub.step_timeout=.7;self.pub.confirmation_timeout=5
        return [{'id':'picture','path':str(self.asset),'media_type':'photo'}]

    async def test_portal_plain_rows_select_first_despite_duplicate_names(self):
        assets=await self.setup_popup()
        result=await self.pub.publish(assets,'A leisurely stroll in the early hours.','San Francisco')
        self.assertEqual('San Francisco, California',result['location'])
        self.assertEqual([0],await self.worker.page.evaluate('window.rowClicks'))
        self.assertEqual(1,sum(e['kind']=='share' for e in self.events))
        self.assert_original_before_location()

    async def test_inline_plain_rows_without_button_roles_or_pointer_cursor(self):
        assets=await self.setup_popup(portal=False)
        result=await self.pub.publish(assets,'文案','San Francisco')
        self.assertEqual('San Francisco, California',result['location'])
        self.assertEqual([0],await self.worker.page.evaluate('window.rowClicks'))
        self.assert_original_before_location()

    async def test_replaced_location_section_confirms_selected_chip(self):
        assets=await self.setup_popup(chip=True)
        result=await self.pub.publish(assets,'文案','San Francisco')
        self.assertEqual('San Francisco, California',result['location'])
        self.assertEqual([0],await self.worker.page.evaluate('window.rowClicks'))
        self.assert_original_before_location()

    async def test_unaccepted_selection_keeps_popup_and_never_shares(self):
        assets=await self.setup_popup(reject=True)
        with self.assertRaisesRegex(ValidationError,'未确认选中'):
            await self.pub.publish(assets,'San Francisco, California','San Francisco')
        self.assertFalse(any(e['kind']=='share' for e in self.events))
        self.assertEqual([0],await self.worker.page.evaluate('window.rowClicks'))
        self.assert_original_before_location()

    async def test_photo_missing_ratio_control_stops_before_location_or_share(self):
        assets=await self.setup_popup(missing_ratio=True)
        original_wait=self.pub.wait_for
        async def short_crop_wait(check,message,timeout=None):
            return await original_wait(check,message,timeout=.2 if '裁剪页未找到比例按钮' in message else timeout)
        self.pub.wait_for=short_crop_wait
        with self.assertRaisesRegex(ValidationError,'裁剪页未找到比例按钮'):
            await self.pub.publish(assets,'文案','San Francisco')
        kinds=[event['kind'] for event in self.events]
        self.assertIn('upload',kinds)
        self.assertFalse({'continue-crop','location-click','share'} & set(kinds))
        self.assertTrue(self.asset.is_file())

    async def test_result_panel_replaced_during_read_is_rediscovered_without_reposting(self):
        assets=await self.setup_popup()
        original=self.pub.dialog;replaced=False
        async def dialog():
            nonlocal replaced
            root=await original()
            if self.pub.submitted and root is not None and not replaced:
                replaced=True
                await self.worker.page.evaluate("replacePanel('<h2>Reels 已分享</h2><p>你的 Reels 已分享。</p>')")
                # The old locator is now detached, as during a React modal swap.
                await root.evaluate('el=>el.innerText',timeout=30)
            return root
        self.pub.dialog=dialog
        result=await self.pub.publish(assets,'文案','San Francisco')
        self.assertEqual(1,result['published']);self.assertTrue(replaced)
        self.assertEqual(1,sum(e['kind']=='share' for e in self.events))
        self.assert_original_before_location()
