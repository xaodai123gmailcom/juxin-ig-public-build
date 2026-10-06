"""Platform login destinations and in-memory, domain-scoped Cookie imports."""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
import ipaddress
from urllib.parse import urlsplit

from .errors import ValidationError

PLATFORMS = {
    'instagram': {'label':'照片墙（Instagram）','domain':'instagram.com','url':'https://www.instagram.com/?hl=zh-cn'},
    'whatsapp': {'label':'WhatsApp','domain':'whatsapp.com','url':'https://web.whatsapp.com/?lang=zh_cn','hint':'使用手机扫码或关联电话号码登录；Cookie 不能代替设备关联。'},
    'telegram_k': {'label':'Telegram WebK','domain':'telegram.org','url':'https://web.telegram.org/k/','hint':'手机扫码或手机号验证登录。'},
    'telegram_a': {'label':'Telegram WebA','domain':'telegram.org','url':'https://web.telegram.org/a/'},
    'line_biz': {'label':'LINE 官方账号（LineBiz）','domain':'line.biz','url':'https://manager.line.biz/','hint':'适用于 LINE 官方账号管理，个人聊天账号不适用。'},
    'teams': {'label':'Microsoft Teams','domain':'cloud.microsoft','url':'https://teams.cloud.microsoft/','hint':'平台可能要求更新的浏览器版本或组织授权。'},
    'zalo': {'label':'Zalo','domain':'zalo.me','url':'https://chat.zalo.me/'},
    'x': {'label':'X（Twitter）','domain':'x.com','url':'https://x.com/'},
    'tiktok': {'label':'TikTok','domain':'tiktok.com','url':'https://www.tiktok.com/'},
    'snapchat': {'label':'Snapchat','domain':'snapchat.com','url':'https://web.snapchat.com/'},
    'zoom': {'label':'Zoom','domain':'zoom.us','url':'https://app.zoom.us/wc/'},
    'discord': {'label':'Discord','domain':'discord.com','url':'https://discord.com/app'},
    'google_chat': {'label':'Google Chat','domain':'google.com','url':'https://chat.google.com/','hint':'Google 可能限制嵌入环境登录，是否可用以平台提示为准。'},
    'google_voice': {'label':'Google Voice','domain':'google.com','url':'https://voice.google.com/','hint':'需要符合地区及账号资格；Google 可能限制嵌入环境登录。'},
    'custom': {'label':'自定义网页','domain':'','url':''},
}
UNAVAILABLE_PLATFORMS = [
    {'id':'signal','label':'Signal','hint':'官方提供手机和桌面客户端，暂不提供可嵌入的官方聊天网页版。'},
    {'id':'line','label':'LINE 个人版','hint':'需要官方桌面客户端或 Chrome 扩展；目前不能作为普通网页嵌入。'},
]
COOKIE_LIMIT = 512 * 1024


def custom_url(value):
    if not isinstance(value,str) or len(value)>2048 or any(ord(c)<33 for c in value):raise ValidationError('请输入有效的 HTTPS 网页地址')
    try:
        u=urlsplit(value);host=(u.hostname or '').lower()
        if u.scheme!='https' or not host or '.' not in host or u.username or u.password or u.port not in (None,443) or host.endswith(('.local','.localhost','.internal')):raise ValueError()
        if host in {'facebook.com', 'fb.com', 'fb.me', 'messenger.com'} or host.endswith(('.facebook.com', '.messenger.com', '.fb.com', '.fb.me')):
            raise ValidationError('此版本不支持 Facebook 或 Messenger')
        try:ipaddress.ip_address(host)
        except ValueError:pass
        else:raise ValueError()
    except ValueError:raise ValidationError('请输入公开网站的 HTTPS 地址，不支持本机地址或带密码的地址') from None
    return value


def platform_config(platform):
    if isinstance(platform,dict) and platform.get('id')=='custom':
        url=custom_url(platform.get('url'))
        return {'label':'自定义网页','domain':urlsplit(url).hostname,'url':url,'id':'custom'}
    if not isinstance(platform, str) or platform not in PLATFORMS:
        raise ValidationError('请选择支持的软件平台')
    if platform=='custom':raise ValidationError('请填写自定义网页地址')
    return PLATFORMS[platform]


def platform_from_environment(raw):
    try:
        environment = json.loads(raw)
        value = environment.get('platform', 'instagram')
        if value == 'custom':
            custom_url(environment.get('custom_url', ''))
        return value if value in PLATFORMS else 'unknown'
    except (ValueError, TypeError, AttributeError, ValidationError):
        return 'unknown'


def destination_from_environment(raw):
    value=platform_from_environment(raw)
    return {'id':'custom','url':json.loads(raw).get('custom_url','')} if value=='custom' else value


def is_whatsapp_destination(platform):
    try:
        url=urlsplit(platform_config(platform)['url'])
        return url.scheme=='https' and url.hostname=='web.whatsapp.com' and url.port in (None,443) and not url.username and not url.password
    except (ValueError,ValidationError):
        return False


def profile_platforms(connection, *, owner_user_id=None):
    # Archived plans still identify otherwise unbound windows. Moving a plan
    # out of the account list must not turn a WhatsApp window into an IG worker.
    result = {row['profile_id']: 'unknown' for row in connection.execute(
        'SELECT profile_id FROM retired_account_profiles WHERE (? IS NULL OR owner_user_id=?)',
        (owner_user_id, owner_user_id))}
    for row in connection.execute("SELECT profile_id,environment_json FROM account_window_plans WHERE profile_id<>'' ORDER BY archived ASC,updated_at DESC"):
        result.setdefault(row['profile_id'], platform_from_environment(row['environment_json']))
    return result


def platform_for_profile(connection, profile, *, owner_user_id=None):
    if connection.execute('SELECT 1 FROM retired_account_profiles WHERE profile_id=? AND (? IS NULL OR owner_user_id=?)',
                          (profile, owner_user_id, owner_user_id)).fetchone():
        return 'unknown'
    row = connection.execute("SELECT environment_json FROM account_window_plans WHERE profile_id=? ORDER BY archived ASC,updated_at DESC LIMIT 1", (profile,)).fetchone()
    return platform_from_environment(row['environment_json']) if row else 'instagram'


def belongs_to_platform(url, platform):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        domain = platform_config(platform)['domain']
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and (host == domain or (not isinstance(platform,dict) and host.endswith('.' + domain))))
    except (ValueError, TypeError):
        return False


def parse_cookies(text, platform):
    """Normalize JSON exports, Netscape exports or a Cookie request header.

    Fail before touching the browser on any invalid item. Never include cookie
    names/values or parser input in exceptions, logs, plans or cloud backups.
    """
    config = platform_config(platform)
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > COOKIE_LIMIT:
        raise ValidationError('请输入 Cookie，内容不能超过 512 KB')
    text = text.strip().lstrip('\ufeff').strip()
    problem = '格式不正确，请使用完整 JSON、Netscape TXT 或 name=value 请求头'
    try:
        if text.startswith(('[', '{')):
            rows = json.loads(text)
            if isinstance(rows, dict):
                rows = rows['cookies'] if 'cookies' in rows else [rows]
        elif '\t' in text:
            rows = []
            for line in text.splitlines():
                http_only = line.startswith('#HttpOnly_')
                if http_only:line = line[len('#HttpOnly_'):]
                elif not line.strip() or line.startswith('#'):continue
                domain, include_subdomains, path, secure, expiry, name, value = line.split('\t', 6)
                rows.append({'domain': domain, 'path': path, 'secure': secure.upper() == 'TRUE',
                             'expires': float(expiry), 'name': name, 'value': value,
                             'httpOnly': http_only})
        else:
            if text.lower().startswith('cookie:'):text = text.split(':', 1)[1].strip()
            rows = []
            for pair in text.split(';'):
                if not pair.strip():continue
                name, value = pair.strip().split('=', 1)
                rows.append({'name': name.strip(), 'value': value, 'domain': '.' + config['domain']})
        if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
            raise ValueError()
        result = [];seen = {}
        for index, row in enumerate(rows, 1):
            problem = f'第 {index} 项格式不正确，必须包含字符串 name 和 value'
            if not isinstance(row, dict):raise ValueError()
            name, value = row['name'], row['value']
            if (not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}", name)
                    or not isinstance(value, str) or len(value.encode('utf-8')) > 16384
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise ValueError()
            problem = f'第 {index} 项的域名或 URL 不属于所选平台，请重新导出该平台的 Cookie'
            domain = row.get('domain')
            if domain is not None and not isinstance(domain, str):raise ValueError()
            if isinstance(domain, str):domain = domain.strip().lower()
            url = row.get('url')
            if url and (not isinstance(url, str) or not belongs_to_platform(url, platform)):raise ValueError()
            # Some exporters use a single dot for an omitted domain. Scope it
            # exactly as a domainless Cookie header to the selected destination.
            # An explicit foreign URL/domain must never be rewritten into scope.
            if domain in (None, '', '.'):
                domain = urlsplit(url).hostname if url else '.' + config['domain']
            host = domain.lstrip('.').lower()
            if (domain.startswith('..') or len(host)>253
                    or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.'))
                    or not (host == config['domain'] or (not isinstance(platform,dict) and host.endswith('.' + config['domain'])))):
                raise ValueError()
            problem = f'第 {index} 项的 path 不正确，必须是以 / 开头的路径'
            path = row.get('path', '/')
            if not isinstance(path, str) or not path.startswith('/') or any(ord(c) < 32 for c in path):raise ValueError()
            cookie = {'name': name, 'value': value, 'domain': domain.lower(), 'path': path}
            for field in ('httpOnly', 'secure'):
                problem = f'第 {index} 项的 {field} 必须是 true 或 false（不能是带引号的文字）'
                if field in row and not isinstance(row[field], bool):raise ValueError()
                cookie[field] = row.get(field, field == 'secure')
            problem = f'第 {index} 项的有效期不是有效的 Unix 秒时间戳'
            expiry = row.get('expirationDate', row.get('expires', -1))
            if expiry is not None:
                expiry = float(expiry)
                if not math.isfinite(expiry):raise ValueError()
                if expiry > 0:
                    if expiry <= time.time():
                        problem = f'第 {index} 项 Cookie 已过期，请在平台重新登录后导出'
                        raise ValueError()
                    cookie['expires'] = expiry
            problem = f'第 {index} 项的 sameSite 不受支持，请使用 Lax、Strict 或 None'
            same_site = str(row.get('sameSite', '')).lower()
            if same_site in {'none', 'no_restriction', 'lax', 'strict'}:
                cookie['sameSite'] = {'none': 'None', 'no_restriction': 'None', 'lax': 'Lax', 'strict': 'Strict'}[same_site]
                if cookie['sameSite'] == 'None':cookie['secure'] = True
            elif same_site not in {'', 'unspecified'}:raise ValueError()
            key = (cookie['domain'], path, name)
            if key in seen:
                if seen[key] == cookie:continue
                problem = f'第 {index} 项与前面的 Cookie 同名、同域、同路径，但值或属性不同，请重新导出一份完整记录'
                raise ValueError()
            seen[key] = cookie;result.append(cookie)
        return result
    except json.JSONDecodeError as error:
        raise ValidationError(f'Cookie JSON 格式不正确（第 {error.lineno} 行、第 {error.colno} 列），请粘贴完整导出内容') from None
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValidationError('Cookie ' + problem) from None


async def open_platform_window(provider, profile, platform, cookies=None, *, worker_factory=None, inspect_instagram=False):
    from .account_navigation import manual_page, page_load_result
    config = platform_config(platform)
    native=getattr(provider,'native',None)
    if (worker_factory is None and profile.startswith('native:') and platform=='instagram'
            and cookies is None and callable(getattr(native,'open_instagram',None))):
        result=await asyncio.to_thread(native.open_instagram,profile)
        return {**result,'opened':True,'platform':platform,'cookies_imported':0,'login_verified':False}
    if (worker_factory is None and profile.startswith('native:')
            and urlsplit(config['url']).hostname=='web.whatsapp.com'
            and callable(getattr(native,'open_whatsapp',None))):
        # WhatsApp is a manual messaging page. Do not initialize it through
        # Playwright's task debugger, emulation or injected automation scripts.
        result=await asyncio.to_thread(native.open_whatsapp,profile,config['url'],cookies)
        return {**result,'opened':True,'platform':platform,'cookies_imported':len(cookies or []),'login_verified':False}
    if worker_factory is None:
        from .playwright_worker import PlaywrightWorker
        worker_factory = PlaywrightWorker
    worker = worker_factory(provider)
    try:
        await worker.connect(profile)
        async with worker._destructive_action_lease():
            context = worker._context
            page = await manual_page(worker, platform)
            existing = belongs_to_platform(page.url, platform)
            # This is now a manually opened login tab, not a disposable worker tab.
            if worker._worker_owned_page is page:worker._worker_owned_page = None
            if cookies is not None:
                try:
                    await asyncio.wait_for(context.add_cookies(cookies), timeout=15)
                except Exception:
                    raise ValidationError('浏览器未确认 Cookie 导入，请在窗口内检查登录状态后再试') from None
            failed = False
            response = None
            if not existing or cookies is not None:
                try:
                    await page.set_extra_http_headers({'Accept-Language': 'zh-CN,zh;q=0.9'})
                    response = await page.goto(config['url'], wait_until='domcontentloaded', timeout=30000)
                except Exception:
                    # The window remains available for manual network/login handling.
                    failed = True
            load_result = await page_load_result(page, response, network_error=failed)
            try:await page.bring_to_front()
            except Exception:pass
            stats={}
            if inspect_instagram and platform=='instagram' and load_result['page_loaded']:
                from .instagram_home import read_account_posts
                stats['instagram_stats']=await read_account_posts(page)
            if cookies is not None and platform=='instagram' and load_result['page_loaded'] and not any(c.get('name')=='sessionid' and c.get('value') for c in cookies):
                load_result={**load_result,'message':'Cookie 已导入，但缺少 sessionid 登录会话。请在此窗口登录，或从已登录的 Instagram 重新导出完整 Cookie。'}
            return {**stats, **load_result, 'opened': True, 'platform': platform,
                    'cookies_imported': len(cookies) if cookies is not None else 0,
                    'login_verified': False}
    finally:
        await worker.disconnect()
