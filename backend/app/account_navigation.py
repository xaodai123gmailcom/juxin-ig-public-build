"""Manual navigation and error-page recovery, under the original action lease."""
import asyncio
import re
from .account_platforms import belongs_to_platform, platform_config

INBOX = {'instagram': 'https://www.instagram.com/direct/inbox/',
         'whatsapp': 'https://web.whatsapp.com/'}


def load_failure(status=0, text='', network_error=False):
    if status == 429:
        return '请求过于频繁（HTTP 429），请稍后手动重试。'
    if status == 407:
        return '代理要求身份验证（HTTP 407），请检查该窗口的代理设置。'
    if status >= 400:
        return f'网页请求失败（HTTP {status}），请检查该窗口的网络、代理或平台登录提示。'
    if re.search(r'\b[45]xx\s+(?:Client|Server)\s+Error\b', text, re.I):
        return '网页返回 4xx / 5xx 错误页，请检查该窗口的网络或代理后刷新或重新打开窗口。'
    if network_error:
        return '网页未能完成加载，请检查网络或代理后刷新或重新打开窗口。'
    return ''


async def page_load_result(page, response=None, *, network_error=False):
    status = getattr(response, 'status', 0) or 0
    text = ''
    try:
        text = await asyncio.wait_for(page.evaluate(
            "() => document.title + '\\n' + (document.querySelector('h1')?.innerText || '')"), timeout=3)
    except Exception:
        # The renderer may have crashed or still be loading. Never declare a
        # successful page when its state could not be read.
        network_error = True
    message = load_failure(status, text, network_error)
    return {'page_loaded': not bool(message), 'http_status': status or None, 'message': message}


async def manual_page(worker, platform):
    pages = [p for p in worker._context.pages if not p.is_closed()]
    page = next((p for p in reversed(pages) if belongs_to_platform(p.url, platform)), None)
    if page is None:
        page = next((p for p in reversed(pages) if p.url in ('', 'about:blank', 'chrome-error://chromewebdata/')), None)
    if page is None:
        page = await asyncio.wait_for(worker._context.new_page(), timeout=15)
    # A manual recovery page must survive the automation client's disconnect.
    if worker._worker_owned_page is page:
        worker._worker_owned_page = None
    return page


async def navigate_account(provider, profile, platform, action, *, worker_factory=None):
    config = platform_config(platform)
    if worker_factory is None:
        from .playwright_worker import PlaywrightWorker
        worker_factory = PlaywrightWorker
    worker = worker_factory(provider)
    try:
        await worker.connect(profile)
        async with worker._destructive_action_lease():
            page = await manual_page(worker, platform)
            response = None
            failed = False
            try:
                if action in ('back','forward'):
                    response = await (page.go_back if action=='back' else page.go_forward)(wait_until='domcontentloaded',timeout=30000)
                elif action == 'refresh' and belongs_to_platform(page.url, platform):
                    response = await page.reload(wait_until='domcontentloaded', timeout=30000)
                else:
                    response = await page.goto(INBOX.get(platform,config['url']) if action == 'inbox' and isinstance(platform,str) else config['url'],
                                               wait_until='domcontentloaded', timeout=30000)
            except Exception:
                failed = True
            result = await page_load_result(page, response, network_error=failed)
            try:
                await page.bring_to_front()
            except Exception:
                result = {**result, 'page_loaded': False, 'message': load_failure(network_error=True)}
            return result
    finally:
        await worker.disconnect()
