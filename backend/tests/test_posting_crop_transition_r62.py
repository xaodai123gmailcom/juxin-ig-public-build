"""Offline Chromium regressions for real crop/continue transitions; no IG requests."""
import unittest
from app.errors import ValidationError
import test_posting_dom as fixtures


class CropTransitionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.PostingDOMTests.asyncSetUp
    asyncTearDown = fixtures.PostingDOMTests.asyncTearDown
    load_fixture = fixtures.PostingDOMTests.load_fixture

    async def prepare(self, script):
        html = fixtures.HTML.replace('const composer=', 'let composer=')
        await self.load_fixture(html.replace('</body>', '<script>'+script+'</script></body>'))
        root = await self.pub.create()
        return root, [{'id':'photo', 'path':str(self.asset), 'media_type':'photo'}]

    async def test_slow_crop_transition_clicks_once_then_observes_new_stage(self):
        root, assets = await self.prepare(r'''
        window.cropClicks=0; window.editClicks=0;
        const baseCrop=crop; crop=()=>{baseCrop();
          document.querySelector('#composer > button:last-child').onclick=()=>{
            cropClicks++; clearTimeout(window.pendingCrop); window.pendingCrop=setTimeout(edit,1700);
          };
        };
        const baseEdit=edit; edit=()=>{baseEdit();
          document.querySelector('#composer > button:last-child').onclick=()=>{
            editClicks++; clearTimeout(window.pendingEdit); window.pendingEdit=setTimeout(caption,800);
          };
        };
        ''')
        _, box = await self.pub.upload(root, assets)
        self.assertTrue(await box.is_visible())
        self.assertEqual([1,1,0], await self.pub.page.evaluate('[cropClicks,editClicks,shares]'))

    async def test_roleless_header_continue_is_scoped_away_from_feed_and_carousel(self):
        for word in ('继续', '繼續', 'Continue', 'Next', '下一步'):
            with self.subTest(word=word):
                root, assets = await self.prepare(r'''
                window.wrongNext=0;
                document.querySelector('main').insertAdjacentHTML('afterbegin','<div tabindex="0" onclick="wrongNext++">Continue</div>');
                const baseCrop=crop; crop=()=>{baseCrop();
                  const header=document.createElement('header');
                  header.append(document.querySelector('#composer h2'));
                  const next=document.createElement('div'); next.tabIndex=0;next.textContent=WORD;
                  next.style.cssText='cursor:pointer;position:absolute;right:16px;top:32px';
                  next.onclick=edit;header.append(next);composer.prepend(header);
                  document.querySelector('#composer > button:last-child').remove();
                  composer.insertAdjacentHTML('beforeend','<button aria-label="Next" onclick="wrongNext++"><svg></svg></button>');
                };
                '''.replace('WORD',repr(word)))
                self.pub.step_timeout=.5
                original_wait=self.pub.wait_for
                async def bounded(find,message,timeout=None):
                    return await original_wait(find,message,timeout=.7)
                self.pub.wait_for=bounded
                _, box = await self.pub.upload(root, assets)
                self.assertTrue(await box.is_visible())
                self.assertEqual([0,0],await self.pub.page.evaluate('[wrongNext,shares]'))
                self.pub.wait_for=original_wait

    async def test_no_transition_does_not_repeat_continue_or_submit(self):
        root, assets = await self.prepare(r'''
        window.cropClicks=0; const baseCrop=crop;
        crop=()=>{baseCrop();document.querySelector('#composer > button:last-child').onclick=()=>cropClicks++;};
        ''')
        original_wait=self.pub.wait_for
        async def bounded(find,message,timeout=None):
            return await original_wait(find,message,timeout=.7)
        self.pub.wait_for=bounded
        with self.assertRaises(ValidationError):
            await self.pub.upload(root, assets)
        self.assertEqual([1,0],await self.pub.page.evaluate('[cropClicks,shares]'))
        self.assertFalse(self.pub.submitted)

    async def test_crop_waits_for_upload_busy_state_before_continue(self):
        root, assets = await self.prepare(r'''
        window.prematureNext=0; const baseCrop=crop;
        crop=()=>{baseCrop();composer.setAttribute('aria-busy','true');
          document.querySelector('#composer > button:last-child').onclick=()=>{
            if(composer.getAttribute('aria-busy')==='true'){prematureNext++;return;}edit();
          };
          setTimeout(()=>composer.removeAttribute('aria-busy'),650);
        };
        ''')
        _, box = await self.pub.upload(root, assets)
        self.assertTrue(await box.is_visible())
        self.assertEqual([0,0],await self.pub.page.evaluate('[prematureNext,shares]'))


if __name__=='__main__':unittest.main()
