"""Select the composer's original aspect ratio using controls, never annotation colors."""
import asyncio
import re
from .errors import ValidationError

ORIGINAL = re.compile(r'^(Original|原版|原始|原始比例|原始尺寸|原图|原圖)$', re.I)

from .instagram_crop_dom import CROP_BUTTON


async def select_original(publisher, root):
    state = {'option': '原版', 'stage': 'finding_ratio_control'}
    publisher.entry_diagnostics['crop'] = state

    async def current_root():
        # React can replace a whole modal while its image is decoding. Always
        # rediscover the current crop panel, without treating its absence as an
        # acknowledgement that the ratio was selected.
        current = await publisher.dialog()
        if current is None:
            return None
        heading = await publisher.visible(current.get_by_text(re.compile(r'^(Crop|裁剪|裁切)$', re.I), exact=True))
        return await publisher.pin_composer(current) if heading is not None else None

    async def read_probe(find):
        from playwright.async_api import Error as BrowserError
        try:
            return await asyncio.wait_for(find(), timeout=2)
        except (asyncio.TimeoutError, BrowserError):
            return None

    async def find_option():
        current = await current_root()
        return await publisher.visible(current.get_by_text(ORIGINAL, exact=True)) if current is not None else None

    async def original_option():
        return await read_probe(find_option)

    async def find_button():
        current = await current_root()
        if current is not None:
            probe = await current.evaluate(CROP_BUTTON, publisher.entry_marker)
            state['control_probe'] = probe
            if probe.get('count') == 1:
                return current.locator(f'[data-juxin-crop="{publisher.entry_marker}"]')
        return None

    async def button():
        return await read_probe(find_button)

    try:
        option = await original_option()
        if option is None:
            target = await publisher.wait_for(button, '裁剪页未找到比例按钮，尚未继续或发布', timeout=30)
            await publisher.progress('3/6 正在打开裁剪比例，选择原版')
            await publisher.browser.guard()
            await target.click(timeout=10000)
            state['stage'] = 'waiting_original_option'
            option = await publisher.wait_for(original_option, '裁剪比例菜单未出现“原版”，尚未继续或发布', timeout=30)
        await publisher.progress('3/6 正在选择原版比例')
        state['stage'] = 'waiting_original_acknowledgement'
        await publisher.browser.guard()
        await option.click(timeout=10000)

        async def selection_acknowledged():
            current = await current_root()
            if current is None:
                return None
            option = await publisher.visible(current.get_by_text(ORIGINAL, exact=True))
            if option is None:
                # The crop dialog remains, while the ratio menu was dismissed.
                return True
            confirmed = await option.evaluate("el=>[el,el.closest('[role=menuitemradio],[role=option],button')].filter(Boolean).some(x=>x.getAttribute('aria-checked')==='true'||x.getAttribute('aria-selected')==='true'||x.getAttribute('data-state')==='checked')")
            return True if confirmed else None

        async def selected():
            return await read_probe(selection_acknowledged)

        await publisher.wait_for(selected, '已点击原版但菜单状态未确认，尚未继续或发布', timeout=15)
        state['stage'] = 'selected'
        state['status'] = 'original_selected'
    except Exception as exc:
        state['error'] = str(exc)[:240]
        raise
    finally:
        await publisher.record_diagnostics()
