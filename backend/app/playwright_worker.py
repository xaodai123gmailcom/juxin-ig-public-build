from __future__ import annotations

import asyncio
import base64
import copy
import inspect
import json
import math
import random
import re
import sys
import unicodedata
import weakref
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Literal
from urllib.parse import parse_qs, unquote, urlparse

from .collection_surface import GUARD_EVIDENCE_SCRIPT, RELATION_ROWS_SCRIPT, _RELATION_VISIBLE, relation_scroll_script, relation_position_status, relation_neighbour_status
from .profile_hover_preview import HOVER_PREVIEW_SCRIPT
from .async_cleanup import finish_owned
from .task_page_labels import label_task_page
from .bitbrowser_api import BitBrowserClient
from .errors import DomainError, UpstreamUnavailableError, ValidationError
from .service import normalize_instagram_username


CandidateBatchSink = Callable[..., Awaitable[int | dict[str, Any]]]
CollectionProgressSink = Callable[[dict[str, Any]], Awaitable[None]]


def following_row_button_state(labels: Iterable[str]) -> Literal["following", "not_following", "unknown"]:
    """Exact action labels only: 'Follow' must never match 'Following'."""
    following = {
        "following", "已关注", "关注中", "正在关注", "已關注", "關注中", "正在關注",
        "追蹤中", "已追蹤", "追踪中", "已追踪", "กำลังติดตาม", "フォロー中", "팔로잉",
        "siguiendo", "seguindo", "abonné(e)", "abonné", "abonniert", "segui già",
        "mengikuti", "đang theo dõi", "takiptesin", "подписки", "подписан",
    }
    not_following = {
        "follow", "follow back", "requested", "request sent", "关注", "關注", "追蹤", "追踪",
        "回关", "回關", "回追", "已请求", "已請求", "已申请", "已申請", "请求已发送",
        "已发送请求", "已傳送要求", "已送出邀請", "请求中", "請求中", "ติดตาม", "ติดตามกลับ", "ส่งคำขอแล้ว",
        "フォローする", "フォローバック", "リクエスト済み", "팔로우", "맞팔로우", "요청됨",
        "seguir", "seguir de volta", "solicitado", "solicitud enviada", "s’abonner", "s'abonner",
        "folgen", "angefragt", "segui", "richiesta inviata", "ikuti", "diminta",
        "theo dõi", "đã yêu cầu", "takip et", "istek gönderildi", "подписаться", "запрос отправлен",
    }
    normalize = lambda label: " ".join(unicodedata.normalize("NFKC", str(label)).casefold().split())
    normalized = {normalize(label) for label in labels}
    following = {normalize(label) for label in following}
    not_following = {normalize(label) for label in not_following}
    if normalized & not_following:
        return "not_following"
    if normalized & following:
        return "following"
    return "unknown"


_MONITOR_FOLLOWING_ROWS_SCRIPT = r"""root => {""" + _RELATION_VISIBLE + r"""
  const profile = link => {
    try {
      const url = new URL(link.getAttribute('href') || '', 'https://www.instagram.com/');
      if (!['www.instagram.com', 'instagram.com'].includes(url.hostname)) return null;
      const match = url.pathname.match(/^\/([a-zA-Z0-9._]{1,30})\/?$/);
      return match ? match[1].toLowerCase() : null;
    } catch { return null; }
  };
  const rendered = node => {
    if (node.hidden || node.getAttribute('aria-hidden') === 'true' || !node.getClientRects().length) return false;
    const style = getComputedStyle(node);
    return style.display !== 'none' && style.visibility !== 'hidden';
  };
  const actionSelector = 'button,[role="button"],input[type="button"]';
  const profileLinks = node => Array.from(node.querySelectorAll('a[href]')).filter(link => profile(link));
  const links = profileLinks(root).filter(rendered);
  const rows = links.map(link => {
    const username = profile(link);
    let row = null;
    let actions = [];
    for (let parent = link.parentElement; parent && parent !== root; parent = parent.parentElement) {
      // Stop at a multi-account container. Never borrow a neighbouring row's button.
      const names = new Set(profileLinks(parent).map(profile));
      if (names.size > 1 || (names.size && !names.has(username))) break;
      const controls = Array.from(parent.querySelectorAll(actionSelector)).filter(control =>
        rendered(control) && !control.contains(link) && !control.closest('a[href]'));
      if (controls.length || parent.querySelector('img')) row = parent;
      if (controls.length) actions = controls.flatMap(control => [
        control.innerText || control.textContent || '',
        control.getAttribute('aria-label') || '', control.getAttribute('value') || ''
      ]).map(value => value.trim()).filter(Boolean);
      if (controls.length) break;
    }
    return {link, row, actions};
  });
  const recommendationLabels = new Set([
    '为你推荐','為你推薦','为您推荐','為您推薦','推荐用户','推薦用戶','推荐账户','推薦帳號',
    'suggested for you','suggestions for you','suggested accounts','recommended for you',
    'people you may know','แนะนำสำหรับคุณ','おすすめ','おすすめのアカウント','회원님을 위한 추천',
    'sugerencias para ti','sugestões para você','suggestions pour vous','vorschläge für dich'
  ]);
  const normalize = text => (text || '').normalize('NFKC').trim().toLowerCase().replace(/\s+/g, '');
  const markers = new Set(Array.from(recommendationLabels, normalize));
  const elements = Array.from(root.querySelectorAll('*'));
  const order = new Map(elements.map((node, index) => [node,index]));
  const boundaries = elements.filter(node => rendered(node) &&
    markers.has(normalize(node.innerText || node.textContent)) &&
    !node.closest('a[href],button,[role="button"]') &&
    !node.querySelector('a[href],button,[role="button"]') &&
    !rows.some(item => item.row?.contains(node)));
  const boundary = boundaries.length ? Math.min(...boundaries.map(node => order.get(node))) : Infinity;
  return rows.map(({link,row,actions}) => ({
    href: link.getAttribute('href') || '', actions, has_row: Boolean(row),
    viewport_visible: visibleRow(link),
    recommended: order.get(link) > boundary
  }));
}"""


class WorkerExecutionError(DomainError):
    code = "worker_execution_error"

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        pause_required: bool = True,
        status_code: int = 409,
        retry_after_seconds: float | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "reason": reason,
            "pause_required": pause_required,
        }
        if retry_after_seconds is not None:
            details["retry_after_seconds"] = max(
                0.0, float(retry_after_seconds)
            )
        super().__init__(message, details=details)
        self.code = reason
        self.status_code = status_code


def normalize_direct_message_text(value: Any) -> str:
    """Normalize editor-only artifacts without changing greeting formatting."""

    return (
        str(value or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u00a0", " ")
        .replace("\u200b", "")
        .replace("\ufeff", "")
    )


def direct_recipient_text_matches(
    row_text: str,
    username_norm: str,
    *,
    profile_hrefs: Iterable[str] = (),
) -> bool:
    """Match a Direct recipient only by profile href or an explicit ``@handle``.

    Display names are not unique and may equal another account's username. A bare
    text line is therefore never strong enough evidence for an automated send.
    """

    target = username_norm.strip().lstrip("@").casefold()
    if not target:
        return False
    if any(
        extract_instagram_profile_username(href) == target
        for href in profile_hrefs
    ):
        return True
    return any(
        line.strip().casefold() == f"@{target}"
        for line in row_text.splitlines()
        if line.strip().startswith("@")
    )


def direct_inbox_result_text_matches(
    row_text: str,
    username_norm: str,
    *,
    profile_hrefs: Iterable[str] = (),
) -> bool:
    """Match the structured username field in the first inbox search result.

    Instagram's inbox search currently renders each account as ``display name``
    followed by a bare username without ``@``. That bare value is safe only in
    this tightly scoped result-row position. A display-name collision in the
    first line must never be accepted as the requested account.
    """

    target = username_norm.strip().lstrip("@").casefold()
    if not target:
        return False
    if any(
        extract_instagram_profile_username(href) == target
        for href in profile_hrefs
    ):
        return True
    lines = [" ".join(line.split()) for line in row_text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    # Only the second structured line is Instagram's account handle. Mentions
    # in a later biography line are not recipient identity evidence.
    return lines[1].strip().lstrip("@").casefold() == target


def classify_guard_state(url: str, visible_text: str, *, system_text: bool = False) -> str | None:
    """Use route or standalone system notices, never arbitrary biography substrings."""
    url_value = urlparse(url).path.casefold()
    text = " ".join(visible_text.casefold().split())
    lines = [" ".join(line.casefold().split()).strip(" .!:：。！") for line in visible_text.splitlines() if line.strip()]
    # Body fallback is deliberately conservative when a normal profile is visible.
    profile_visible = sum(value is not None for value in extract_visible_metrics(visible_text)) >= 2
    def notice(marker: str) -> bool:
        if not system_text and profile_visible:
            return False
        for line in lines:
            if line == marker:
                return True
            if line.startswith(marker) and len(line) > len(marker) and line[len(marker)] in " .,:：，。!！":
                # Prefix matching is only safe inside a bounded system surface, or
                # on a short standalone error page without normal profile metrics.
                if system_text or (len(lines) <= 6 and len(text) <= 1200):
                    return True
        return False
    if any(
        marker in url_value
        for marker in (
            "/accounts/login",
            "/accounts/onetap",
            "/accounts/password/reset",
            "/two_factor",
        )
    ) or any(
        notice(marker)
        for marker in (
            "log in to instagram",
            "login to instagram",
            "登录 instagram",
            "登錄 instagram",
            "登入 instagram",
            "đăng nhập instagram",
            "iniciar sesión en instagram",
            "entrar no instagram",
            "masuk ke instagram",
            "instagram にログイン",
            "instagram에 로그인",
            "เข้าสู่ระบบ instagram",
        )
    ):
        return "instagram_login_required"
    if any(
        marker in url_value
        for marker in (
            "/challenge/",
            "/checkpoint/",
            "/accounts/confirm_email",
            "/accounts/confirm_phone",
        )
    ) or any(
        notice(marker)
        for marker in (
            "confirm it's you",
            "confirm it’s you",
            "security check",
            "确认是你本人",
            "確認是你本人",
            "xác nhận đó là bạn",
            "confirma que eres tú",
            "confirme que é você",
            "本人確認",
            "본인 확인",
            "ยืนยันว่าเป็นคุณ",
        )
    ):
        return "instagram_challenge"
    if any(
        notice(marker)
        for marker in (
            "try again later",
            "please wait a few minutes",
            "请稍后再试",
            "請稍後再試",
            "vui lòng đợi vài phút",
            "espera unos minutos",
            "aguarde alguns minutos",
            "tunggu beberapa menit",
            "しばらくしてから",
            "몇 분 후에",
            "โปรดรอสักครู่",
        )
    ):
        return "instagram_rate_limited"
    if notice("action blocked") or notice("限制某些活动"):
        return "instagram_action_blocked"
    if any(
        notice(marker)
        for marker in (
            "page isn't available",
            "page isn’t available",
            "content isn't available",
            "content isn’t available",
            "抱歉，无法访问此页面",
            "此内容不可用",
            "此內容無法使用",
            "trang này không hiện có",
            "nội dung này không hiện có",
            "esta página no está disponible",
            "este contenido no está disponible",
            "esta página não está disponível",
            "este conteúdo não está disponível",
            "halaman ini tidak tersedia",
            "konten ini tidak tersedia",
            "このページはご利用いただけません",
            "このコンテンツは利用できません",
            "페이지를 사용할 수 없습니다",
            "콘텐츠를 사용할 수 없습니다",
            "ไม่มีหน้านี้",
            "เนื้อหานี้ไม่พร้อมใช้งาน",
        )
    ):
        return "instagram_content_not_visible"
    return None


_PRIVATE_PROFILE_MARKERS = (
    "this account is private",
    "this profile is private",
    "这是私密主页",
    "這是私密主頁",
    "这是私密帐户",
    "这是私密账户",
    "此帐户为私密帐户",
    "此账户为私密账户",
    "该帐户为私密帐户",
    "该账户为私密账户",
    "此帐号不公开",
    "此账号不公开",
    "此帳號不公開",
    "此帳戶不公開",
    "这个账户是私密账户",
    "這個帳戶是私密帳戶",
    "บัญชีนี้เป็นส่วนตัว",
    "esta cuenta es privada",
    "esta conta é privada",
    "ce compte est privé",
    "dieses konto ist privat",
    "questo account è privato",
    "akun ini bersifat pribadi",
    "akun ini privat",
    "tài khoản này ở chế độ riêng tư",
    "tài khoản này là riêng tư",
    "このアカウントは非公開です",
    "非公開アカウント",
    "비공개 계정입니다",
    "этот аккаунт закрыт",
    "هذا الحساب خاص",
    "bu hesap gizli",
    "dit account is privé",
    "to konto jest prywatne",
    "यह अकाउंट प्राइवेट है",
    # Instagram also renders a relationship prompt instead of the short private
    # heading in a number of desktop experiments.  These are still visible-page
    # signals; no hidden endpoint or page source is inspected.
    "follow this account to see their photos and videos",
    "follow this profile to see their photos and videos",
    "follow to see their photos and videos",
    "关注此帐户即可查看对方的照片和视频",
    "关注此账户即可查看对方的照片和视频",
    "关注后即可查看对方的照片和视频",
    "关注即可查看其照片和视频",
    "追蹤此帳號即可查看對方的相片和影片",
    "關注即可查看其相片和影片",
    "ติดตามบัญชีนี้เพื่อดูรูปภาพและวิดีโอ",
    "sigue esta cuenta para ver sus fotos y vídeos",
    "siga esta conta para ver suas fotos e vídeos",
    "suivez ce compte pour voir ses photos et vidéos",
    "folge diesem konto, um seine fotos und videos zu sehen",
    "このアカウントをフォローすると、写真や動画を見ることができます",
    "이 계정을 팔로우하면 사진과 동영상을 볼 수 있습니다",
)

_PRIVATE_RELATIONSHIP_STATE_MARKERS = {
    "requested",
    "request sent",
    "follow request sent",
    "已请求",
    "已发送",
    "请求已发送",
    "關注請求已傳送",
    "已發送追蹤請求",
    "กำลังรอ",
    "solicitud enviada",
    "solicitação enviada",
    "demande envoyée",
    "anfrage gesendet",
    "リクエスト済み",
    "요청됨",
}

_PRIVATE_HEADING_MARKERS = {
    "private account",
    "private instagram account",
    "这是私密主页",
    "這是私密主頁",
    "私密账户",
    "私密帐户",
    "非公開アカウント",
    "비공개 계정",
    "cuenta privada",
    "conta privada",
    "compte privé",
}

_PROFILE_FOLLOW_CONTROL_MARKERS = {
    "follow",
    "关注",
    "關注",
    "追蹤",
    "ติดตาม",
    "seguir",
    "suivre",
    "folgen",
    "フォローする",
    "팔로우",
}

_FOLLOW_CONFIRMED_MARKERS = _PRIVATE_RELATIONSHIP_STATE_MARKERS | {
    "following",
    "followed",
    "已关注",
    "正在关注",
    "已關注",
    "追蹤中",
    "กำลังติดตาม",
    "siguiendo",
    "seguindo",
    "abonné(e)",
    "gefolgt",
    "フォロー中",
    "팔로잉",
}

_PROFILE_SOFT_LOAD_FAILURE_MARKERS = (
    "couldn't refresh feed",
    "couldn't load posts",
    "something went wrong",
    "reload page",
    "无法刷新动态",
    "无法加载帖子",
    "出了点问题",
    "重新加载页面",
)

_PROFILE_HARD_NETWORK_FAILURE_MARKERS = (
    "this site can't be reached",
    "you are offline",
    "no internet",
    "err_internet_disconnected",
    "err_network_changed",
    "err_connection_reset",
    "err_connection_timed_out",
    "网络连接已中断",
    "未连接到互联网",
    "无法访问此网站",
)

_PROFILE_LOAD_FAILURE_MARKERS = (
    *_PROFILE_SOFT_LOAD_FAILURE_MARKERS,
    *_PROFILE_HARD_NETWORK_FAILURE_MARKERS,
)

_PAGE_CONNECTION_ERROR_MARKERS = (
    "target page, context or browser has been closed",
    "page has been closed",
    "browser has been closed",
    "context has been closed",
    "websocket is not open",
    "connection closed",
    "connection reset",
    "connection refused",
    "socket hang up",
    "target closed",
    "browser disconnected",
)

_PAGE_NETWORK_ERROR_MARKERS = (
    "net::err_",
    "err_internet_disconnected",
    "err_network_changed",
    "err_connection_reset",
    "err_connection_closed",
    "err_name_not_resolved",
    "err_connection_timed_out",
)

_PROFILE_PRIVACY_RESPONSE_GRACE_SECONDS = 0.20
# A semantic main/header can precede profile data. Let that same document settle
# before replacing it; navigating again would restart the very request we need.
_PROFILE_DATA_SETTLE_SECONDS = 3.0
_PROFILE_DATA_POLL_SECONDS = 0.15

# An incomplete profile is infrastructure evidence, never UNKNOWN account data.
# Diagnose it once in a fresh same-account page before requesting intervention.


def is_instagram_navigation_shell(body_text: str) -> bool:
    """A remaining navigation/message bar is not a rendered profile."""
    labels = {
        "instagram", "messages", "message", "home", "search", "explore",
        "reels", "notifications", "create", "profile", "more", "meta",
        "消息", "訊息", "首页", "首頁", "搜索", "搜尋", "探索", "通知",
        "创建", "建立", "个人主页", "個人檔案", "更多", "threads",
    }
    lines = [" ".join(line.casefold().split()) for line in body_text.splitlines() if line.strip()]
    return bool(lines) and all(line in labels or line.isdecimal() for line in lines)


_PROFILE_TAB_RECOVERY_REASONS = {
    "instagram_network_unavailable",
    "instagram_profile_not_ready",
    "browser_window_surface_unstable",
    "instagram_profile_dom_unrecognized",
}
_PROFILE_RECOVERY_AUTHORITATIVE_REASONS = {
    "instagram_login_required",
    "instagram_challenge",
    "instagram_rate_limited",
    "instagram_action_blocked",
    "instagram_content_not_visible",
}
_PROFILE_RECOVERY_NAVIGATION_TIMEOUT_MILLISECONDS = 30_000
_PROFILE_RECOVERY_SURFACE_TIMEOUT_MILLISECONDS = 10_000
_UNKNOWN_VISIBILITY_WAIT_MILLISECONDS = 1_400
_VISIBLE_LOADING_SELECTOR = (
    '[role="progressbar"], [aria-busy="true"], '
    'svg[aria-label*="loading" i], svg[aria-label*="加载"], '
    'svg[aria-label*="載入"], svg[aria-label*="đang tải" i], '
    'svg[aria-label*="cargando" i], svg[aria-label*="memuat" i]'
)
_LOCATION_FLOW_ATTEMPTS = 2
_LOCATION_FLOW_RETRY_DELAY_SECONDS = 0.35
_LOCATION_TRIGGER_POLL_ATTEMPTS = 8
_LOCATION_TRIGGER_POLL_MILLISECONDS = 250
_LOCATION_DIALOG_POLL_ATTEMPTS = 20
_LOCATION_DIALOG_POLL_MILLISECONDS = 150
_LOCATION_LOAD_FAILURE_MARKERS = (
    "failed to load",
    "couldn't load",
    "could not load",
    "unable to load",
    "加载失败",
    "載入失敗",
    "无法加载",
    "無法加載",
    "无法载入",
    "無法載入",
)
_ACTIVITY_GRID_POLL_ATTEMPTS = 12
_ACTIVITY_GRID_POLL_MILLISECONDS = 250
_TRANSIENT_PROFILE_REASONS = {
    "instagram_network_unavailable",
    "instagram_profile_not_ready",
    "instagram_profile_dom_unrecognized",
    "browser_window_surface_unstable",
}

_PUBLIC_EMPTY_PROFILE_MARKERS = (
    "no posts yet",
    "尚无帖子",
    "还没有帖子",
    "尚無貼文",
    "還沒有貼文",
    "ยังไม่มีโพสต์",
    "aún no hay publicaciones",
    "ainda não há publicações",
    "belum ada postingan",
    "chưa có bài viết nào",
    "投稿はまだありません",
    "게시물 없음",
)

# Current Simplified/Traditional Chinese desktop builds use this friendlier copy.
# It is intentionally separate from the established explicit phrases because the
# words alone could also occur in a bio; classification requires a visible 0-post
# counter alongside one of these markers.
_PUBLIC_ZERO_POST_COPY_MARKERS = (
    "这里空荡荡",
    "這裡空蕩蕩",
)


def classify_profile_visibility(
    visible_text: str,
    *,
    has_visible_posts: bool,
    has_private_indicator: bool = False,
    structured_is_private: bool | None = None,
    has_stable_private_structure: bool = False,
) -> Literal["public", "private", "unknown"]:
    """Classify only evidence visible on the rendered profile surface.

    Relationship/profile counts are intentionally not accepted as proof of a public
    account because Instagram renders those counts for private accounts too.
    """
    folded = " ".join(visible_text.casefold().split())
    if (
        structured_is_private is True
        or has_private_indicator
        or any(marker in folded for marker in _PRIVATE_PROFILE_MARKERS)
    ):
        return "private"
    zero_copy_positions = [
        position for marker in _PUBLIC_ZERO_POST_COPY_MARKERS
        if (position := folded.find(marker)) >= 0
    ]
    has_zero_post_copy = bool(zero_copy_positions) and extract_visible_metrics(
        folded[:min(zero_copy_positions)]
    )[2] == 0
    if (
        has_visible_posts
        or any(marker in folded for marker in _PUBLIC_EMPTY_PROFILE_MARKERS)
        or has_zero_post_copy
    ):
        return "public"
    if structured_is_private is False:
        return "public"
    if has_stable_private_structure:
        return "private"
    return "unknown"


_REAL_ESTATE_ACCOUNT_CATEGORIES = {
    "房地产",
    "房地產",
    "房地产经纪",
    "房地產經紀",
    "房地产经纪人",
    "房地產經紀人",
    "房产经纪人",
    "房產經紀人",
    "地产经纪人",
    "地產經紀人",
    "房地产公司",
    "房地產公司",
    "房地产服务",
    "房地產服務",
    "real estate",
    "real estate agent",
    "real estate agents",
    "realtor",
    "realtors",
    "real estate broker",
    "real estate brokers",
    "real estate company",
    "real estate companies",
    "real estate service",
    "real estate services",
    "commercial real estate agency",
    "real estate appraiser",
    "real estate developer",
    "real estate investment firm",
    "property consultant",
    "property management company",
}


def normalize_account_category(value: Any) -> str:
    """Normalize one dedicated Instagram account-category label for matching."""

    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    normalized = normalized.replace("®", "").replace("™", "")
    normalized = re.sub(r"[_/|·–—-]+", " ", normalized)
    return " ".join(normalized.split()).strip(" .,:;!()[]{}")


_NORMALIZED_REAL_ESTATE_ACCOUNT_CATEGORIES = frozenset(
    normalize_account_category(item) for item in _REAL_ESTATE_ACCOUNT_CATEGORIES
)


def is_real_estate_account_category(value: Any) -> bool:
    """Match only Instagram's dedicated category field, never biography text."""

    return normalize_account_category(value) in _NORMALIZED_REAL_ESTATE_ACCOUNT_CATEGORIES


def external_profile_link_url(value: Any) -> str | None:
    """Return a real advertising/off-platform profile link, including link shims.

    Instagram renders the account's Threads identity as a blue link in the same
    header region as biography links.  It is an account-connection affordance,
    not the external biography URL this screening rule is intended to reject.
    """

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.startswith("//"):
        raw = f"https:{raw}"
    try:
        parsed = urlparse(raw)
    except (TypeError, ValueError):
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if host in {"l.instagram.com", "l.threads.net", "l.threads.com"}:
        query = parse_qs(parsed.query)
        for key in ("u", "url"):
            for destination in query.get(key, ()):
                resolved = external_profile_link_url(destination)
                if resolved is not None:
                    return resolved
        return None
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return None
    if any(
        host == threads_host or host.endswith(f".{threads_host}")
        for threads_host in ("threads.net", "threads.com")
    ):
        # The built-in Threads identity badge links to exactly one @handle
        # profile. A post, share or other Threads URL is still an explicit bio
        # destination and must remain eligible for external-link exclusion.
        threads_path = unquote(parsed.path).strip("/")
        if re.fullmatch(r"@[A-Za-z0-9._]{1,64}", threads_path):
            return None
    return raw


def _decode_jsonish_values(text: str) -> list[Any]:
    """Decode JSON scripts and JSON embedded in a small JS assignment wrapper."""
    if not text or len(text) > 4_000_000:
        return []
    decoder = json.JSONDecoder()
    starts = [index for index, character in enumerate(text) if character in "{["][:64]
    values: list[Any] = []
    for start in starts:
        try:
            value, _ = decoder.raw_decode(text, start)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        values.append(value)
        # A valid top-level object contains every nested object already; avoid
        # repeatedly decoding each of its opening braces.
        if start <= 64:
            break
    return values


@dataclass(frozen=True, slots=True)
class EmbeddedProfileEvidence:
    """Exact-target evidence already delivered while rendering a profile page."""

    is_private: bool | None = None
    posts_count: int | None = None
    post_datetimes: tuple[str, ...] = ()
    instagram_user_id: str | None = None
    account_category: str | None = None
    external_bio_url: str | None = None
    is_verified: bool | None = None
    is_professional_account: bool | None = None


def _coerce_embedded_post_datetime(
    value: Any,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Normalize visible-page media timestamps without accepting comment times."""
    if isinstance(value, bool) or value is None:
        return None
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    reference = reference.astimezone(timezone.utc)
    parsed: datetime | None = None
    try:
        if isinstance(value, (int, float)):
            numeric = float(value)
            if numeric > 10_000_000_000:
                numeric /= 1000
            parsed = datetime.fromtimestamp(numeric, tz=timezone.utc)
        elif isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            if re.fullmatch(r"\d{10,16}(?:\.\d+)?", raw):
                numeric = float(raw)
                if numeric > 10_000_000_000:
                    numeric /= 1000
                parsed = datetime.fromtimestamp(numeric, tz=timezone.utc)
            else:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (OSError, OverflowError, TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if datetime(2010, 1, 1, tzinfo=timezone.utc) <= parsed <= reference + timedelta(minutes=5):
        return parsed
    return None


def extract_embedded_profile_evidence(
    payloads: Iterable[Any],
    username: str,
    *,
    now: datetime | None = None,
) -> EmbeddedProfileEvidence:
    """Read privacy, post count and post times from an exact target profile object.

    The payloads are responses/inline data already used by the currently visible
    Instagram page.  We do not issue an additional endpoint request.  Post times are
    accepted only from timeline/media-shaped nodes, which avoids mistaking comment or
    suggestion timestamps for the target account's latest post.
    """
    target = username.strip().lstrip("@").casefold()
    if not target:
        return EmbeddedProfileEvidence()
    privacy_matches: set[bool] = set()
    posts_count_matches: set[int] = set()
    datetime_matches: set[str] = set()
    instagram_user_id_matches: set[str] = set()
    account_category_matches: dict[str, str] = {}
    external_bio_urls: set[str] = set()
    verified_matches: set[bool] = set()
    professional_matches: set[bool] = set()
    remaining_nodes = 120_000
    seen_containers: set[int] = set()
    timestamp_keys = {"taken_at_timestamp", "taken_at", "takenat"}
    media_hints = {
        "shortcode", "code", "media_type", "product_type", "display_url",
        "thumbnail_src", "image_versions2", "video_url", "carousel_media",
    }

    def embedded_count(value: Any) -> int | None:
        """Accept only an explicit, non-negative media count from the target node."""

        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
            parsed = int(value.strip())
        else:
            return None
        return parsed if 0 <= parsed <= 2_147_483_647 else None

    def collect_posts_count(profile_node: dict[Any, Any]) -> None:
        # Instagram uses ``media_count`` in web API payloads and a nested ``count``
        # in GraphQL payloads. Read only fields attached to the exact username node;
        # recommendation cards can never provide a fallback value for the target.
        for key in ("media_count", "posts_count", "post_count"):
            parsed = embedded_count(profile_node.get(key))
            if parsed is not None:
                posts_count_matches.add(parsed)
        for key in (
            "edge_owner_to_timeline_media",
            "owner_to_timeline_media",
            "timeline_media",
        ):
            container = profile_node.get(key)
            if not isinstance(container, dict):
                continue
            parsed = embedded_count(container.get("count"))
            if parsed is not None:
                posts_count_matches.add(parsed)

    def collect_profile_metadata(profile_node: dict[Any, Any]) -> None:
        category_value = next(
            (
                profile_node.get(key)
                for key in (
                    "category_name",
                    "business_category_name",
                    "professional_category",
                    "categoryName",
                    "businessCategoryName",
                    "professionalCategory",
                )
                if isinstance(profile_node.get(key), str)
                and profile_node.get(key).strip()
            ),
            None,
        )
        if isinstance(category_value, str):
            normalized_category = normalize_account_category(category_value)
            if normalized_category:
                account_category_matches.setdefault(
                    normalized_category, " ".join(category_value.split())
                )

        verified_value = next(
            (
                profile_node.get(key)
                for key in ("is_verified", "isVerified", "verified")
                if isinstance(profile_node.get(key), bool)
            ),
            None,
        )
        if isinstance(verified_value, bool):
            verified_matches.add(verified_value)

        professional_value = next(
            (
                profile_node.get(key)
                for key in (
                    "is_professional_account",
                    "isProfessionalAccount",
                    "is_business_account",
                    "isBusinessAccount",
                )
                if isinstance(profile_node.get(key), bool)
            ),
            None,
        )
        if isinstance(professional_value, bool):
            professional_matches.add(professional_value)

        for key in (
            "external_url",
            "external_url_linkshimmed",
            "externalUrl",
            "externalUrlLinkshimmed",
        ):
            resolved = external_profile_link_url(profile_node.get(key))
            if resolved is not None:
                external_bio_urls.add(resolved)
        for key in ("bio_links", "bioLinks"):
            links = profile_node.get(key)
            if not isinstance(links, list):
                continue
            for link in links[:20]:
                if isinstance(link, str):
                    resolved = external_profile_link_url(link)
                    if resolved is not None:
                        external_bio_urls.add(resolved)
                    continue
                if not isinstance(link, dict):
                    continue
                for url_key in (
                    "url",
                    "lynx_url",
                    "link_url",
                    "external_url",
                    "href",
                ):
                    resolved = external_profile_link_url(link.get(url_key))
                    if resolved is not None:
                        external_bio_urls.add(resolved)
                        break
    def collect_media_times(value: Any, *, in_timeline: bool = False, depth: int = 0) -> None:
        nonlocal remaining_nodes
        if remaining_nodes <= 0 or depth > 35:
            return
        remaining_nodes -= 1
        if isinstance(value, dict):
            normalized_keys = {str(key).casefold(): key for key in value}
            looks_like_media = bool(media_hints.intersection(normalized_keys))
            for normalized_key, original_key in normalized_keys.items():
                compact_key = normalized_key.replace("-", "_")
                if compact_key in timestamp_keys and (in_timeline or looks_like_media):
                    parsed = _coerce_embedded_post_datetime(value.get(original_key), now=now)
                    if parsed is not None:
                        datetime_matches.add(parsed.isoformat())
            for key, child in value.items():
                folded_key = str(key).casefold()
                if any(marker in folded_key for marker in ("stories", "story", "reels_tray", "reel_tray", "highlight")):
                    continue
                child_in_timeline = in_timeline or any(
                    marker in folded_key
                    for marker in (
                        "timeline_media", "owner_to_timeline", "media_grid",
                        "profile_grid", "clips_grid", "profile_posts",
                    )
                )
                collect_media_times(child, in_timeline=child_in_timeline, depth=depth + 1)
        elif isinstance(value, list):
            for child in value:
                collect_media_times(child, in_timeline=in_timeline, depth=depth + 1)

    def visit(value: Any, depth: int = 0) -> None:
        nonlocal remaining_nodes
        if remaining_nodes <= 0 or depth > 40:
            return
        remaining_nodes -= 1
        if isinstance(value, (dict, list)):
            identity = id(value)
            if identity in seen_containers:
                return
            seen_containers.add(identity)
        if isinstance(value, dict):
            candidate_username = next(
                (
                    value.get(key)
                    for key in ("username", "user_name", "userName")
                    if isinstance(value.get(key), str)
                ),
                None,
            )
            if (
                isinstance(candidate_username, str)
                and candidate_username.strip().lstrip("@").casefold() == target
            ):
                stable_id = next(
                    (
                        value.get(key)
                        for key in ("instagram_user_id", "user_id", "pk", "id")
                        if key in value
                    ),
                    None,
                )
                if not isinstance(stable_id, bool) and stable_id is not None:
                    normalized_stable_id = str(stable_id).strip()
                    if (
                        normalized_stable_id.isdigit()
                        and len(normalized_stable_id) <= 100
                    ):
                        instagram_user_id_matches.add(normalized_stable_id)
                privacy_value = next(
                    (
                        value.get(key)
                        for key in ("is_private", "isPrivate", "is_private_account", "private")
                        if isinstance(value.get(key), bool)
                    ),
                    None,
                )
                if isinstance(privacy_value, bool):
                    privacy_matches.add(privacy_value)
                collect_posts_count(value)
                collect_profile_metadata(value)
                collect_media_times(value)
            for child in value.values():
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
        elif isinstance(value, str) and depth < 12:
            folded = value.casefold()
            if target in folded and any(
                marker in folded
                for marker in (
                    "is_private",
                    "isprivate",
                    "taken_at",
                    "takenat",
                    "media_count",
                    "posts_count",
                    "owner_to_timeline_media",
                    "category_name",
                    "business_category",
                    "professional_category",
                    "external_url",
                    "bio_links",
                    "is_verified",
                    "is_professional_account",
                    "isprofessionalaccount",
                    "is_business_account",
                    "isbusinessaccount",
                )
            ):
                for decoded in _decode_jsonish_values(value):
                    visit(decoded, depth + 1)

    for payload in payloads:
        if isinstance(payload, str):
            folded = payload.casefold()
            if target not in folded or not any(
                marker in folded
                for marker in (
                    "is_private",
                    "isprivate",
                    "taken_at",
                    "takenat",
                    "media_count",
                    "posts_count",
                    "owner_to_timeline_media",
                    "user_id",
                    '"pk"',
                    "category_name",
                    "business_category",
                    "professional_category",
                    "external_url",
                    "bio_links",
                    "is_verified",
                    "is_professional_account",
                    "isprofessionalaccount",
                    "is_business_account",
                    "isbusinessaccount",
                )
            ):
                continue
            for decoded in _decode_jsonish_values(payload):
                visit(decoded)
        else:
            visit(payload)
    privacy = next(iter(privacy_matches)) if len(privacy_matches) == 1 else None
    posts_count = (
        next(iter(posts_count_matches)) if len(posts_count_matches) == 1 else None
    )
    ordered_times = tuple(sorted(datetime_matches, reverse=True))
    instagram_user_id = (
        next(iter(instagram_user_id_matches))
        if len(instagram_user_id_matches) == 1
        else None
    )
    account_category = (
        next(iter(account_category_matches.values()))
        if len(account_category_matches) == 1
        else None
    )
    external_bio_url = (
        sorted(external_bio_urls, key=lambda value: (len(value), value))[0]
        if external_bio_urls
        else None
    )
    is_verified = next(iter(verified_matches)) if len(verified_matches) == 1 else None
    is_professional_account = (
        True
        if account_category is not None
        else next(iter(professional_matches))
        if len(professional_matches) == 1
        else None
    )
    return EmbeddedProfileEvidence(
        is_private=privacy,
        posts_count=posts_count,
        post_datetimes=ordered_times,
        instagram_user_id=instagram_user_id,
        account_category=account_category,
        external_bio_url=external_bio_url,
        is_verified=is_verified,
        is_professional_account=is_professional_account,
    )


def extract_embedded_profile_privacy(
    payloads: Iterable[Any],
    username: str,
) -> bool | None:
    """Extract an exact target's ``is_private`` from page-loaded structured data.

    Instagram often includes the profile node in an inline JSON bootstrap or the
    GraphQL response used to paint the same profile. Suggested users are ignored by
    requiring the privacy flag and target username on the same profile object.
    Conflicting evidence remains unknown.
    """
    return extract_embedded_profile_evidence(payloads, username).is_private


_VISIBLE_COLLECTION_EMPTY_MARKERS = (
    "no followers yet",
    "no following yet",
    "doesn't follow anyone",
    "isn't following anyone",
    "no likes yet",
    "be the first to like this",
    "尚无粉丝",
    "还没有粉丝",
    "没有关注任何人",
    "尚未关注任何人",
    "还没有赞",
    "暂无点赞",
    "尚無粉絲",
    "沒有追蹤任何人",
    "ยังไม่มีผู้ติดตาม",
    "ยังไม่ได้ติดตามใคร",
    "aún no tiene seguidores",
    "no sigue a nadie",
    "ainda não tem seguidores",
    "não segue ninguém",
    "フォロワーはいません",
    "誰もフォローしていません",
    "팔로워가 없습니다",
    "팔로우하는 계정이 없습니다",
)


def classify_visible_collection_surface(
    visible_text: str,
    *,
    parsed_accounts: int,
    known_zero: bool = False,
) -> Literal["populated", "empty", "unrecognized"]:
    """Require positive, visible evidence before declaring a collection complete."""
    if parsed_accounts > 0:
        return "populated"
    folded = " ".join(visible_text.casefold().split())
    if known_zero or any(marker in folded for marker in _VISIBLE_COLLECTION_EMPTY_MARKERS):
        return "empty"
    if re.search(r"(?:^|\s)0\s*(?:followers?|following|likes?|粉丝|粉絲|关注|關注|赞|讚)(?:\s|$)", folded):
        return "empty"
    return "unrecognized"


def has_visible_zero_likes(visible_text: str) -> bool:
    """Recognize only a zero-like state, never an unrelated zero profile metric."""
    folded = " ".join((visible_text or "").casefold().split())
    markers = (
        "no likes yet", "be the first to like this", "还没有赞", "暂无点赞",
        "尚无点赞", "還沒有讚", "暫無點讚", "aún no hay me gusta",
        "ainda não há curtidas", "まだいいねはありません", "좋아요가 없습니다",
    )
    return any(marker in folded for marker in markers) or bool(
        re.search(r"(?:^|\s)0\s*(?:likes?|次?[赞讚]|个赞|個讚)(?:\s|$)", folded)
    )


def extract_instagram_profile_username(href: str) -> str | None:
    """Extract a visible profile-link username from relative or absolute hrefs."""
    value = (href or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        if parsed.netloc.casefold() not in {"instagram.com", "www.instagram.com"}:
            return None
        path = parsed.path
    else:
        path = value.split("?", 1)[0].split("#", 1)[0]
    parts = [part for part in path.split("/") if part]
    if len(parts) != 1:
        return None
    try:
        username_norm, _ = normalize_instagram_username(parts[0])
    except ValidationError:
        return None
    reserved_paths = {
        "accounts", "about", "challenge", "direct", "explore", "legal",
        "p", "reel", "reels", "stories", "web",
    }
    return None if username_norm in reserved_paths else username_norm


def build_instagram_post_likers_url(post_url: str) -> str | None:
    """Return Instagram's normal visible ``liked_by`` route for one post."""
    value = (post_url or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.netloc and parsed.netloc.casefold() not in {"instagram.com", "www.instagram.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].casefold() not in {"p", "reel"}:
        return None
    return f"https://www.instagram.com/{parts[0]}/{parts[1]}/liked_by/"


_POST_LIKERS_TRIGGER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:view\s+(?:all\s+)?)?\d[\d,.\s]*\s+likes?\b",
        r"\bliked\s+by\b.+\b(?:and\s+(?:\d[\d,.\s]*\s+)?others?|others?)\b",
        r"\bliked\s+by\b",
        r"\band\s+(?:\d[\d,.\s]*\s+)?others?\b",
        r"\b\d[\d,.\s]*\s+others?\b",
        r"\b(?:others?|likes?)\s*[:：]?\s*\d[\d,.\s]*\b",
        r"\b\d[\d,.\s]*\s+(?:people|users?)\s+(?:like|liked)\s+this\b",
        r"\b(?:people|users?)\s+(?:who\s+)?liked\s+this\b",
        r"\d[\d,.\s]*\s*(?:次|个|個)?(?:点赞|點讚|[赞讚])",
        r"(?:查看|檢視)(?:全部|所有).{0,18}(?:点赞|點讚|[赞讚])",
        r"(?:另有|其他|其余|其餘)\s*\d[\d,.\s]*\s*(?:人|位|个|個|用户|用戶)?",
        r"(?:和|及).{0,18}(?:其他|其余|其餘).{0,10}(?:人|用户|用戶)",
        r"\d[\d,.\s]*\s*(?:me\s+gusta|curtidas?|j.?aime|gefällt\s+mir)",
    )
)


def is_post_likers_trigger_candidate(
    *,
    text: str = "",
    aria_label: str = "",
    title: str = "",
    href: str = "",
) -> bool:
    """Return true only for a visible likes-count/list control.

    Instagram renders the count as an anchor, button, role-button or nested span,
    depending on locale and experiment. The heart action itself must never match.
    """
    if "/liked_by" in (href or "").casefold():
        return True
    folded = " ".join(
        " ".join(value.split())
        for value in (text, aria_label, title)
        if value and value.strip()
    ).casefold().strip()
    if not folded:
        return False
    action_labels = {
        "like", "unlike", "赞", "讚", "点赞", "點讚", "取消赞", "取消讚",
        "取消点赞", "取消點讚", "me gusta", "curtir", "j’aime", "j'aime",
        "gefällt mir",
    }
    if folded in action_labels:
        return False
    return any(pattern.search(folded) for pattern in _POST_LIKERS_TRIGGER_PATTERNS)


def select_original_post_datetime(
    raw_values: Iterable[str | None],
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Choose the post timestamp from visible ``time`` nodes in one post.

    Comments and replies cannot predate their post, so the earliest valid timestamp
    inside the post article is the original post time.  Future/invalid timestamps are
    ignored instead of being clamped to activity day zero.
    """
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    reference = reference.astimezone(timezone.utc)
    minimum = datetime(2010, 1, 1, tzinfo=timezone.utc)
    maximum = reference + timedelta(minutes=5)
    parsed_values: list[datetime] = []
    for raw_value in raw_values:
        if not raw_value:
            continue
        try:
            parsed = datetime.fromisoformat(raw_value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        if minimum <= parsed <= maximum:
            parsed_values.append(parsed)
    return min(parsed_values) if parsed_values else None


@dataclass(slots=True)
class SelectorStrategy:
    """Versioned, field-calibratable visible-DOM selectors.

    The worker never issues a private Instagram request, exports cookies or bypasses a
    challenge. Privacy classification may passively read the target profile node from
    the same page navigation response that paints the visible profile.
    """

    version: str = "visible-dom-v1"
    dialog: str = 'div[role="dialog"]'
    account_links: str = 'a[href^="/"], a[href^="https://www.instagram.com/"]'
    post_links: str = 'a[href*="/p/"], a[href*="/reel/"]'
    likes_trigger: str = (
        'a[href*="/liked_by"], article a:has-text("likes"), main a:has-text("likes"), '
        'article button:has-text("likes"), main button:has-text("likes"), '
        'article [role="button"]:has-text("likes"), main [role="button"]:has-text("likes"), '
        'article a:has-text("others"), main a:has-text("others"), '
        'article button:has-text("others"), main button:has-text("others"), '
        'article [role="button"]:has-text("others"), main [role="button"]:has-text("others"), '
        'article a:has-text("赞"), main a:has-text("赞"), article button:has-text("赞"), '
        'main button:has-text("赞"), article [role="button"]:has-text("赞"), '
        'main [role="button"]:has-text("赞"), article a:has-text("其他"), '
        'main a:has-text("其他"), article [role="button"]:has-text("其他"), '
        'main [role="button"]:has-text("其他")'
    )
    # Instagram currently ships more than one Direct editor implementation.  Some
    # windows expose a textarea, others a Lexical/Slate contenteditable, and a few
    # experiments put ``role=textbox`` on an outer node instead of the editable
    # element itself.  Selection is intentionally broad here; the worker applies
    # visibility, editability, dialog and conversation-column checks before using a
    # candidate.
    message_composer: str = (
        'main textarea, main [role="textbox"], '
        'main [contenteditable="true"], main [contenteditable="plaintext-only"], '
        'main [contenteditable=""], main [data-lexical-editor="true"], '
        'main [data-slate-editor="true"]'
    )


@dataclass(slots=True)
class _LocationRequestCoordinator:
    """Serialize sensitive About requests shared by one BitBrowser context."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_allowed_at: float = 0.0
    blocked_until: float = 0.0


@dataclass(slots=True)
class CollectionOutcome:
    mode: Literal["followers", "following", "post_likers"]
    usernames: list[str]
    visible_only: bool = True
    skipped_posts: list[str] = field(default_factory=list)
    candidate_count: int | None = None
    # The relationship count rendered by Instagram for the source target.  This is
    # deliberately distinct from ``candidate_count``: the former is the source's
    # visible followers/following total, while the latter is the number of unique
    # usernames durably discovered for this collection mode.  Post-liker totals are
    # frequently hidden or expressed as an uncounted "and others", so callers must
    # preserve ``None`` instead of substituting the configured collection limit.
    source_total: int | None = None


@dataclass(frozen=True, slots=True)
class _VisibleRelationCount:
    """One visible header count, with a legacy field name kept for adapters.

    completion_floor now equals count; no percentage allowance shortens the
    end-check wait. A 99/100 hint must not hide a delayed
    final row. A healthy settled short list can still end after the full grace.
    """

    count: int
    completion_floor: int


@dataclass(slots=True)
class ActionOutcome:
    operation: Literal["follow", "greet"]
    target: str
    status: Literal["confirmed", "already_done"]
    visible_confirmation: str


@dataclass(frozen=True, slots=True)
class AvatarCaptureResult:
    payload: bytes | None
    reason: str | None
    source: str | None = None
    image_width: int | None = None
    image_height: int | None = None
    natural_width: int | None = None
    natural_height: int | None = None
    source_url: str | None = None


@dataclass(slots=True)
class VisibleProfile:
    username: str
    visibility: Literal["public", "private", "unknown", "not_visible"]
    followers: int | None
    following: int | None
    posts: int | None
    recent_post_datetime: str | None = None
    activity_days: int | None = None
    activity_status: str = "not_checked"
    display_name: str | None = None
    visible_description: str | None = None
    avatar_url: str | None = None
    recent_posts: list[dict[str, str]] = field(default_factory=list)
    # Small, temporary screenshots used only by the pending manual-review page.
    # Core clears this cache immediately after approve/reject.
    review_cache: dict[str, Any] = field(default_factory=dict, repr=False)
    # Ephemeral bytes from the already-rendered BitBrowser profile header. They are
    # consumed by local inference and intentionally excluded from ``as_dict`` so no
    # image bytes enter profile_json, API responses, logs or checkpoints.
    avatar_image_bytes: bytes | None = field(default=None, repr=False)
    avatar_capture_reason: str | None = None
    avatar_capture_source: str | None = None
    avatar_capture_width: int | None = None
    avatar_capture_height: int | None = None
    location: str | None = None
    is_verified: bool | None = None
    is_professional_account: bool | None = None
    account_category: str | None = None
    external_bio_url: str | None = None
    instagram_user_id: str | None = None
    # Post age is separate from account activity: a Story must not reset it.
    post_activity_days: int | None = None
    post_activity_status: str = "not_checked"

    def as_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "visibility": self.visibility,
            "followers": self.followers,
            "following": self.following,
            "posts": self.posts,
            "instagram_user_id": self.instagram_user_id,
            "recent_post_datetime": self.recent_post_datetime,
            "activity_days": self.activity_days,
            "activity_status": self.activity_status,
            "post_activity_days": self.post_activity_days,
            "post_activity_status": self.post_activity_status,
            "display_name": self.display_name,
            "visible_description": self.visible_description,
            "avatar_url": self.avatar_url,
            "recent_posts": list(self.recent_posts),
            "avatar_capture_reason": self.avatar_capture_reason,
            "avatar_capture_source": self.avatar_capture_source,
            "avatar_capture_width": self.avatar_capture_width,
            "avatar_capture_height": self.avatar_capture_height,
            "location": self.location,
            "is_verified": self.is_verified,
            "is_professional_account": self.is_professional_account,
            "account_category": self.account_category,
            "external_bio_url": self.external_bio_url,
            "has_external_bio_link": self.external_bio_url is not None,
        }


_COUNTRY_TRANSLATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    ((
        "united states of america",
        "united states",
        "u.s.a.",
        "u.s.",
        "usa",
        "estados unidos de américa",
        "estados unidos",
        "états-unis",
        "vereinigte staaten",
        "stati uniti",
        "amerika serikat",
        "hoa kỳ",
        "สหรัฐอเมริกา",
        "アメリカ合衆国",
        "미국",
        "соединенные штаты",
        "الولايات المتحدة",
        "amerika birleşik devletleri",
        "verenigde staten",
        "stany zjednoczone",
        "संयुक्त राज्य अमेरिका",
        "美国",
        "美國",
    ), "美国"),
    (("people's republic of china", "mainland china", "china", "中国", "中國"), "中国"),
    (("philippines", "the philippines", "菲律宾", "菲律賓"), "菲律宾"),
    (("mexico", "méxico", "墨西哥"), "墨西哥"),
    (("australia", "澳大利亚", "澳洲", "澳大利亞"), "澳大利亚"),
    (("canada", "加拿大"), "加拿大"),
    (("indonesia", "印度尼西亚", "印度尼西亞", "印尼"), "印度尼西亚"),
    (("vietnam", "viet nam", "việt nam", "越南"), "越南"),
    (("cambodia", "柬埔寨"), "柬埔寨"),
    (("laos", "lao people's democratic republic", "lao pdr", "老挝", "寮国", "寮國"), "老挝"),
    (("thailand", "泰国", "泰國"), "泰国"),
    (("singapore", "新加坡"), "新加坡"),
    (("india", "印度"), "印度"),
    (("syria", "syrian arab republic", "叙利亚", "敘利亞"), "叙利亚"),
    (("south africa", "南非"), "南非"),
    (("united kingdom", "great britain", "britain", "uk", "英国", "英國"), "英国"),
    (("malaysia", "马来西亚", "馬來西亞"), "马来西亚"),
    (("japan", "日本"), "日本"),
    (("south korea", "republic of korea", "韩国", "韓國"), "韩国"),
    (("taiwan", "台湾", "台灣"), "中国台湾"),
    (("hong kong", "香港"), "中国香港"),
    (("new zealand", "新西兰", "新西蘭"), "新西兰"),
    (("united arab emirates", "uae", "阿联酋", "阿聯酋"), "阿联酋"),
    (("saudi arabia", "沙特阿拉伯"), "沙特阿拉伯"),
    (("pakistan", "巴基斯坦"), "巴基斯坦"),
    (("bangladesh", "孟加拉国", "孟加拉國"), "孟加拉国"),
    (("brazil", "brasil", "巴西"), "巴西"),
    (("argentina", "阿根廷"), "阿根廷"),
    (("germany", "deutschland", "德国", "德國"), "德国"),
    (("france", "法国", "法國"), "法国"),
    (("italy", "italia", "意大利"), "意大利"),
    (("spain", "españa", "西班牙"), "西班牙"),
)


def translate_location_to_zh(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.split()).strip()
    folded = normalized.casefold()
    for aliases, chinese_name in _COUNTRY_TRANSLATIONS:
        for alias in sorted(aliases, key=len, reverse=True):
            alias_folded = alias.casefold()
            if re.search(rf"(?<![a-z]){re.escape(alias_folded)}(?![a-z])", folded):
                return chinese_name
    return None


def _is_undisclosed_location_value(value: str | None) -> bool:
    """Recognize visible non-disclosure copy, never a country's identity."""
    normalized = " ".join(str(value or "").casefold().split()).strip(" ：:·-—.。")
    for label in sorted(_ACCOUNT_LOCATION_LABELS, key=len, reverse=True):
        if normalized.startswith(label.casefold()):
            normalized = normalized[len(label):].strip(" ：:·-—.。")
            break
    return normalized in {
        "not shared", "not public", "not disclosed", "not available", "unknown",
        "not publicly available", "not provided", "not specified", "undisclosed",
        "location not shared", "location not disclosed", "location not public",
        "country not shared", "country not disclosed", "country not public",
        "未公开", "不公开", "未公開", "不公開", "尚未公开", "尚未公開",
        "地区未公开", "地区不公开", "地區未公開", "地區不公開",
        "未提供", "未分享", "未披露", "不提供", "暂无", "暫無", "未知", "无", "無",
    }


def _translate_labelled_location_value(value: str | None) -> str | None:
    """Normalize a value already proven to belong to the location row.

    Instagram localizes country names to the signed-in window's UI language.  A
    new or uncommon localized country must not become UNKNOWN merely because it is
    absent from our translation aliases.  The fallback is deliberately limited to
    a short human-readable value and is used only after the visible account-location
    label was matched.
    """

    # A short localized string is not necessarily a country. In particular,
    # Instagram's explicit "Not shared" outcome must stay unknown for review.
    if _is_undisclosed_location_value(value):
        return None
    # An empty country row can be followed by the panel's close/back control.
    # The DOM-neighbour fallback may include the label itself, so strip it only
    # for recognizing interface text; never turn that text into a country.
    interface_value = " ".join(str(value or "").casefold().split()).strip(" ：:·-—")
    for label in sorted(_ACCOUNT_LOCATION_LABELS, key=len, reverse=True):
        if interface_value.startswith(label.casefold()):
            interface_value = interface_value[len(label):].strip(" ：:·-—")
            break
    interface_markers = {
        "close", "cancel", "done", "back", "ok", "关闭", "關閉", "取消",
        "完成", "返回", "确定", "確定", "cerrar", "fechar", "fermer",
        "schließen", "chiudi", "tutup", "đóng", "ปิด", "閉じる", "닫기",
        *(marker.casefold() for marker in _ABOUT_ACCOUNT_TITLE_MARKERS),
        *(marker.casefold() for marker in _ABOUT_ACCOUNT_ENTRY_LABELS),
    }
    if interface_value in interface_markers:
        return None
    panel_markers = (*_ABOUT_ACCOUNT_TITLE_MARKERS, *_ABOUT_ACCOUNT_ENTRY_LABELS,
                     *_ABOUT_ACCOUNT_DETAIL_LABELS)
    if any(interface_value.startswith(marker.casefold() + " ")
           for marker in panel_markers):
        return None
    translated = translate_location_to_zh(value)
    if translated is not None:
        return translated
    if not value:
        return None
    normalized = " ".join(value.split()).strip(" ：:·-—")
    if normalized.casefold() in {label.casefold() for label in _ACCOUNT_LOCATION_LABELS}:
        return None
    if normalized.casefold() in {
        label.casefold() for label in _ABOUT_ACCOUNT_DETAIL_LABELS
    }:
        return None
    # This value is already scoped to an exact, visible account-location row. Keep
    # localized country names that are not yet in our Chinese translation table,
    # while rejecting URLs, dates and long explanatory copy from neighbouring rows.
    if (
        1 < len(normalized) <= 64
        and any(character.isalpha() for character in normalized)
        and not any(character.isdigit() for character in normalized)
        and not re.search(r"(?:https?://|www\.|@)", normalized, re.IGNORECASE)
        and len(normalized.split()) <= 8
    ):
        return normalized
    return None


_ABOUT_ACCOUNT_TITLE_MARKERS = (
    "about this account",
    "about this profile",
    "account information",
    "关于此帐户",
    "关于此账户",
    "账户简介",
    "账号简介",
    "帐号简介",
    "帳戶簡介",
    "关于这个账号",
    "關於這個帳號",
    "información sobre esta cuenta",
    "acerca de esta cuenta",
    "sobre esta conta",
    "à propos de ce compte",
    "informationen zu diesem konto",
    "informazioni su questo account",
    "tentang akun ini",
    "giới thiệu về tài khoản này",
    "เกี่ยวกับบัญชีนี้",
    "このアカウントについて",
    "이 계정 정보",
)

_ABOUT_ACCOUNT_ENTRY_LABELS = (
    "about this account",
    "about this profile",
    "account information",
    "关于此帐户",
    "关于此账户",
    "关于这个账号",
    "關於這個帳號",
    "账户简介",
    "账号简介",
    "información sobre esta cuenta",
    "acerca de esta cuenta",
    "sobre esta conta",
    "à propos de ce compte",
    "informationen zu diesem konto",
    "informazioni su questo account",
    "tentang akun ini",
    "giới thiệu về tài khoản này",
    "เกี่ยวกับบัญชีนี้",
    "このアカウントについて",
    "이 계정 정보",
)

_ACCOUNT_LOCATION_LABELS = (
    "account based in",
    "based in",
    "account location",
    "账户所在地",
    "账号所在地",
    "帐号所在地",
    "帐户所在地",
    "帳戶所在地",
    "所在地",
    "cuenta ubicada en",
    "ubicación de la cuenta",
    "conta baseada em",
    "localização da conta",
    "compte basé à",
    "localisation du compte",
    "kontostandort",
    "account con sede in",
    "posizione dell'account",
    "lokasi akun",
    "tài khoản đặt tại",
    "vị trí tài khoản",
    "ตำแหน่งที่ตั้งของบัญชี",
    "บัญชีตั้งอยู่ใน",
    "アカウントの所在地",
    "계정 기반 위치",
    "계정 위치",
)

_ABOUT_ACCOUNT_DETAIL_LABELS = (
    "date joined",
    "joined",
    "former usernames",
    "shared followers",
    "加入日期",
    "注册日期",
    "曾用账号",
    "曾用用户名",
    "fecha de registro",
    "nombres de usuario anteriores",
    "data de entrada",
    "nomes de usuário anteriores",
    "date d'inscription",
    "anciens noms d'utilisateur",
    "beitrittsdatum",
    "frühere benutzernamen",
    "data di iscrizione",
    "nomi utente precedenti",
    "tanggal bergabung",
    "nama pengguna sebelumnya",
    "ngày tham gia",
    "tên người dùng cũ",
    "วันที่เข้าร่วม",
    "ชื่อผู้ใช้เดิม",
    "登録日",
    "以前のユーザーネーム",
    "가입 날짜",
    "이전 사용자 이름",
)


def extract_about_account_location(visible_text: str | None) -> str | None:
    """Read only the value belonging to the visible account-location row.

    The About panel can contain unrelated country names in explanatory text. A
    country is therefore accepted only on the location-label line or the next
    non-empty line, never by scanning the complete dialog.
    """
    if not visible_text:
        return None
    lines = [" ".join(line.split()).strip() for line in visible_text.splitlines()]
    lines = [line for line in lines if line]
    for index, line in enumerate(lines):
        folded = line.casefold().strip(" ：:")
        for label in _ACCOUNT_LOCATION_LABELS:
            label_folded = label.casefold()
            if folded == label_folded:
                if index + 1 < len(lines):
                    return _translate_labelled_location_value(lines[index + 1])
                return None
            match = re.match(
                rf"^{re.escape(label_folded)}\s*[:：\-]?\s*(.+)$",
                folded,
                re.IGNORECASE,
            )
            if match:
                return _translate_labelled_location_value(match.group(1))
    return None


def _about_account_location_is_undisclosed(visible_text: str | None) -> bool:
    """Accept non-disclosure only in the labelled, visible location row."""
    lines = [" ".join(line.split()).strip() for line in str(visible_text or "").splitlines()]
    lines = [line for line in lines if line]
    for index, line in enumerate(lines):
        folded = line.casefold().strip(" ：:")
        for label in sorted(_ACCOUNT_LOCATION_LABELS, key=len, reverse=True):
            if folded == label.casefold():
                return index + 1 < len(lines) and _is_undisclosed_location_value(lines[index + 1])
            match = re.match(rf"^{re.escape(label)}\s*[:：\-]?\s*(.+)$", line, re.IGNORECASE)
            if match:
                return _is_undisclosed_location_value(match.group(1))
    return False


def is_about_account_load_failure(visible_text: str | None) -> bool:
    """Recognize Instagram's visible failure surface for account details.

    The main profile can remain fully rendered while the secondary request behind
    “About this account” fails. That outcome is transport/session evidence, not
    proof that the account omitted its location.
    """

    folded = " ".join(str(visible_text or "").casefold().split())
    return bool(folded) and any(
        marker in folded for marker in _LOCATION_LOAD_FAILURE_MARKERS
    )


def parse_visible_count(value: str) -> int | None:
    """Parse a localized count already rendered in the visible page."""
    compact = (
        value.strip()
        .casefold()
        .replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
    )
    match = re.search(
        r"(\d[\d.,]*)(mil|rb|jt|k|m|b|万|萬|千|亿|億|만)?",
        compact,
    )
    if not match:
        return None
    raw_number = match.group(1)
    suffix = match.group(2)
    if suffix:
        if "," in raw_number and "." not in raw_number:
            parts = raw_number.split(",")
            raw_number = "".join(parts) if len(parts[-1]) == 3 else ".".join(parts)
        elif "," in raw_number and "." in raw_number:
            raw_number = raw_number.replace(",", "")
    else:
        if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw_number):
            raw_number = raw_number.replace(",", "").replace(".", "")
        else:
            raw_number = raw_number.replace(",", "")
    try:
        number = float(raw_number)
    except ValueError:
        return None
    multiplier = {
        None: 1,
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
        "千": 1_000,
        "万": 10_000,
        "萬": 10_000,
        "만": 10_000,
        "亿": 100_000_000,
        "億": 100_000_000,
        "mil": 1_000,
        "rb": 1_000,
        "jt": 1_000_000,
    }[suffix]
    return int(number * multiplier)


def extract_visible_metrics(text: str) -> tuple[int | None, int | None, int | None]:
    labels = {
        "followers": (
            "followers", "follower", "粉丝", "粉絲", "người theo dõi",
            "ผู้ติดตาม", "seguidores", "seguidor", "pengikut",
            "フォロワー", "팔로워", "abonnés",
        ),
        "following": (
            "following", "关注", "關注", "追蹤", "đang theo dõi",
            "กำลังติดตาม", "siguiendo", "seguidos", "seguindo", "mengikuti",
            "フォロー中", "フォロー", "팔로잉", "abonnements",
        ),
        "posts": (
            "posts", "post", "帖子", "贴文", "貼文", "bài viết", "โพสต์",
            "publicaciones", "publicações", "publicacoes", "postingan", "kiriman",
            "投稿", "掲示物", "게시물", "publications", "beiträge",
        ),
    }
    values: dict[str, int | None] = {"followers": None, "following": None, "posts": None}
    folded = " ".join(text.casefold().replace("\u00a0", " ").replace("\u202f", " ").split())
    count_token = r"\d[\d.,\u00a0\u202f]*(?:\s*(?:mil|rb|jt|k|m|b|万|萬|千|亿|億|만))?"
    label_first = {
        "フォロワー", "フォロー中", "フォロー", "投稿", "掲示物",
        "팔로워", "팔로잉", "게시물",
    }
    for key, candidates in labels.items():
        for label in sorted(set(candidates), key=len, reverse=True):
            escaped = re.escape(label)
            # Chinese desktop counters can be adjacent text nodes whose innerText
            # is "0帖子1粉丝56关注". A following digit starts the next counter;
            # a following letter/underscore still means this is a longer word
            # (e.g. "粉丝团"), not an account statistic. Keep Latin label boundaries
            # strict so ordinary biography words never become profile counters.
            label_boundary = (
                r"(?![^\W\d])"
                if any("\u3400" <= char <= "\u9fff" for char in label)
                else r"(?![\w])"
            )
            before_label = rf"({count_token})\s*{escaped}{label_boundary}"
            after_label = rf"(?<![\w]){escaped}\s*[:：]?\s*({count_token})"
            patterns = (after_label, before_label) if label in label_first else (
                before_label,
                rf"(?<![\w]){escaped}\s*[:：]?\s*({count_token})",
            )
            match = next(
                (found for pattern in patterns if (found := re.search(pattern, folded, re.IGNORECASE))),
                None,
            )
            if match:
                values[key] = parse_visible_count(match.group(1))
                break
    return values["followers"], values["following"], values["posts"]


def extract_public_empty_profile_metrics(
    visible_text: str,
) -> tuple[int, int, int] | None:
    """Accept body-level counts only for an explicit, rendered zero-post grid.

    New Instagram layouts may place the three profile counters outside semantic
    ``header``/statistics containers. Reading the complete body unconditionally
    would risk borrowing counts from recommendations. The public empty-grid copy
    plus a complete counter cluster before that grid is a terminal surface, so it
    can serve as a narrow compatibility fallback without reading suggestions.
    """

    folded = " ".join(visible_text.casefold().split())
    if any(marker in folded for marker in _PRIVATE_PROFILE_MARKERS):
        return None
    empty_positions = [
        position
        for marker in (
            *_PUBLIC_EMPTY_PROFILE_MARKERS,
            *_PUBLIC_ZERO_POST_COPY_MARKERS,
        )
        if (position := folded.find(marker)) >= 0
    ]
    if not empty_positions:
        return None
    # Suggested profiles are rendered after the empty grid. Missing target counts
    # must not be filled from those unrelated accounts merely because the target
    # supplied the zero-post count. Require the complete cluster before the grid.
    followers, following, posts = extract_visible_metrics(folded[:min(empty_positions)])
    if followers is None or following is None or posts != 0:
        return None
    return followers, following, posts


def extract_private_profile_metrics(
    visible_text: str,
) -> tuple[int, int, int] | None:
    """Read a complete count cluster before an explicit private-profile notice.

    Private profiles can use the same header-less counter layout as public empty
    profiles. Their hidden grid is terminal, even while recommendations load.
    Missing counts must not be borrowed from suggested accounts after the notice.
    """
    folded = " ".join(visible_text.casefold().split())
    private_positions = [
        position
        for marker in _PRIVATE_PROFILE_MARKERS
        if (position := folded.find(marker)) >= 0
    ]
    if not private_positions:
        return None
    followers, following, posts = extract_visible_metrics(folded[:min(private_positions)])
    if followers is None or following is None or posts is None:
        return None
    return followers, following, posts


def is_exact_profile_relation_href(
    href: str | None,
    username_norm: str,
    relation: Literal["followers", "following"],
) -> bool:
    """Match one profile relation URL while allowing harmless query parameters."""

    raw = str(href or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    if parsed.netloc and parsed.netloc.casefold() not in {
        "instagram.com", "www.instagram.com",
    }:
        return False
    path = unquote(parsed.path).rstrip("/").casefold()
    expected = f"/{username_norm.strip().lstrip('@').casefold()}/{relation}"
    return path == expected


def _visible_relation_count_estimate(
    visible_text: str, parsed_count: int
) -> _VisibleRelationCount:
    """Retain the visible count without a percentage-based early-end allowance.

    The text has already been parsed. An abbreviated or changing count is still
    only a hint: a healthy short list can end after the full no-progress grace.
    """
    parsed_count = max(0, int(parsed_count))
    return _VisibleRelationCount(count=parsed_count, completion_floor=parsed_count)


_PROFILE_REUSABLE_IDENTITIES = 512
_PROFILE_REUSABLE_ENTRY_BYTES = 64 * 1024
_PROFILE_REUSABLE_CACHE_BYTES = 2 * 1024 * 1024
# One parent + three children can retain four optional captures apiece. More
# windows share this event-loop allowance and use the same exact inline/visible
# fallback when it is occupied; browser connections and required work are uncapped.
_PROFILE_CAPTURE_LOOP_LIMIT = 16
_PROFILE_CAPTURE_TASKS_BY_LOOP: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _profile_metadata_size(value: Any, limit: int = _PROFILE_REUSABLE_ENTRY_BYTES) -> int:
    """Conservative bounded-cost retained-size estimate, never content truncation."""
    stack = [(value, 0)]
    seen: set[int] = set()
    total = 0
    visited = 0
    while stack:
        item, depth = stack.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        visited += 1
        total += sys.getsizeof(item)
        if total > limit or depth > 16 or visited > 4096:
            return max(total, limit + 1)
        if isinstance(item, VisibleProfile):
            stack.extend((getattr(item, entry.name), depth + 1) for entry in fields(item))
        elif isinstance(item, dict):
            if len(item) * 2 > 4096 - visited:
                return limit + 1
            stack.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif isinstance(item, (tuple, list)):
            if len(item) > 4096 - visited:
                return limit + 1
            stack.extend((part, depth + 1) for part in item)
        elif not isinstance(item, (str, bytes, int, float, bool, type(None))):
            return limit + 1  # Unknown objects never become reusable metadata.
    return total


class _ProfileMetadataCache(dict):
    """Account for writes in the existing observation dictionaries in O(entry)."""
    def __init__(self):
        super().__init__()
        self._sizes: dict[str, int] = {}
        self._value_bytes = 0
        self._oversized: set[str] = set()

    def __setitem__(self, key, value):
        size = sys.getsizeof(key) + _profile_metadata_size(value)
        self._value_bytes += size - self._sizes.get(key, 0)
        self._sizes[key] = size
        if size > _PROFILE_REUSABLE_ENTRY_BYTES:
            self._oversized.add(key)
        else:
            self._oversized.discard(key)
        super().__setitem__(key, value)

    def __delitem__(self, key):
        super().__delitem__(key)
        self._value_bytes -= self._sizes.pop(key, 0)
        self._oversized.discard(key)

    def pop(self, key, *default):
        if key in self:
            value = self[key]
            del self[key]
            return value
        if default:
            return default[0]
        raise KeyError(key)

    def clear(self):
        super().clear()
        self._sizes.clear()
        self._oversized.clear()
        self._value_bytes = 0

    def move_to_end(self, key):
        if key in self:
            # Reordering must not repeatedly traverse an unchanged profile.
            value = dict.pop(self, key)
            dict.__setitem__(self, key, value)

    def estimated_bytes(self):
        return self._value_bytes + sys.getsizeof(self) + sys.getsizeof(self._sizes) + sys.getsizeof(self._oversized)


class PlaywrightWorker:
    _PROFILE_CACHE_NAMES = (
        "_profile_privacy_cache", "_profile_user_id_cache", "_profile_verified_cache",
        "_profile_professional_cache", "_profile_category_cache",
        "_profile_external_bio_url_cache", "_profile_posts_count_cache",
        "_profile_post_datetime_cache", "_profile_post_url_cache", "_profile_base_cache",
    )
    # Restore R59's durable batch producer/consumer flow. Keep the newer
    # transport, DOM and window-ownership fixes beneath that scheduling policy.
    collection_pipeline = "r59-batch"
    # Capability retained for older/custom adapters, not the default policy.
    supports_single_candidate_handoff = True
    # ExecutionManager feature-detects this flag.  Test/dummy workers that still
    # return one in-memory CollectionOutcome remain source-compatible, while the real
    # browser worker durably flushes bounded username batches as it scrolls.
    supports_candidate_batch_sink = True
    supports_collection_progress_sink = True
    supports_avatar_image_capture = True
    # ExecutionManager may overlap one source-list producer with one profile
    # screener only when the real worker can create an isolated, worker-owned tab.
    supports_parallel_screening_tab = True
    # read_visible_profile already owns a bounded retry policy.  The execution
    # manager uses this marker to avoid wrapping it in another three-attempt loop,
    # which previously multiplied a short outage into as many as nine navigations.
    handles_profile_read_retries = True

    def __init__(self, bitbrowser: BitBrowserClient, selectors: SelectorStrategy | None = None) -> None:
        self.bitbrowser = bitbrowser
        self.selectors = selectors or SelectorStrategy()
        self.monitor_checkpoint: Callable[[], Awaitable[None]] | None = None
        self._monitor_paused_seconds = 0.0
        self.collection_checkpoint: Callable[[], Awaitable[None]] | None = None
        # A manual-interference request yields at a browser-operation boundary,
        # never by cancelling an in-flight Playwright click/navigation.
        self.manual_control_checkpoint: Callable[[], Awaitable[None]] | None = None
        self._deferred_screening_workers: dict[str, Any] = {}
        self._screening_worker_pool: dict[int, Any] = {}
        self._screening_slots_in_use: set[int] = set()
        self._screening_slots_opening: dict[int, object] = {}
        self._screening_epoch = 0
        self._disconnect_task: asyncio.Task[Any] | None = None
        self._screening_retirement_task: asyncio.Task[Any] | None = None
        self._retired_pages: dict[int, Any] = {}
        self._retired_page_close_tasks: dict[int, asyncio.Task[Any]] = {}
        self._page_factories: set[object] = set()
        self._late_resource_tasks: set[asyncio.Task[Any]] = set()
        self._task_page_slot: int | None = None
        self._collection_paused_seconds = 0.0
        self._monitor_recommendations_reached = False
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._cdp_session: Any = None
        self._cdp_relay: Any = None
        self.page: Any = None
        # Only pages created by this worker (startup, account homepage or recovery) are
        # owned by it. Existing BitBrowser tabs belong to the operator.
        self._worker_owned_page: Any = None
        self.profile_id: str | None = None
        self._connected_endpoint: str | None = None
        self._connection_generation: int | None = None
        self._connecting_attempt_id: str | None = None
        self._profile_privacy_cache: dict[str, bool] = _ProfileMetadataCache()
        self._profile_user_id_cache: dict[str, str] = _ProfileMetadataCache()
        self._profile_verified_cache: dict[str, bool] = _ProfileMetadataCache()
        self._profile_professional_cache: dict[str, bool] = _ProfileMetadataCache()
        self._profile_category_cache: dict[str, str] = _ProfileMetadataCache()
        self._profile_external_bio_url_cache: dict[str, str] = _ProfileMetadataCache()
        self._profile_posts_count_cache: dict[str, int] = _ProfileMetadataCache()
        self._profile_post_datetime_cache: dict[str, tuple[str, ...]] = _ProfileMetadataCache()
        self._profile_post_url_cache: dict[str, tuple[str, ...]] = _ProfileMetadataCache()
        self._profile_base_cache: dict[str, VisibleProfile] = _ProfileMetadataCache()
        self._profile_cache_recency: dict[str, None] = {}
        # All screening tabs in one BitBrowser context share this coordinator. The
        # ordinary profile/count/activity stages remain parallel, while Instagram's
        # more sensitive “About this account” request is strictly single-flight and
        # naturally spaced like the established 1+1 workflow.
        self._location_request_coordinator = _LocationRequestCoordinator()
        self.location_request_min_interval_seconds = 1.5
        self.location_request_max_interval_seconds = 3.0
        self.location_load_failure_backoff_seconds = (8.0, 20.0, 45.0)
        self.location_failure_circuit_seconds = 180.0
        # Playwright and raw CDP calls can occasionally ignore cancellation when a
        # Chromium target or its transport is wedged.  These are wall-clock limits:
        # callers are released when the deadline expires even if the underlying
        # coroutine only settles later.  Owned resources published after a timeout
        # are reclaimed by the late-result callback below.
        self.relay_start_timeout_seconds = 10.0
        self.playwright_start_timeout_seconds = 15.0
        self.cdp_attach_timeout_seconds = 35.0
        self.page_create_timeout_seconds = 10.0
        self.cdp_session_timeout_seconds = 5.0
        self.cdp_command_timeout_seconds = 3.0
        self.disconnect_timeout_seconds = 5.0
        self._late_lifecycle_tasks: set[asyncio.Task[Any]] = set()
        # Passive response bodies may outlive their short observation window.
        # Their ownership and admission limit span successive navigations.
        self._profile_capture_tasks: set[asyncio.Task[Any]] = set()
        # Fast balanced mode: this loop only rereads the already-rendered DOM.
        # A 200 ms cadence discovers new rows sooner without generating extra
        # Instagram requests; natural-end and loading checks remain unchanged.
        # Instagram relationship rows are virtualized and arrive asynchronously.
        # A slower overlapping cadence prevents a rendered row from being recycled
        # between DOM reads on ordinary or high-latency connections.
        self.collection_poll_interval_seconds = 0.45
        self.collection_initial_idle_rounds = 10
        self.collection_settled_idle_rounds = 8
        self.collection_loading_grace_seconds = 20.0
        # Locator.evaluate_all has no Playwright timeout option.  Bound the CDP
        # round-trip explicitly so a wedged renderer is returned to the checkpoint
        # recovery layer instead of pinning one collection window forever.
        self.collection_dom_operation_timeout_seconds = 10.0
        self.profile_read_timeout_seconds = 45.0
        self.location_read_timeout_seconds = 35.0
        self._page_stage_abandoned = False
        self._profile_shell_detected = False
        self._validated_recovery_profile: tuple[Any, str] | None = None
        self._page_recovery_targets: dict[str, None] = {}
        self._fresh_page_retry_targets: dict[str, str] = {}
        self._paused_relation_surface: tuple[Any, str, str, Any, int | None] | None = None
        self._pending_recovery_old: tuple[Any, Any, Any] | None = None
        self._pending_recovery_target: str | None = None
        self._last_page_recovery_error: BaseException | None = None

    def invalidate_after_manual_control(self) -> None:
        """Forget page observations after input has been revoked, without browser I/O."""
        self._validated_recovery_profile = None
        self._paused_relation_surface = None
        self._profile_shell_detected = False
        for name in self._PROFILE_CACHE_NAMES:
            cache = getattr(self, name, None)
            if isinstance(cache, dict):
                cache.clear()
        self._profile_cache_recency.clear()

    @staticmethod
    def _safe_relation_diagnostics(value: Any) -> dict[str, Any]:
        """Only bounded counters and known states may enter recovery diagnostics."""
        if not isinstance(value, dict):
            return {}
        counters = {
            "dialog_count", "visible_dialog_count", "relation_title_count", "row_surface_count",
            "scroll_surface_count", "ambiguous_surface_count", "polls", "profile_links",
            "visible_profile_links", "context_links", "candidate_accounts", "recommended_accounts",
            "scroll_candidates", "attempts", "movements", "invalid", "matched_links",
            "card_polls", "cdp_dispatches", "row_missing", "hover_input_failures",
            "row_recycled", "stale_cards", "position_checks", "expected_anchors",
        }
        states = {
            "stage": {"surface_probe", "unsupported_surface_probe", "find_trigger", "reset", "rows", "advance",
                      "find_hover_row", "hover_input", "read_hover_card", "cdp_hover_input", "confirmed", "unavailable", "position_check"},
            "selected_kind": {"main", "dialog", None},
            "continuity_status": {"confirmed", "missing_anchor", "anchor_order_changed", "anchor_position_changed"},
        }
        output: dict[str, Any] = {}
        for key, item in value.items():
            if key in counters and type(item) is int and 0 <= item <= 1_000_000:
                output[key] = item
            elif key in {"height", "client", "top", "visible_height", "viewport_top", "viewport_bottom"} and type(item) in {int, float} and math.isfinite(item) and 0 <= item <= 1_000_000:
                output[key] = item
            elif key in {"source_matches", "valid", "moved", "bottom", "tail_clipped"} and type(item) is bool:
                output[key] = item
            elif key in states and (item is None or isinstance(item, str)) and item in states[key]:
                output[key] = item
        return output

    def _relation_diagnostics(self) -> dict[str, Any]:
        return {key: clean for key, name in (
            ("surface_diagnostics", "last_relation_surface_diagnostics"),
            ("row_diagnostics", "last_relation_row_diagnostics"),
            ("scroll_diagnostics", "last_relation_scroll_diagnostics"),
            ("hover_diagnostics", "last_relation_hover_diagnostics"),
        ) if (clean := self._safe_relation_diagnostics(getattr(self, name, None)))}

    @classmethod
    def _page_recovery_exhausted(cls, cause: BaseException | None = None, target: str | None = None) -> WorkerExecutionError:
        reason = getattr(cause, "code", "")
        cause_details = getattr(cause, "details", {})
        if not isinstance(cause_details, dict):
            cause_details = {}
        if reason == "instagram_page_recovery_exhausted":
            reason = cause_details.get("original_reason", "")
        if not reason and cause is not None:
            reason = cls._page_failure_reason(cause) or ""
        if target is None:
            target = cause_details.get("recovery_target")
        retry_after = cause_details.get("retry_after_seconds")
        if (
            isinstance(retry_after, bool)
            or not isinstance(retry_after, (int, float))
            or not math.isfinite(retry_after)
            or retry_after < 0
        ):
            retry_after = None
        error = WorkerExecutionError(
            "新页面暂未恢复，已保留旧页和采集进度" +
            (f"；原始原因：{reason}" if reason else ""),
            reason="instagram_page_recovery_exhausted", pause_required=True, status_code=409,
            retry_after_seconds=retry_after,
        )
        # This worker performs one replacement per admitted attempt. Only the
        # owning manager may schedule the next round under its lease and cooldown;
        # keeping the exact cause lets it distinguish transport failure from guards.
        error.details.update(original_reason=reason, auto_retry=False,
                             recovery_target=target, recovery_strategy="new_page_first")
        for key in ("surface_diagnostics", "row_diagnostics", "scroll_diagnostics", "hover_diagnostics"):
            if clean := cls._safe_relation_diagnostics(cause_details.get(key)):
                error.details[key] = clean
        return error

    async def _replace_stuck_page_once(self, username_norm: str, cause: BaseException | None = None) -> None:
        if username_norm in self._page_recovery_targets:
            raise self._page_recovery_exhausted(cause, username_norm)
        self._page_recovery_targets[username_norm] = None
        while len(self._page_recovery_targets) > 256:
            self._page_recovery_targets.pop(next(iter(self._page_recovery_targets)))
        self._last_page_recovery_error = None
        if not await self._recover_stalled_profile_page(username_norm):
            raise self._page_recovery_exhausted(self._last_page_recovery_error or cause, username_norm)
        self._pending_recovery_target = username_norm

    def prepare_page_retry(self, target: str | None) -> None:
        # Called after an explicit retry or a permitted cooldown, under the same lease.
        if isinstance(target, str) and not self._page_stage_abandoned:
            self._page_recovery_targets.pop(target, None)
            held = self._deferred_screening_workers.get(target)
            if held is not None:
                held.prepare_page_retry(target)

    def request_page_replacement(self, target: str, reason: str = "instagram_profile_not_ready") -> bool:
        """Schedule one fresh source page after the manager permits a retry.

        The collection coroutine still owns all page mutation. No tab is opened by
        the UI, no other account lease is acquired, and a poisoned worker must first
        reconnect instead of publishing a new page beside a lingering operation.
        """
        if self._page_stage_abandoned:
            return False
        username, _ = normalize_instagram_username(target)
        self.prepare_page_retry(username)
        self._fresh_page_retry_targets[username] = reason
        while len(self._fresh_page_retry_targets) > 256:
            self._fresh_page_retry_targets.pop(next(iter(self._fresh_page_retry_targets)))
        return True

    async def _finish_page_recovery(self, *, progressed: bool) -> None:
        target = self._pending_recovery_target
        self._pending_recovery_target = None
        if progressed and target is not None:
            self._page_recovery_targets.pop(target, None)
        previous = self._pending_recovery_old
        if previous is None:
            return
        self._pending_recovery_old = None
        old_page, old_session, old_owned_page = previous
        if progressed:
            # The operator explicitly requested closing the old task tab only after
            # its replacement has made progress, including an originally manual tab.
            await self._dispose_page_resources(old_page, old_session, close_page=True)
        else:
            failed_page, failed_session = self.page, self._cdp_session
            self.page, self._cdp_session, self._worker_owned_page = old_page, old_session, old_owned_page
            self._validated_recovery_profile = None
            await self._dispose_page_resources(failed_page, failed_session, close_page=True)
            await self._label_task_page_best_effort(getattr(self, "_task_page_role", "task"))

    async def _await_page_stage(self, operation: Awaitable[Any], *, timeout: float) -> Any:
        """Bound a DOM stage, including CDP calls without native timeouts.

        Never reuse this worker if a timed-out coroutine refuses cancellation:
        a late old read must not navigate or mutate a replacement tab.
        """
        if self._page_stage_abandoned:
            if inspect.iscoroutine(operation):
                operation.close()
            raise WorkerExecutionError(
                "页面读取未能停止，等待重新连接并恢复检查点",
                reason="worker_not_connected", pause_required=True, status_code=503,
            )
        task = asyncio.ensure_future(operation)
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0.001, timeout))
        except asyncio.CancelledError:
            task.cancel()
            self._page_stage_abandoned = True
            self._track_late_lifecycle_task(task)
            raise
        if task in done:
            return task.result()
        task.cancel()
        try:
            done, _ = await asyncio.wait({task}, timeout=0.25)
        except asyncio.CancelledError:
            self._page_stage_abandoned = True
            raise
        finally:
            self._track_late_lifecycle_task(task)
        if task not in done:
            self._page_stage_abandoned = True
        raise WorkerExecutionError(
            "页面读取超时，已保留当前账号等待恢复",
            reason="worker_not_connected" if self._page_stage_abandoned else "browser_window_surface_unstable",
            pause_required=True, status_code=503,
        )

    async def _await_page_probe(self, operation: Awaitable[Any], *, timeout: float) -> Any:
        """Keep normal probe fallbacks, but never reuse an abandoned transport.

        These preflight probes previously used wait_for/native DOM timeouts.
        A cooperative timeout can retry or use the visible route fallback; a
        transport that ignores cancellation must escape to checkpoint recovery.
        """
        try:
            return await self._await_page_stage(operation, timeout=timeout)
        except WorkerExecutionError as exc:
            if not self._page_stage_abandoned and exc.code == "browser_window_surface_unstable":
                raise TimeoutError("Instagram page probe timed out") from exc
            raise

    def _track_late_lifecycle_task(
        self,
        task: asyncio.Task[Any],
        *,
        late_result_cleanup: Callable[[Any], Awaitable[Any]] | None = None,
        owns_resource: bool = False,
    ) -> None:
        """Consume an abandoned task and reclaim any resource it returns later.

        A hard deadline deliberately does not wait for cancellation acknowledgement.
        Keeping a strong reference also prevents ``Task exception was never
        retrieved`` warnings while a cancellation-hostile Playwright transport
        finishes in the background.
        """

        self._late_lifecycle_tasks.add(task)
        if owns_resource or late_result_cleanup is not None:
            self._late_resource_tasks.add(task)
        loop = asyncio.get_running_loop()

        def settled(completed: asyncio.Task[Any]) -> None:
            self._late_lifecycle_tasks.discard(completed)
            self._late_resource_tasks.discard(completed)
            try:
                result = completed.result()
            except BaseException:
                return
            if late_result_cleanup is None or loop.is_closed():
                return

            async def reclaim() -> None:
                try:
                    await self._await_lifecycle_operation(
                        late_result_cleanup(result),
                        timeout=self.disconnect_timeout_seconds,
                    )
                except BaseException:
                    pass

            try:
                cleanup_task = loop.create_task(reclaim())
            except RuntimeError:
                return
            self._track_late_lifecycle_task(cleanup_task, owns_resource=True)

        task.add_done_callback(settled)

    async def _await_lifecycle_operation(
        self,
        awaitable: Awaitable[Any],
        *,
        timeout: float,
        late_result_cleanup: Callable[[Any], Awaitable[Any]] | None = None,
        cancel_on_abandon: bool = True,
    ) -> Any:
        """Await an operation to a real wall-clock deadline.

        ``asyncio.wait_for`` waits for a cancelled coroutine to acknowledge
        cancellation and can therefore hang forever.  ``asyncio.wait`` lets this
        worker detach at the deadline.  For creators such as ``new_page`` and
        ``new_cdp_session`` cancellation is intentionally optional so a resource
        published late can still be closed/detached deterministically.
        """

        task = asyncio.ensure_future(awaitable)
        try:
            done, _pending = await asyncio.wait(
                {task},
                timeout=max(0.001, float(timeout)),
                return_when=asyncio.ALL_COMPLETED,
            )
        except asyncio.CancelledError:
            if cancel_on_abandon:
                task.cancel()
            self._track_late_lifecycle_task(
                task,
                late_result_cleanup=late_result_cleanup,
            )
            raise
        if task not in done:
            if cancel_on_abandon:
                task.cancel()
            self._track_late_lifecycle_task(
                task,
                late_result_cleanup=late_result_cleanup,
            )
            raise TimeoutError(
                f"Browser lifecycle operation exceeded {float(timeout):.3f}s"
            )
        return task.result()

    async def _label_task_page_best_effort(self, role: str, *, cdp_session=None) -> None:
        """Keep optional desktop tab labels outside the collection critical path."""

        try:
            await self._await_lifecycle_operation(
                label_task_page(self, role, cdp_session=cdp_session),
                timeout=self.cdp_command_timeout_seconds,
            )
        except Exception:
            # A stalled metadata CDP request must not block a usable page.  Task
            # cancellation still propagates to the caller's owned-page cleanup.
            pass

    async def _detach_late_cdp_session(self, session: Any) -> None:
        detach = getattr(session, "detach", None)
        if callable(detach):
            await detach()

    async def _close_late_owned_page(self, page: Any) -> None:
        await self._dispose_page_resources(page, None, close_page=True)

    async def _stop_late_playwright(self, playwright: Any) -> None:
        stop = getattr(playwright, "stop", None)
        if callable(stop):
            await stop()

    async def connect(self, profile_id: str, *, open_if_needed: bool = True) -> None:
        if (self._late_lifecycle_tasks or self._screening_slots_opening or self._retired_pages
                or self._page_factories
                or (self._disconnect_task is not None
                    and (not self._disconnect_task.done() or self._worker_owned_page is not None))
                or any(child._disconnect_task is not None
                       for child in self._screening_worker_pool.values())):
            # Include child retirement and queued late-resource callbacks. A done
            # native creator can still publish its cleanup on the next loop turn.
            raise WorkerExecutionError(
                "旧页面操作尚未退出，已保留窗口占用；退出后自动继续",
                reason="browser_operations_pending", pause_required=True,
            )
        if self._page_stage_abandoned:
            # All old operations have settled and disconnect detached the old
            # resources. Only now may this worker acquire a fresh generation.
            self._page_stage_abandoned = False
        begin_attempt = getattr(
            self.bitbrowser, "begin_connection_attempt", None
        )
        attempt_id = (
            begin_attempt(profile_id) if callable(begin_attempt) else None
        )
        self._connecting_attempt_id = attempt_id
        try:
            await self._connect_impl(
                profile_id,
                open_if_needed=open_if_needed,
                attempt_id=attempt_id,
            )
            if attempt_id is not None:
                commit_attempt = getattr(
                    self.bitbrowser, "commit_connection_attempt", None
                )
                if not callable(commit_attempt):
                    raise UpstreamUnavailableError(
                        "BitBrowser connection ticket cannot be committed",
                        details={
                            "profile_id": profile_id,
                            "reason": "connection_attempt_commit_missing",
                            "pause_required": True,
                        },
                    )
                committed = commit_attempt(attempt_id)
                committed_generation = committed.get("generation")
                if isinstance(committed_generation, int):
                    self._connection_generation = committed_generation
            self._connecting_attempt_id = None
        except BaseException:
            if attempt_id is not None:
                cancel_attempt = getattr(
                    self.bitbrowser, "cancel_connection_attempt", None
                )
                if callable(cancel_attempt):
                    cancel_attempt(attempt_id)
            self._connecting_attempt_id = None
            try:
                await asyncio.shield(self.disconnect())
            except BaseException:
                pass
            raise

    async def _connect_impl(
        self,
        profile_id: str,
        *,
        open_if_needed: bool,
        attempt_id: str | None,
    ) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise WorkerExecutionError(
                "Playwright is not installed in the local core",
                reason="playwright_not_installed",
                pause_required=True,
                status_code=503,
            ) from exc
        endpoint_kwargs: dict[str, Any] = {
            "open_if_needed": open_if_needed,
        }
        if attempt_id is not None:
            endpoint_kwargs["attempt_id"] = attempt_id
        endpoints = await asyncio.to_thread(
            self.bitbrowser.connection_endpoint,
            profile_id,
            **endpoint_kwargs,
        )
        endpoint = endpoints.get("ws") or endpoints.get("http")
        endpoint_generation = endpoints.get("generation")
        if not endpoint:
            raise UpstreamUnavailableError(
                "BitBrowser did not provide a CDP endpoint",
                details={"profile_id": profile_id, "pause_required": True},
            )
        resolve_cdp_endpoint = getattr(
            self.bitbrowser, "resolve_cdp_endpoint", None
        )
        if callable(resolve_cdp_endpoint):
            endpoint = await asyncio.to_thread(resolve_cdp_endpoint, endpoint)
        playwright_endpoint = endpoint
        create_cdp_relay = getattr(self.bitbrowser, "create_cdp_relay", None)
        if callable(create_cdp_relay):
            relay: Any = None
            relay_start_task: asyncio.Task[Any] | None = None
            try:
                relay = create_cdp_relay(endpoint)
                # Publish ownership before crossing the to_thread cancellation
                # boundary.  If the caller cancels while start() is still running,
                # disconnect() can now see the relay; this block also waits for
                # the start thread and stops it once more after it returns.
                self._cdp_relay = relay
                relay_start_task = asyncio.create_task(
                    asyncio.to_thread(relay.start)
                )
                playwright_endpoint = await self._await_lifecycle_operation(
                    relay_start_task,
                    timeout=self.relay_start_timeout_seconds,
                    # The relay object is already owned even if its start thread
                    # publishes the endpoint late.  Do not cancel that thread-backed
                    # task: stop the relay once more after start eventually returns.
                    late_result_cleanup=lambda _result: asyncio.to_thread(relay.stop),
                    cancel_on_abandon=False,
                )
            except BaseException as exc:
                if relay is not None:
                    try:
                        await self._await_lifecycle_operation(
                            asyncio.to_thread(relay.stop),
                            timeout=self.disconnect_timeout_seconds,
                        )
                    except BaseException:
                        pass
                if self._cdp_relay is relay:
                    self._cdp_relay = None
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise UpstreamUnavailableError(
                    "Could not start the protected BitBrowser CDP relay",
                    details={
                        "profile_id": profile_id,
                        "reason": "cdp_relay_start_failed",
                        "pause_required": True,
                    },
                ) from exc
        playwright_manager = async_playwright()
        playwright_start_task = asyncio.create_task(playwright_manager.start())
        try:
            # Shield preserves the late owner for the bounded cleanup block below;
            # wait_for applies to the shield wrapper and therefore returns at the
            # wall-clock deadline even if manager.start() ignores cancellation.
            self._playwright = await asyncio.wait_for(
                asyncio.shield(playwright_start_task),
                timeout=self.playwright_start_timeout_seconds,
            )
        except BaseException as exc:
            # Playwright start owns a driver subprocess/transport before its
            # coroutine publishes the Playwright object.  Cancellation at that
            # boundary must wait for start() to settle and stop the late owner;
            # otherwise no later disconnect() can see or reclaim it.
            cleanup_cancellation: asyncio.CancelledError | None = None

            async def bounded_result(
                task: asyncio.Task[Any], *, cancel_on_timeout: bool = True
            ) -> tuple[bool, Any]:
                nonlocal cleanup_cancellation
                deadline = asyncio.get_running_loop().time() + max(
                    0.05, self.disconnect_timeout_seconds
                )
                while not task.done():
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(task), timeout=remaining
                        )
                    except asyncio.CancelledError as cleanup_exc:
                        # shield() also raises CancelledError when the child task
                        # itself was cancelled.  Only an outer caller cancellation
                        # belongs to this worker; our own timeout cancellation must
                        # remain an upstream-start failure.
                        if not task.cancelled() and cleanup_cancellation is None:
                            cleanup_cancellation = cleanup_exc
                        continue
                    except Exception:
                        break
                if not task.done() and cancel_on_timeout:
                    task.cancel()
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(task), timeout=0.1
                        )
                    except asyncio.CancelledError as cleanup_exc:
                        if not task.cancelled() and cleanup_cancellation is None:
                            cleanup_cancellation = cleanup_exc
                    except BaseException:
                        pass
                if not task.done():
                    return False, None
                try:
                    return True, task.result()
                except BaseException:
                    return True, None

            settled, late_playwright = await bounded_result(
                playwright_start_task
            )

            async def stop_playwright(owner: Any) -> None:
                stop_task = asyncio.create_task(owner.stop())
                await bounded_result(stop_task)

            if not settled:
                # A cancellation-hostile manager may still publish an owner later.
                # Keep a callback attached so that late publication is stopped even
                # after this connect() returns to the execution manager.
                self._track_late_lifecycle_task(
                    playwright_start_task,
                    late_result_cleanup=self._stop_late_playwright,
                )
            if late_playwright is not None:
                await stop_playwright(late_playwright)
            if late_playwright is None:
                manager_exit = getattr(playwright_manager, "__aexit__", None)
                if callable(manager_exit):
                    exit_task = asyncio.create_task(manager_exit(None, None, None))
                    await bounded_result(exit_task)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if cleanup_cancellation is not None:
                raise cleanup_cancellation
            raise UpstreamUnavailableError(
                "Could not start the local Playwright driver",
                details={
                    "profile_id": profile_id,
                    "reason": "playwright_start_failed",
                    "pause_required": True,
                },
            ) from exc
        try:
            self._browser = await self._await_lifecycle_operation(
                self._playwright.chromium.connect_over_cdp(
                    playwright_endpoint,
                    timeout=30_000,
                ),
                timeout=self.cdp_attach_timeout_seconds,
            )
        except Exception as exc:
            relay_failure = getattr(self._cdp_relay, "failure", None)
            await self.disconnect()
            raise UpstreamUnavailableError(
                "Could not attach Playwright to the BitBrowser window",
                details={
                    "profile_id": profile_id,
                    "reason": "cdp_attach_failed",
                    "relay_failure": relay_failure,
                    "pause_required": True,
                },
            ) from exc
        verify_endpoint = getattr(self.bitbrowser, "verify_connection_endpoint", None)
        if callable(verify_endpoint):
            try:
                verify_args: tuple[Any, ...] = (
                    profile_id,
                    endpoint,
                    endpoint_generation,
                )
                if attempt_id is not None:
                    verify_args = (*verify_args, attempt_id)
                await asyncio.to_thread(verify_endpoint, *verify_args)
            except DomainError:
                await self.disconnect()
                raise
            except Exception as exc:
                await self.disconnect()
                raise UpstreamUnavailableError(
                    "Could not verify the BitBrowser window after CDP attachment",
                    details={
                        "profile_id": profile_id,
                        "reason": "cdp_profile_verification_failed",
                        "pause_required": True,
                    },
                ) from exc
        if not self._browser.contexts:
            await self.disconnect()
            raise WorkerExecutionError(
                "BitBrowser window has no reusable browser context",
                reason="browser_context_missing",
                status_code=503,
            )
        self._context = self._browser.contexts[0]
        pages = self._context.pages

        def page_is_open(candidate: Any) -> bool:
            try:
                is_closed = getattr(candidate, "is_closed", None)
                return not bool(is_closed()) if callable(is_closed) else True
            except Exception:
                return False

        open_pages = [candidate for candidate in pages if page_is_open(candidate)]
        self.page = next(
            (
                candidate
                for candidate in reversed(open_pages)
                if "instagram.com" in str(getattr(candidate, "url", "")).casefold()
            ),
            open_pages[-1] if open_pages else None,
        )
        if self.page is None:
            try:
                self.page = await self._await_lifecycle_operation(
                    self._context.new_page(),
                    timeout=self.page_create_timeout_seconds,
                    late_result_cleanup=self._close_late_owned_page,
                    cancel_on_abandon=False,
                )
            except TimeoutError as exc:
                raise UpstreamUnavailableError(
                    "BitBrowser did not create an automation tab before the deadline",
                    details={
                        "profile_id": profile_id,
                        "reason": "browser_page_create_timeout",
                        "pause_required": True,
                    },
                ) from exc
            self._worker_owned_page = self.page
        # A successful CDP attachment is the connection check. Do not classify an old
        # tab's login/challenge/business page as the next target's state here; every
        # operation navigates and validates its own requested surface.
        # Never bring the native BitBrowser window to the foreground.  The operator
        # may keep collection windows minimized while Playwright continues to drive
        # and capture their Chromium targets through CDP.
        #
        # Ask Chromium to keep the target lifecycle active so background timer and
        # rendering throttling do not stall Instagram. Unsupported CDP methods are
        # harmless; normal navigation/readiness checks remain authoritative.
        self._cdp_session = await self._new_active_page_session(self.page)
        # A completed reconnect represents an explicit fresh attempt for this
        # BitBrowser session. Drop any circuit opened by the previous attachment;
        # children created below will borrow this new shared coordinator.
        self._location_request_coordinator = _LocationRequestCoordinator()
        self.profile_id = profile_id
        await self._label_task_page_best_effort("task")
        self._connected_endpoint = endpoint
        self._connection_generation = (
            int(endpoint_generation)
            if isinstance(endpoint_generation, int)
            else None
        )

    async def connection_healthy(self) -> bool:
        """Probe the existing local page, without navigating or touching the WAN."""
        try:
            if self._page_stage_abandoned or self._browser is None or not self._browser.is_connected():
                return False
            if self.page is None or self.page.is_closed():
                return False
            return await self._await_lifecycle_operation(
                self.page.evaluate("() => 1"), timeout=2.0
            ) == 1
        except Exception:
            return False

    async def _new_active_page_session(self, page: Any) -> Any:
        """Create a best-effort CDP session that keeps one background tab active."""

        try:
            new_session = getattr(self._context, "new_cdp_session", None)
            if not callable(new_session):
                return None
            session = await self._await_lifecycle_operation(
                new_session(page),
                timeout=self.cdp_session_timeout_seconds,
                late_result_cleanup=self._detach_late_cdp_session,
                cancel_on_abandon=False,
            )
            for method, params in (
                ("Page.setWebLifecycleState", {"state": "active"}),
                ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ):
                try:
                    await self._await_lifecycle_operation(
                        session.send(method, params),
                        timeout=self.cdp_command_timeout_seconds,
                    )
                except TimeoutError:
                    # A timed-out raw command indicates a poisoned session.  Detach
                    # it once and continue without the optional background hints;
                    # navigation and DOM validation remain authoritative.
                    await self._dispose_page_resources(
                        None,
                        session,
                        close_page=False,
                    )
                    return None
                except Exception:
                    pass
            return session
        except Exception:
            # Page navigation and rendered-surface validation remain authoritative on
            # Chromium builds that do not expose one of these optional CDP methods.
            return None

    async def open_account_home_page(self) -> Any:
        """Start on a fresh homepage tab in this account's existing context."""
        if self._context is None:
            raise WorkerExecutionError(
                "账号窗口尚未连接，无法新建账号首页标签页",
                reason="browser_disconnected",
            )
        context, source_page, epoch = self._context, self.page, self._screening_epoch

        def assert_owner() -> None:
            if epoch != self._screening_epoch or context is not self._context:
                raise WorkerExecutionError('Account page owner disconnected', reason='worker_not_connected')

        async def reclaim_late(page: Any) -> None:
            if page is not source_page:
                await self._close_late_owned_page(page)

        candidate = candidate_session = None
        activated = False
        factory = object()
        self._page_factories.add(factory)
        try:
            await self._drain_retired_pages_before_replacement()
            assert_owner()
            candidate = await self._await_lifecycle_operation(
                context.new_page(),
                timeout=self.page_create_timeout_seconds,
                late_result_cleanup=reclaim_late,
                cancel_on_abandon=False,
            )
            if candidate is source_page:
                candidate = None
                raise WorkerExecutionError('浏览器未创建独立账号首页', reason='account_page_not_isolated')
            assert_owner()
            candidate_session = await self._new_active_page_session(candidate)
            # Label the newly owned target before navigation/layout; never the
            # operator's existing tab. Other task viewport policies stay intact.
            if candidate_session is not None:
                await self._label_task_page_best_effort("task", cdp_session=candidate_session)
            assert_owner()
            await self._await_lifecycle_operation(
                candidate.goto(
                    "https://www.instagram.com/",
                    wait_until="domcontentloaded",
                    timeout=45000,
                ),
                timeout=50.0,
            )
            assert_owner()
            old_owned_page, old_session = self._worker_owned_page, self._cdp_session
            self.page, self._cdp_session = candidate, candidate_session
            self._worker_owned_page = candidate
            activated = True
            # Repeated starts must not accumulate owned tabs. Never close or
            # navigate an operator's existing tab or unfinished composer.
            await self._dispose_page_resources(old_owned_page, old_session, close_page=True)
            await self._label_task_page_best_effort("task")
            assert_owner()
            return candidate
        finally:
            try:
                if not activated and candidate is not None:
                    await self._dispose_page_resources(candidate, candidate_session, close_page=True)
            finally:
                self._page_factories.discard(factory)

    async def create_parallel_screening_worker(self) -> "PlaywrightWorker":
        """Create one isolated screening tab without acquiring shared owners.

        The returned worker borrows this worker's BrowserContext and BitBrowser
        connection identity, but owns only the newly created page and its optional
        page-scoped CDP session.  It deliberately has no Playwright driver, Browser,
        relay or operator-page ownership, so ``child.disconnect()`` cannot stop or
        close any parent resource.
        """

        context, source_page, profile_id = self._context, self.page, self.profile_id
        epoch = self._screening_epoch

        def assert_parent() -> None:
            if (context is None or source_page is None or profile_id is None
                    or self._context is not context or self.profile_id != profile_id
                    or self.page is None or self._screening_epoch != epoch
                    or self._page_stage_abandoned
                    or (self._disconnect_task is not None and not self._disconnect_task.done())):
                raise WorkerExecutionError(
                    "Cannot create a screening tab from a disconnected browser worker",
                    reason="worker_not_connected", pause_required=True, status_code=503,
                )

        assert_parent()
        # Fixed 1-1 / 1-2 / 1-3 slots belong to this account worker. Idle pages
        # survive candidates and source handoffs until the parent disconnects.
        for slot, child in sorted(self._screening_worker_pool.items()):
            if child._disconnect_task is not None and not child._disconnect_task.done():
                continue  # Native close (including late cleanup) still owns this slot.
            if child._late_lifecycle_tasks:
                # A finished consumer can leave a timed-out optional CDP/label
                # operation behind. Merely skipping its idle slot forever gives
                # the next source no way to recover: the parent page is healthy,
                # and the late call may settle only when this owned page closes.
                # Retire only a released child. Its slot stays reserved until
                # native close AND every late operation have actually settled.
                if slot not in self._screening_slots_in_use:
                    # Fence concurrent factories before finish_owned schedules
                    # disconnect. A just-settled late call must not make this
                    # child reusable in that scheduling gap.
                    self._screening_slots_in_use.add(slot)
                    await finish_owned(child.disconnect())
                    assert_parent()
                continue
            if child._disconnect_task is not None:
                # A previous close may have failed before native destruction.
                # Retry that cleanup even if its old consumer reserved the slot.
                await finish_owned(child.disconnect())
                assert_parent()
                continue
            if slot not in self._screening_slots_in_use:
                page=child.page
                closed=page is None or (callable(getattr(page,'is_closed',None)) and page.is_closed())
                if closed or child._page_stage_abandoned:
                    await finish_owned(child.disconnect())
                    assert_parent()
                    continue
                self._screening_slots_in_use.add(slot)
                return child
        slot = next((value for value in (1, 2, 3)
                     if value not in self._screening_worker_pool
                     and value not in self._screening_slots_opening), None)
        if slot is None:
            raise WorkerExecutionError("固定筛选子页面已占用，等待当前账号处理完成", reason="screening_slots_busy", pause_required=True)
        new_page = getattr(context, "new_page", None)
        if not callable(new_page):
            raise WorkerExecutionError(
                "The connected browser context cannot create a screening tab",
                reason="browser_context_missing",
                pause_required=True,
                status_code=503,
            )

        # Reserve before the first await; concurrent calls and timed-out native
        # creators must count toward the same three physical page slots.
        reservation = object()
        self._screening_slots_opening[slot] = reservation
        abandoned_creation = False

        def release_reservation() -> None:
            if self._screening_slots_opening.get(slot) is reservation:
                self._screening_slots_opening.pop(slot, None)

        async def reclaim_late_page(page: Any) -> None:
            try:
                if page is not source_page:
                    # Late pages get the same retryable close owner as ordinary
                    # children. A native close error must not orphan this page.
                    orphan = PlaywrightWorker(self.bitbrowser)
                    orphan.page = orphan._worker_owned_page = page
                    orphan._task_page_slot = slot
                    orphan._screening_pool_parent = self
                    orphan.disconnect_timeout_seconds = self.disconnect_timeout_seconds
                    if self._screening_slots_opening.get(slot) is reservation:
                        self._screening_worker_pool[slot] = orphan
                        self._screening_slots_in_use.add(slot)
                    await orphan.disconnect()
            finally:
                release_reservation()

        def release_failed_creation(task: asyncio.Task[Any]) -> None:
            if task.cancelled() or task.exception() is not None:
                release_reservation()

        candidate_page: Any = None
        child: PlaywrightWorker | None = None
        try:
            creation = asyncio.ensure_future(new_page())
            try:
                candidate_page = await self._await_lifecycle_operation(
                    creation,
                    timeout=self.page_create_timeout_seconds,
                    late_result_cleanup=reclaim_late_page,
                    cancel_on_abandon=False,
                )
            except asyncio.CancelledError:
                abandoned_creation = True
                creation.add_done_callback(release_failed_creation)
                raise
            except TimeoutError as exc:
                abandoned_creation = True
                creation.add_done_callback(release_failed_creation)
                raise WorkerExecutionError(
                    "BitBrowser did not create the parallel screening tab before the deadline",
                    reason="parallel_screening_page_create_timeout",
                    pause_required=True,
                    status_code=503,
                ) from exc
            except Exception as exc:
                self._raise_page_failure(
                    exc, operation="create parallel Instagram screening tab"
                )
                raise WorkerExecutionError(
                    "BitBrowser could not create a parallel screening tab",
                    reason="parallel_screening_page_create_failed",
                    pause_required=True,
                    status_code=503,
                ) from exc

            if candidate_page is source_page:
                # ``BrowserContext.new_page`` must return a distinct page.  Treat a
                # broken adapter/test shim defensively: never hand the operator page
                # to a child which owns and closes its page during cleanup.
                candidate_page = None
                raise WorkerExecutionError(
                    "BitBrowser did not provide an isolated screening tab",
                    reason="parallel_screening_page_not_isolated",
                    pause_required=True,
                    status_code=503,
                )

            assert_parent()
            child = PlaywrightWorker(
                self.bitbrowser,
                selectors=replace(self.selectors),
            )
            # Copy only non-owning connection references and immutable identity.
            # In particular, _playwright/_browser/_cdp_relay remain None.
            child._context = context
            child.page = candidate_page
            child._worker_owned_page = candidate_page
            child.profile_id = profile_id
            child._connected_endpoint = self._connected_endpoint
            child._task_page_slot = slot
            child._screening_pool_parent = self
            child._connection_generation = self._connection_generation
            child._location_request_coordinator = self._location_request_coordinator
            for setting in (
                "page_create_timeout_seconds",
                "cdp_session_timeout_seconds",
                "cdp_command_timeout_seconds",
                "disconnect_timeout_seconds",
                "location_request_min_interval_seconds",
                "location_request_max_interval_seconds",
                "location_load_failure_backoff_seconds",
                "location_failure_circuit_seconds",
            ):
                setattr(child, setting, getattr(self, setting))
            child._cdp_session = await child._new_active_page_session(candidate_page)
            assert_parent()
            await self._label_task_page_best_effort("source")
            await child._label_task_page_best_effort("screening")
            assert_parent()
            self._screening_worker_pool[slot] = child
            self._screening_slots_in_use.add(slot)
            return child
        except BaseException:
            try:
                if child is not None:
                    # A failed factory never publishes this child to its caller.
                    # Keep ownership until cleanup settles even if Stop interrupts
                    # the error path; shield alone leaves an untracked closing tab.
                    if (self._screening_slots_opening.get(slot) is reservation
                            and slot not in self._screening_worker_pool):
                        self._screening_worker_pool[slot] = child
                        self._screening_slots_in_use.add(slot)
                    await finish_owned(child.disconnect())
                elif candidate_page is not None:
                    await self._dispose_page_resources(
                        candidate_page,
                        None,
                        close_page=True,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Preserve the original construction failure after best-effort
                # resource cleanup. Cancellation remains authoritative above.
                pass
            raise
        finally:
            if not abandoned_creation:
                release_reservation()

    def release_parallel_screening_worker(self, child: Any) -> bool:
        """Release a slot to the same parent only, without closing its page."""
        for slot, owned in self._screening_worker_pool.items():
            if owned is child:
                self._screening_slots_in_use.discard(slot)
                return True
        return False

    async def disconnect(self) -> None:
        if self._disconnect_task is None or self._disconnect_task.done():
            # Fence factories before scheduling any asynchronous cleanup.
            self._screening_epoch += 1
            self._disconnect_task = asyncio.create_task(self._disconnect_owned())
        await finish_owned(self._disconnect_task)

    async def wait_for_cleanup(self) -> None:
        """Join actual cleanup before a scheduler releases this window's lease."""
        async def drain() -> None:
            delay = .05
            while True:
                try:
                    await self.disconnect()
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                except Exception:
                    # A transient close failure must keep its original owner.
                    await asyncio.sleep(delay)
                    delay = min(2.0, delay * 2)
                    continue
                if not (self._late_lifecycle_tasks or self._screening_worker_pool
                        or self._screening_slots_opening or self._page_factories
                        or self._retired_pages or self._worker_owned_page is not None):
                    return
                await asyncio.sleep(delay)
                delay = min(2.0, delay * 2)
        await finish_owned(drain())

    def _retire_screening_slot(self) -> None:
        parent = getattr(self, '_screening_pool_parent', None)
        if parent is None:
            return
        slot = self._task_page_slot

        def release() -> None:
            if self._worker_owned_page is not None or self._retired_pages:
                return  # A failed native close remains owned and retryable.
            if parent._screening_worker_pool.get(slot) is self:
                parent._screening_worker_pool.pop(slot, None)
                parent._screening_slots_in_use.discard(slot)
            self._screening_pool_parent = None

        if not self._late_lifecycle_tasks:
            release()
            return
        if self._screening_retirement_task is not None and not self._screening_retirement_task.done():
            return

        async def retire() -> None:
            # Completion callbacks may themselves publish late cleanup tasks.
            # Drain the ownership set, not just its first snapshot.
            while self._late_lifecycle_tasks:
                await asyncio.wait(tuple(self._late_lifecycle_tasks))
                await asyncio.sleep(0)
            release()

        self._screening_retirement_task = asyncio.create_task(retire())
        parent._track_late_lifecycle_task(self._screening_retirement_task)

    async def _disconnect_owned(self) -> None:
        cancellation: asyncio.CancelledError | None = None
        held = list({id(child): child for child in [*self._deferred_screening_workers.values(), *self._screening_worker_pool.values()]}.values())
        self._deferred_screening_workers.clear()
        if held:
            await asyncio.gather(*(child.disconnect() for child in held), return_exceptions=True)
        self._fresh_page_retry_targets.clear()
        self._paused_relation_surface = None
        try:
            await self._finish_page_recovery(progressed=False)
        except asyncio.CancelledError as exc:
            cancellation = exc
        # Do not call BitBrowser /browser/close and never close an operator-owned tab.
        # A tab created by this worker may be closed so recovery/reconnect cannot leak
        # a new native tab each time.
        connecting_attempt_id = self._connecting_attempt_id
        self._connecting_attempt_id = None
        if connecting_attempt_id is not None:
            cancel_attempt = getattr(
                self.bitbrowser, "cancel_connection_attempt", None
            )
            if callable(cancel_attempt):
                cancel_attempt(connecting_attempt_id)
        playwright = self._playwright
        cdp_relay = self._cdp_relay
        # Detach all public references before awaiting driver cleanup. A crashed or
        # wedged driver must never remain reachable and be overwritten by reconnect().
        cdp_session = self._cdp_session
        # Screening children borrow the parent's context and notification session.
        notification_session = getattr(self._context, "_juxin_notifications_session", None) if self._browser is not None else None
        if notification_session is not None:
            self._context._juxin_notifications_session = None
        worker_owned_page = self._worker_owned_page
        self._playwright = self._browser = self._context = self._cdp_session = self.page = None
        # Keep an owned page until its close actually succeeds. Public DOM access
        # is already detached above; retaining this handle only permits cleanup.
        self._cdp_relay = None
        self.profile_id = None
        self._connected_endpoint = None
        self._connection_generation = None
        self._profile_privacy_cache.clear()
        self._profile_user_id_cache.clear()
        self._profile_verified_cache.clear()
        self._profile_professional_cache.clear()
        self._profile_category_cache.clear()
        self._profile_external_bio_url_cache.clear()
        self._profile_posts_count_cache.clear()
        self._profile_post_datetime_cache.clear()
        self._profile_post_url_cache.clear()
        self._profile_base_cache.clear()
        self._profile_cache_recency.clear()

        async def finish_cleanup(awaitable: Awaitable[Any]) -> None:
            """Run one cleanup to a hard deadline despite caller cancellation."""
            nonlocal cancellation
            cleanup_task = asyncio.create_task(
                self._await_lifecycle_operation(
                    awaitable,
                    timeout=self.disconnect_timeout_seconds,
                )
            )
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError as exc:
                    if cancellation is None:
                        cancellation = exc
                    continue
                except Exception:
                    break
            if cleanup_task.done():
                try:
                    cleanup_task.result()
                except asyncio.CancelledError as exc:
                    if cancellation is None:
                        cancellation = exc
                except Exception:
                    pass

        if cdp_session is not None:
            await finish_cleanup(cdp_session.detach())
        if notification_session is not None:
            await finish_cleanup(notification_session.detach())
        pages = dict(self._retired_pages)
        if worker_owned_page is not None:
            pages[id(worker_owned_page)] = worker_owned_page
        for page in pages.values():
            try:
                await self._close_page_for_cleanup(page)
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
        def native_pages_pending() -> bool:
            return bool(self._worker_owned_page is not None or self._retired_pages
                        or self._screening_worker_pool or self._screening_slots_opening
                        or self._page_factories or self._late_resource_tasks)

        async def finish_transport() -> None:
            # A detached public worker still needs its original transport to close
            # native pages. Stopping the relay/driver first makes every retry fail.
            # Keep this cleanup tracked so reconnect/manual takeover remain fenced.
            retry_delay = .25
            while native_pages_pending():
                await asyncio.sleep(retry_delay)
                retry_delay = min(2.0, retry_delay * 2)
                for child in tuple(self._screening_worker_pool.values()):
                    try:
                        await child.disconnect()
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise
                    except Exception:
                        pass  # Keep the failed child's ownership for the next retry.
                for page in tuple(self._retired_pages.values()):
                    try:
                        await self._close_page_for_cleanup(page)
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise
            if cdp_relay is not None:
                await finish_cleanup(asyncio.to_thread(cdp_relay.stop))
            if playwright is not None:
                await finish_cleanup(playwright.stop())

        if cdp_relay is not None or playwright is not None:
            if native_pages_pending():
                self._track_late_lifecycle_task(asyncio.create_task(finish_transport()))
            else:
                await finish_transport()
        self._retire_screening_slot()
        if cancellation is not None:
            raise cancellation

    async def __aenter__(self) -> "PlaywrightWorker":
        if self.page is None:
            raise RuntimeError("Call connect() before entering the worker context")
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()

    async def _visible_transport_failure(self, page: Any, body_text: str) -> str | None:
        """Only standalone/system notices are transport evidence, never a bio."""
        try:
            # Chromium can expose its internal error document after Page.goto
            # returns/raises. It is a transport failure, never a removed IG user.
            if urlparse(str(page.url)).scheme.casefold() == "chrome-error":
                return "instagram_network_unavailable"
        except Exception:
            pass

        def classify(text, *, system_text=False):
            folded = " ".join(text.casefold().replace("\u2019", "'").split()).strip(" .!。！")
            for markers, reason in (
                (_PROFILE_HARD_NETWORK_FAILURE_MARKERS, "instagram_network_unavailable"),
                (_PROFILE_SOFT_LOAD_FAILURE_MARKERS, "instagram_profile_not_ready"),
            ):
                for marker in markers:
                    if folded == marker:
                        return reason
                    # Only bounded, visible system nodes may include explanatory
                    # text/buttons after the notice. Never substring-match a bio.
                    if (system_text and folded.startswith(marker)
                            and len(folded) > len(marker)
                            and folded[len(marker)] in " .,:：，。!！·"):
                        return reason
            return None
        standalone = classify(body_text or "")
        if standalone == "instagram_network_unavailable":
            return standalone
        soft_failure = standalone
        try:
            evidence = await self._await_page_stage(
                page.locator("body").evaluate(GUARD_EVIDENCE_SCRIPT), timeout=2.0
            )
            if isinstance(evidence, list):
                for text in evidence:
                    if isinstance(text, str):
                        reason = classify(text, system_text=True)
                        if reason == "instagram_network_unavailable":
                            return reason
                        if reason:
                            soft_failure = reason
        except WorkerExecutionError as exc:
            if exc.code != "browser_window_surface_unstable":
                raise
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect Instagram transport evidence")
        return soft_failure

    async def _classify_page_guard(self, page: Any, url: str, body_text: str) -> str | None:
        route = classify_guard_state(url, "")
        if route:
            return route
        try:
            evidence = await self._await_page_stage(
                page.locator("body").evaluate(GUARD_EVIDENCE_SCRIPT), timeout=2.0,
            )
            if isinstance(evidence, list):
                for text in evidence:
                    if isinstance(text, str):
                        reason = classify_guard_state("", text, system_text=True)
                        if reason:
                            return reason
        except WorkerExecutionError as exc:
            if exc.code != "browser_window_surface_unstable":
                raise
        except Exception as exc:
            # Old/custom DOM adapters still use the conservative standalone fallback.
            self._raise_page_failure(exc, operation="inspect Instagram guard evidence")
        return classify_guard_state(url, body_text)

    async def _guard(self) -> None:
        manual_checkpoint = getattr(self, "manual_control_checkpoint", None)
        if callable(manual_checkpoint):
            await manual_checkpoint()
        if self._page_stage_abandoned:
            raise WorkerExecutionError(
                "旧页面读取未结束，需要重新连接后恢复检查点",
                reason="worker_not_connected", pause_required=True, status_code=503,
            )
        if self.page is None:
            raise WorkerExecutionError("Worker is not connected", reason="worker_not_connected", status_code=503)
        try:
            visible_text = await self._await_page_stage(
                self.page.locator("body").inner_text(timeout=5_000), timeout=5.0,
            )
        except WorkerExecutionError:
            raise
        except Exception as exc:
            self._raise_page_failure(exc, operation="read Instagram page")
            visible_text = ""
        try:
            current_url = str(self.page.url)
        except Exception as exc:
            self._raise_page_failure(exc, operation="read Instagram page URL")
            current_url = ""
        reason = await self._classify_page_guard(self.page, current_url, visible_text[:100_000])
        if reason:
            messages = {
                "instagram_login_required": "Instagram login is required in this BitBrowser window",
                "instagram_challenge": "Instagram requires manual verification in this window",
                "instagram_rate_limited": "Instagram asked this window to wait before continuing",
                "instagram_action_blocked": "Instagram blocked actions in this window",
                "instagram_content_not_visible": "The requested Instagram content is not visible to this window",
            }
            raise WorkerExecutionError(
                messages[reason],
                reason=reason,
                pause_required=reason != "instagram_content_not_visible",
            )

    async def _verify_destructive_action_window(self) -> None:
        """Fence a follow/send immediately before its external side effect."""
        verify_endpoint = getattr(self.bitbrowser, "verify_connection_endpoint", None)
        if not callable(verify_endpoint):
            return
        if not self.profile_id or not self._connected_endpoint:
            raise WorkerExecutionError(
                "BitBrowser window connection is no longer available",
                reason="worker_not_connected",
                status_code=503,
            )
        profile_id = self.profile_id
        endpoint = self._connected_endpoint
        generation = self._connection_generation
        try:
            await asyncio.to_thread(
                verify_endpoint,
                profile_id,
                endpoint,
                generation,
            )
        except DomainError:
            await self.disconnect()
            raise
        except Exception as exc:
            await self.disconnect()
            raise UpstreamUnavailableError(
                "BitBrowser window identity changed before the action",
                details={
                    "profile_id": profile_id,
                    "reason": "cdp_profile_verification_failed",
                    "pause_required": True,
                },
            ) from exc

    @asynccontextmanager
    async def _destructive_action_lease(self) -> Any:
        """Linearize a follow/send with window close or cancellation."""
        begin_action_attempt = getattr(
            self.bitbrowser, "begin_action_attempt", None
        )
        resolve_action_attempt = getattr(
            self.bitbrowser, "resolve_action_attempt", None
        )
        adopt_action_attempt = getattr(
            self.bitbrowser, "adopt_action_attempt", None
        )
        cancel_action_attempt = getattr(
            self.bitbrowser, "cancel_action_attempt", None
        )
        begin_action = getattr(self.bitbrowser, "begin_action", None)
        end_action = getattr(self.bitbrowser, "end_action", None)
        ticket_api = all(
            callable(method)
            for method in (
                begin_action_attempt,
                resolve_action_attempt,
                adopt_action_attempt,
                cancel_action_attempt,
                end_action,
            )
        )
        if not ticket_api and (
            not callable(begin_action) or not callable(end_action)
        ):
            await self._verify_destructive_action_window()
            yield
            return
        if not self.profile_id or not self._connected_endpoint:
            raise WorkerExecutionError(
                "BitBrowser window connection is no longer available",
                reason="worker_not_connected",
                status_code=503,
            )
        action_attempt_id: str | None = None
        try:
            if ticket_api:
                action_attempt_id = begin_action_attempt(
                    self.profile_id,
                    self._connected_endpoint,
                    self._connection_generation,
                )
                await asyncio.to_thread(
                    resolve_action_attempt, action_attempt_id
                )
                lease_id = adopt_action_attempt(action_attempt_id)
                action_attempt_id = None
            else:
                lease_id = await asyncio.to_thread(
                    begin_action,
                    self.profile_id,
                    self._connected_endpoint,
                    self._connection_generation,
                )
        except asyncio.CancelledError:
            if action_attempt_id is not None:
                cancel_action_attempt(action_attempt_id)
            raise
        except DomainError:
            if action_attempt_id is not None:
                cancel_action_attempt(action_attempt_id)
            await self.disconnect()
            raise
        except Exception as exc:
            if action_attempt_id is not None:
                cancel_action_attempt(action_attempt_id)
            await self.disconnect()
            raise UpstreamUnavailableError(
                "Could not acquire the BitBrowser action lease",
                details={
                    "profile_id": self.profile_id,
                    "reason": "action_window_verification_failed",
                    "pause_required": True,
                },
            ) from exc
        try:
            yield
        finally:
            # end_action is a pure in-memory release and must also run when the
            # Playwright action is cancelled or raises after it may have fired.
            end_action(lease_id)

    @staticmethod
    def _page_failure_reason(
        exc: BaseException,
        *,
        navigation_timeout: bool = False,
    ) -> str | None:
        message = f"{type(exc).__name__}: {exc}".casefold()
        if any(marker in message for marker in _PAGE_CONNECTION_ERROR_MARKERS):
            return "worker_not_connected"
        if any(marker in message for marker in _PAGE_NETWORK_ERROR_MARKERS):
            return "instagram_network_unavailable"
        if navigation_timeout and (
            type(exc).__name__.casefold() == "timeouterror"
            or ("timeout" in message and "exceeded" in message)
        ):
            return "instagram_network_unavailable"
        return None

    @classmethod
    def _raise_page_failure(
        cls,
        exc: BaseException,
        *,
        operation: str,
        navigation_timeout: bool = False,
    ) -> None:
        reason = cls._page_failure_reason(
            exc, navigation_timeout=navigation_timeout
        )
        if reason is None:
            return
        raise WorkerExecutionError(
            f"{operation} was interrupted; the task will wait and resume from its checkpoint",
            reason=reason,
            pause_required=True,
            status_code=503,
        ) from exc

    @staticmethod
    def _page_matches_profile(page: Any, username_norm: str) -> bool:
        try:
            parsed = urlparse(str(page.url))
            if (parsed.scheme.casefold() not in {"https", "http"}
                    or (parsed.hostname or "").casefold() not in {
                        "instagram.com", "www.instagram.com", "m.instagram.com",
                    }):
                return False
            parts = [
                part.casefold()
                for part in parsed.path.split("/")
                if part
            ]
        except Exception:
            return False
        return len(parts) == 1 and parts[0] == username_norm

    async def _recovery_page_has_loading_indicator(self, page: Any) -> bool:
        try:
            loading = page.locator(
                '[role="progressbar"], [aria-busy="true"], '
                'svg[aria-label*="loading" i], svg[aria-label*="加载"], '
                'svg[aria-label*="載入"], svg[aria-label*="đang tải" i], '
                'svg[aria-label*="cargando" i], svg[aria-label*="memuat" i]'
            )
            count = min(await loading.count(), 40)
        except Exception:
            return False
        for index in range(count):
            try:
                if await loading.nth(index).is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _validate_recovery_profile_page(
        self,
        page: Any,
        username_norm: str,
        *,
        navigation_error: BaseException | None = None,
    ) -> None:
        """Verify a fresh tab without rebinding the worker to it prematurely.

        A complete metric header is terminal even when an unrelated grid/recommendation
        spinner remains visible. This is important for private and zero-post accounts:
        neither should be mistaken for a dead tab merely because no post tile appears.
        """

        surface_visible = False
        try:
            surface = page.locator("main:visible, header:visible").first
            await surface.wait_for(
                state="visible",
                timeout=_PROFILE_RECOVERY_SURFACE_TIMEOUT_MILLISECONDS,
            )
            surface_visible = True
        except Exception:
            pass

        try:
            body_text = (await page.locator("body").inner_text(timeout=3_000))[
                :100_000
            ]
        except Exception as exc:
            reason = self._page_failure_reason(exc)
            if reason is None and navigation_error is not None:
                reason = self._page_failure_reason(
                    navigation_error,
                    navigation_timeout=True,
                )
            raise WorkerExecutionError(
                "Fresh Instagram recovery tab did not expose a readable document",
                reason=reason or "instagram_profile_not_ready",
                pause_required=True,
                status_code=503,
            ) from exc

        folded = " ".join(body_text.casefold().split())
        try:
            current_url = str(page.url)
        except Exception as exc:
            raise WorkerExecutionError(
                "Fresh Instagram recovery tab URL is unavailable",
                reason=self._page_failure_reason(exc) or "worker_not_connected",
                pause_required=True,
                status_code=503,
            ) from exc

        guard_reason = await self._classify_page_guard(page, current_url, body_text or "")
        if guard_reason:
            raise WorkerExecutionError(
                "Fresh Instagram recovery tab reached a guarded surface",
                reason=guard_reason,
                pause_required=guard_reason != "instagram_content_not_visible",
                status_code=503 if guard_reason != "instagram_content_not_visible" else 409,
            )
        transport_reason = await self._visible_transport_failure(page, body_text or "")
        if transport_reason:
            raise WorkerExecutionError(
                "Fresh Instagram recovery tab reported a load failure",
                reason=transport_reason, pause_required=True, status_code=503,
            )
        if not self._page_matches_profile(page, username_norm):
            raise WorkerExecutionError(
                "Fresh Instagram recovery tab did not remain on the requested profile",
                reason="instagram_content_not_visible",
                pause_required=False,
                status_code=409,
            )

        metrics = extract_visible_metrics(body_text)
        metrics_complete = all(value is not None for value in metrics)
        explicit_private = any(marker in folded for marker in _PRIVATE_PROFILE_MARKERS)
        explicit_public_empty = extract_public_empty_profile_metrics(body_text) is not None
        # A real profile header is usable regardless of a loader elsewhere on the
        # page. Without terminal profile evidence, however, an empty/spinning shell
        # is exactly the black/white-tab failure this recovery path must reject.
        if metrics_complete or explicit_private or explicit_public_empty:
            return
        loading = await self._recovery_page_has_loading_indicator(page)
        if not folded or is_instagram_navigation_shell(body_text) or loading or not surface_visible:
            reason = "instagram_profile_not_ready"
            if navigation_error is not None:
                reason = self._page_failure_reason(
                    navigation_error,
                    navigation_timeout=True,
                ) or reason
            raise WorkerExecutionError(
                "Fresh Instagram recovery tab stayed empty or loading",
                reason=reason,
                pause_required=True,
                status_code=503,
            )

    async def _dispose_page_resources(
        self,
        page: Any,
        cdp_session: Any,
        *,
        close_page: bool,
    ) -> None:
        """Drain owned cleanup before allowing task cancellation to propagate.

        Recovery has already detached these resources from the worker's public
        references. Cancellation between session detach and page close must not
        abandon an old/failed tab that disconnect can no longer find.
        """
        await finish_owned(self._dispose_page_resources_owned(
            page, cdp_session, close_page=close_page,
        ))

    async def _dispose_page_resources_owned(
        self,
        page: Any,
        cdp_session: Any,
        *,
        close_page: bool,
    ) -> None:
        """Keep each individual cleanup bounded even if the browser is wedged."""

        cancellation: asyncio.CancelledError | None = None
        if close_page and page is not None:
            self._retired_pages[id(page)] = page
        if cdp_session is not None:
            detach = getattr(cdp_session, "detach", None)
            if callable(detach):
                try:
                    await self._await_lifecycle_operation(
                        detach(),
                        timeout=self.disconnect_timeout_seconds,
                    )
                except asyncio.CancelledError as exc:
                    cancellation = exc
                except Exception:
                    pass
        if close_page and page is not None:
            await self._close_page_for_cleanup(page)
        if cancellation is not None:
            raise cancellation

    async def _close_page_for_cleanup(self, page: Any) -> None:
        """Retain failed closes and reuse an already-running native close."""
        key = id(page)
        self._retired_pages[key] = page
        task = self._retired_page_close_tasks.get(key)
        if task is None or task.done():
            async def close_and_forget() -> None:
                is_closed = getattr(page, "is_closed", None)
                try:
                    if not callable(is_closed) or not is_closed():
                        await page.close()
                except Exception:
                    if not callable(is_closed) or not is_closed():
                        raise
                if callable(is_closed) and not is_closed():
                    raise RuntimeError("Owned page close has not been confirmed")
                self._retired_pages.pop(key, None)
                if self._worker_owned_page is page:
                    self._worker_owned_page = None

            task = asyncio.create_task(close_and_forget())
            self._retired_page_close_tasks[key] = task
            def settled(completed: asyncio.Task[Any]) -> None:
                if self._retired_page_close_tasks.get(key) is completed:
                    self._retired_page_close_tasks.pop(key, None)
            task.add_done_callback(settled)
        try:
            await self._await_lifecycle_operation(task, timeout=self.disconnect_timeout_seconds)
        except Exception:
            pass  # The page remains discoverable for the next cleanup attempt.

    async def _drain_retired_pages_before_replacement(self) -> None:
        for page in tuple(self._retired_pages.values()):
            await self._close_page_for_cleanup(page)
        if self._retired_pages or self._late_lifecycle_tasks or self._page_stage_abandoned:
            raise WorkerExecutionError(
                "旧页面尚未关闭，保留进度并等待清理后自动继续",
                reason="browser_operations_pending", pause_required=True,
            )

    async def _recover_stalled_profile_page(self, username_norm: str) -> bool:
        """Replace a dead automation tab with one fresh, same-context owned tab.

        The current page and CDP session are left untouched until the fresh page has
        navigated to and rendered the exact requested profile. On failure only the
        temporary page is reclaimed, so the execution manager can keep its existing
        task checkpoint and retry/wait policy.
        """

        context = self._context
        epoch, source_page = self._screening_epoch, self.page
        new_page = getattr(context, "new_page", None) if context is not None else None
        if not callable(new_page):
            return False

        candidate: Any = None
        candidate_session: Any = None
        activated = False
        factory = object()
        self._page_factories.add(factory)
        try:
            await self._drain_retired_pages_before_replacement()
            if epoch != self._screening_epoch or context is not self._context:
                raise WorkerExecutionError('Recovery owner disconnected', reason='worker_not_connected')
            async def reclaim_late(page: Any) -> None:
                if page is not source_page:
                    await self._close_late_owned_page(page)
            candidate = await self._await_lifecycle_operation(
                new_page(),
                timeout=self.page_create_timeout_seconds,
                late_result_cleanup=reclaim_late,
                cancel_on_abandon=False,
            )
            if candidate is source_page:
                candidate = None
                self._last_page_recovery_error = WorkerExecutionError(
                    "浏览器未创建独立新页面", reason="recovery_page_not_distinct", pause_required=True,
                )
                return False
            if epoch != self._screening_epoch or context is not self._context:
                raise WorkerExecutionError('Recovery owner disconnected', reason='worker_not_connected')
            candidate_session = await self._new_active_page_session(candidate)
            navigation_error: BaseException | None = None
            try:
                await self._await_page_stage(candidate.goto(
                    f"https://www.instagram.com/{username_norm}/",
                    wait_until="domcontentloaded",
                    timeout=_PROFILE_RECOVERY_NAVIGATION_TIMEOUT_MILLISECONDS,
                ), timeout=32.0)
            except WorkerExecutionError:
                raise
            except Exception as exc:
                # Chromium sometimes raises a navigation timeout after the useful DOM
                # has committed. Rendered exact-target validation below gets the final
                # say, matching the normal navigation path.
                navigation_error = exc
            await self._await_page_stage(self._validate_recovery_profile_page(
                candidate,
                username_norm,
                navigation_error=navigation_error,
            ), timeout=15.0)

            if epoch != self._screening_epoch or context is not self._context:
                raise WorkerExecutionError('Recovery owner disconnected', reason='worker_not_connected')

            old_page = self.page
            old_session = self._cdp_session
            old_owned_page = self._worker_owned_page
            # Atomic publication: readers never observe the candidate until both its
            # exact URL and rendered profile surface have passed validation.
            self._pending_recovery_old = (old_page, old_session, old_owned_page)
            self.page = candidate
            self._cdp_session = candidate_session
            self._worker_owned_page = candidate
            self._validated_recovery_profile = (candidate, username_norm)
            await self._label_task_page_best_effort(getattr(self, "_task_page_role", "task"))
            if epoch != self._screening_epoch or context is not self._context:
                raise WorkerExecutionError('Recovery owner disconnected', reason='worker_not_connected')
            activated = True
            return True
        except asyncio.CancelledError:
            raise
        except WorkerExecutionError as exc:
            # A fresh same-context tab is authoritative for explicit Instagram
            # login/challenge/rate-limit/action-block/target-not-visible surfaces.
            # Propagate those precise states after cleanup instead of disguising them
            # as a generic temporary outage. Empty/network pages remain a failed
            # replacement and fall back to the existing checkpoint wait policy.
            if exc.code in _PROFILE_RECOVERY_AUTHORITATIVE_REASONS:
                raise
            self._last_page_recovery_error = exc
            return False
        except TimeoutError:
            # Lifecycle deadlines (creating the tab/session) are temporary browser
            # surface failures, not an unknown account state. Late page results are
            # already owned and reclaimed by _await_lifecycle_operation above.
            self._last_page_recovery_error = WorkerExecutionError(
                "新页面准备超时，已保留原页面和采集进度",
                reason="browser_window_surface_unstable", pause_required=True, status_code=503,
            )
            return False
        except Exception as exc:
            self._last_page_recovery_error = exc
            return False
        finally:
            try:
                if not activated and candidate is not None:
                    if self._pending_recovery_old is not None and self.page is candidate:
                        await self._finish_page_recovery(progressed=False)
                    else:
                        await self._dispose_page_resources(candidate, candidate_session, close_page=True)
            finally:
                self._page_factories.discard(factory)

    def parent_reels_adapter(self):
        """Use only this collector parent's existing page and action fence."""
        from .parent_reels import ParentReelsAdapter
        return ParentReelsAdapter(self)

    async def _navigate_profile(self, username: str) -> str:
        username_norm, username_display = normalize_instagram_username(username)
        await self._ensure_window_surface_stable()
        validated = self._validated_recovery_profile
        self._validated_recovery_profile = None
        if validated is not None and validated[0] is self.page and validated[1] == username_norm and self._is_current_profile(username_norm):
            # Recovery already navigated and validated this exact page. Read it
            # directly, without immediately reloading the healthy replacement.
            await self._guard()
            return username_display
        try:
            await self.page.goto(
                f"https://www.instagram.com/{username_norm}/",
                wait_until="domcontentloaded",
                timeout=45_000,
            )
        except Exception as exc:
            # A timeout can occur after the useful DOM was committed, so first allow
            # an explicit login/challenge/rate-limit page to report its precise state.
            # Otherwise this is infrastructure failure, never evidence that the
            # Instagram account itself has unknown counts/privacy.
            try:
                await self._guard()
            except WorkerExecutionError:
                raise
            self._raise_page_failure(
                exc,
                operation="Instagram profile navigation",
                navigation_timeout=True,
            )
            if not self._is_current_profile(username_norm):
                raise WorkerExecutionError(
                    "Instagram did not remain on the requested profile",
                    reason="instagram_content_not_visible",
                    pause_required=False,
                ) from exc
            raise WorkerExecutionError(
                "Instagram profile navigation completed with an unrecognized page state",
                reason="instagram_profile_dom_unrecognized",
                pause_required=False,
            ) from exc
        await self._wait_for_profile_surface()
        await self._guard()
        return username_display

    async def _ensure_window_surface_stable(self) -> None:
        """Keep the Chromium target active without restoring its native window.

        Playwright reads the DOM and captures element/page images through CDP, so a
        minimized BitBrowser window is valid.  Do not inspect visibility state or
        call ``bring_to_front`` here: either action made background collection fight
        the operator's minimize action. Navigation/readiness guards below still
        prevent incomplete pages from being stored as UNKNOWN data.
        """
        if self.page is None:
            raise WorkerExecutionError(
                "BitBrowser page is not connected",
                reason="worker_not_connected",
                pause_required=True,
                status_code=503,
            )
        session = self._cdp_session
        if session is not None:
            for method, params in (
                ("Page.setWebLifecycleState", {"state": "active"}),
                ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ):
                try:
                    await self._await_lifecycle_operation(
                        session.send(method, params),
                        timeout=self.cdp_command_timeout_seconds,
                    )
                except TimeoutError:
                    if self._cdp_session is session:
                        self._cdp_session = None
                    await self._dispose_page_resources(
                        None,
                        session,
                        close_page=False,
                    )
                    break
                except Exception:
                    pass

    async def bring_window_to_front(self) -> bool:
        """Explicitly hand this window to the operator after a safety pause.

        Routine background work deliberately never calls this method.  It is a
        fallback for the action manager's three-consecutive-failure/manual-attention
        path when the native BitBrowser PID cannot be restored directly.
        """

        if self.page is None:
            return False
        bring_to_front = getattr(self.page, "bring_to_front", None)
        if not callable(bring_to_front):
            return False
        await bring_to_front()
        return True

    async def _wait_for_profile_surface(self) -> None:
        """Wait for Instagram's client-rendered profile surface, not only the HTML shell."""
        # Current layouts can omit both semantic wrappers while already exposing
        # the exact terminal count cluster. Do not spend 15 seconds waiting for a
        # tag that this layout will never create. This is only a structural wait;
        # the guarded profile reader still verifies URL, privacy and all evidence.
        try:
            body_text = await self._page_body_text(timeout=1_000)
            if (extract_public_empty_profile_metrics(body_text) is not None
                    or extract_private_profile_metrics(body_text) is not None):
                return
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect initial profile surface")
        try:
            await self.page.locator("main:visible, header:visible").first.wait_for(state="visible", timeout=15_000)
        except Exception as exc:
            self._raise_page_failure(exc, operation="wait for Instagram profile")
            # Some Instagram experiments omit semantic main/header elements. Give the
            # client renderer a short grace period and let the visible-content checks
            # below decide whether the page is usable.
            try:
                await self.page.wait_for_timeout(800)
            except Exception as wait_exc:
                self._raise_page_failure(wait_exc, operation="wait for Instagram profile")
                await asyncio.sleep(0.8)

    async def _page_body_text(self, *, timeout: int = 3_000) -> str:
        try:
            return (await self._await_page_stage(
                self.page.locator("body").inner_text(timeout=timeout),
                timeout=max(.001, timeout / 1000),
            ))[:100_000]
        except WorkerExecutionError as exc:
            if exc.code != "browser_window_surface_unstable":
                raise
            return ""
        except Exception as exc:
            self._raise_page_failure(exc, operation="read Instagram page")
            return ""

    async def _has_visible_loading_indicator(self) -> bool:
        try:
            loading = self.page.locator(_VISIBLE_LOADING_SELECTOR)
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect Instagram loading state")
            return False
        return await self._first_visible(loading, maximum=40) is not None

    async def _has_visible_relation_loading_indicator(self, surface: Any) -> bool:
        """Inspect only the active relationship surface for a visible loader.

        Instagram commonly keeps unrelated recommendation, navigation or media
        spinners elsewhere in the document.  Those must not extend or invalidate a
        followers/following scan whose own dialog has already settled.
        """

        try:
            loading = surface.locator(_VISIBLE_LOADING_SELECTOR)
        except Exception as exc:
            self._raise_page_failure(
                exc, operation="inspect Instagram relationship loading state"
            )
            return True

        async def inspect_loading() -> bool:
            # The general optional-control helper skips failed count/visibility
            # reads. End-of-list evidence must not treat an unreadable loader as
            # absent, so propagate those failures to the conservative result below.
            count = min(await loading.count(), 40)
            for index in range(count):
                if await loading.nth(index).is_visible():
                    return True
            return False

        try:
            return await self._await_page_stage(
                inspect_loading(),
                timeout=self.collection_dom_operation_timeout_seconds,
            )
        except WorkerExecutionError as exc:
            if exc.code != "browser_window_surface_unstable":
                raise
            return True
        except asyncio.TimeoutError:
            # Do not turn an unresponsive loader query into an unbounded network
            # wait. Conservatively treat it as still loading; the bounded grace then
            # expires and emits the checkpoint-safe incomplete-list reason.
            return True
        except Exception as exc:
            self._raise_page_failure(
                exc, operation="inspect Instagram relationship loading state"
            )
            return True

    async def _relation_surface_failure(self, surface: Any) -> str | None:
        """Return page/guard failures without promoting a relation loader.

        The scan loop owns the scoped loader grace.  Once that bounded grace expires,
        a still-visible relation loader is an incomplete-list result (handled by the
        scheduler's bounded source retry), not proof that the whole network is down.
        """

        if self.page is None:
            # Compatibility for custom workers that provide a synthetic relation
            # surface without attaching a browser page.
            return await self._page_surface_failure()
        body_text = await self._page_body_text()
        folded = " ".join((body_text or "").casefold().split())
        try:
            current_url = str(self.page.url)
        except Exception as exc:
            return self._page_failure_reason(exc) or "worker_not_connected"
        guard_reason = await self._classify_page_guard(self.page, current_url, body_text or "")
        if guard_reason:
            return guard_reason
        transport_reason = await self._visible_transport_failure(self.page, body_text or "")
        if transport_reason:
            return transport_reason
        if not folded:
            return "instagram_profile_not_ready"
        return None

    async def _page_surface_failure(self, *, body_text: str | None = None) -> str | None:
        """Return a precise visible-page failure without guessing from missing rows."""
        if body_text is None:
            body_text = await self._page_body_text()
        folded = " ".join((body_text or "").casefold().split())
        try:
            current_url = str(self.page.url)
        except Exception as exc:
            return self._page_failure_reason(exc) or "worker_not_connected"
        guard_reason = await self._classify_page_guard(self.page, current_url, body_text or "")
        if guard_reason:
            return guard_reason
        transport_reason = await self._visible_transport_failure(self.page, body_text or "")
        if transport_reason:
            return transport_reason
        if await self._has_visible_loading_indicator() or not folded:
            return "instagram_profile_not_ready"
        return None

    @staticmethod
    def _raise_surface_reason(reason: str, message: str) -> None:
        raise WorkerExecutionError(
            message,
            reason=reason,
            pause_required=reason != "instagram_content_not_visible",
            status_code=503 if reason != "instagram_content_not_visible" else 409,
        )

    async def _read_inline_profile_evidence(self, username_norm: str) -> EmbeddedProfileEvidence:
        try:
            scripts = self.page.locator("script:not([src])")
        except Exception:
            return EmbeddedProfileEvidence()
        payloads: list[str] = []
        total_characters = 0

        # Pull matching bootstrap payloads across the CDP boundary in one pass.
        # Reading up to 150 script nodes one by one adds a round trip per node on
        # every profile; filtering in the page keeps the same exact-target and 8 MB
        # safeguards while reducing that cost to a single Playwright call.
        try:
            batched_payloads = await scripts.evaluate_all(
                """
                (nodes, username) => {
                  const target = String(username || "").toLowerCase();
                  const markers = [
                    "is_private", "isprivate", "taken_at", "takenat",
                    "media_count", "posts_count", "owner_to_timeline_media",
                    "user_id", '"pk"', "category_name", "business_category",
                    "professional_category", "external_url", "bio_links",
                    "is_verified", "isverified", "is_professional_account",
                    "isprofessionalaccount", "is_business_account",
                    "isbusinessaccount"
                  ];
                  const payloads = [];
                  let totalCharacters = 0;
                  for (const node of nodes.slice(0, 150)) {
                    const text = String(node.textContent || "");
                    if (!text) continue;
                    const folded = text.toLowerCase();
                    if (
                      !folded.includes(target)
                      || !markers.some((marker) => folded.includes(marker))
                    ) {
                      continue;
                    }
                    if (totalCharacters + text.length > 8000000) break;
                    payloads.push(text);
                    totalCharacters += text.length;
                  }
                  return payloads;
                }
                """,
                username_norm,
            )
            if isinstance(batched_payloads, list):
                payloads = [text for text in batched_payloads if isinstance(text, str)]
                return extract_embedded_profile_evidence(payloads, username_norm)
        except Exception:
            # Compatibility fallback for older Playwright shims and lightweight
            # test doubles that do not expose Locator.evaluate_all().
            pass

        try:
            count = min(await scripts.count(), 150)
        except Exception:
            return EmbeddedProfileEvidence()
        for index in range(count):
            try:
                text = await scripts.nth(index).text_content(timeout=2_000)
            except Exception as exc:
                self._raise_page_failure(exc, operation="inspect Instagram post likes")
                continue
            if not text:
                continue
            folded = text.casefold()
            if username_norm not in folded or not any(
                marker in folded
                for marker in (
                    "is_private",
                    "isprivate",
                    "taken_at",
                    "takenat",
                    "media_count",
                    "posts_count",
                    "owner_to_timeline_media",
                    "user_id",
                    '"pk"',
                    "category_name",
                    "business_category",
                    "professional_category",
                    "external_url",
                    "bio_links",
                    "is_verified",
                    "isverified",
                    "is_professional_account",
                    "isprofessionalaccount",
                    "is_business_account",
                    "isbusinessaccount",
                )
            ):
                continue
            if total_characters + len(text) > 8_000_000:
                break
            payloads.append(text)
            total_characters += len(text)
        return extract_embedded_profile_evidence(payloads, username_norm)

    async def _read_inline_profile_privacy(self, username_norm: str) -> bool | None:
        """Compatibility wrapper retained for privacy-only callers/tests."""
        return (await self._read_inline_profile_evidence(username_norm)).is_private

    async def _navigate_profile_with_privacy(
        self,
        target: str,
        *,
        reuse_navigation: bool = False,
    ) -> tuple[str, bool | None]:
        """Navigate once and retain exact-target evidence loaded for this page.

        Listening is passive: it does not call a GraphQL/private endpoint, and response
        bodies are discarded immediately after extracting exact-target privacy,
        counts, category, verification state, biography links and media timestamps.
        """
        username_norm, _ = normalize_instagram_username(target)
        self._bound_profile_caches(username_norm)
        cached = self._profile_privacy_cache.get(username_norm)
        response_evidence: set[bool] = set()
        response_posts_counts: set[int] = set()
        response_user_ids: set[str] = set()
        response_verified: set[bool] = set()
        response_professional: set[bool] = set()
        response_categories: dict[str, str] = {}
        response_external_bio_urls: set[str] = set()
        response_datetimes: set[str] = set(self._profile_post_datetime_cache.get(username_norm, ()))
        if isinstance(cached, bool):
            response_evidence.add(cached)
        cached_verified = self._profile_verified_cache.get(username_norm)
        if isinstance(cached_verified, bool):
            response_verified.add(cached_verified)
        cached_professional = self._profile_professional_cache.get(username_norm)
        if isinstance(cached_professional, bool):
            response_professional.add(cached_professional)
        cached_category = self._profile_category_cache.get(username_norm)
        if cached_category:
            response_categories[normalize_account_category(cached_category)] = cached_category
        cached_external_bio_url = self._profile_external_bio_url_cache.get(username_norm)
        if cached_external_bio_url:
            response_external_bio_urls.add(cached_external_bio_url)
        pending: set[asyncio.Task[Any]] = set()
        shared_captures = _PROFILE_CAPTURE_TASKS_BY_LOOP.setdefault(
            asyncio.get_running_loop(), set()
        )
        # Profile tabs can emit many unrelated JSON replies during navigation.
        # Keep only a small set of owned body reads and bounded, metadata-only
        # waiting replies; an event burst must not flood the shared CDP channel.
        queued: deque[Any] = deque(maxlen=32)
        capture_closed = False
        max_body_bytes = 8_000_000
        capture_page = self.page

        async def capture(response: Any) -> None:
            try:
                body_reader = getattr(response, "body", None)
                if callable(body_reader):
                    body = await body_reader()
                    if capture_closed or self.page is not capture_page:
                        return  # The next profile must not receive late evidence.
                    if not isinstance(body, bytes) or len(body) > max_body_bytes:
                        return
                    payload = json.loads(body)
                else:
                    # Compatibility for older lightweight adapters. Production
                    # Playwright supplies body(), checked before JSON decoding.
                    payload = await response.json()
            except Exception:
                return
            if capture_closed or self.page is not capture_page:
                return
            evidence = extract_embedded_profile_evidence((payload,), username_norm)
            if isinstance(evidence.is_private, bool):
                response_evidence.add(evidence.is_private)
            if evidence.posts_count is not None:
                response_posts_counts.add(evidence.posts_count)
            response_datetimes.update(evidence.post_datetimes)
            if evidence.instagram_user_id is not None:
                response_user_ids.add(evidence.instagram_user_id)
            if isinstance(evidence.is_verified, bool):
                response_verified.add(evidence.is_verified)
            if isinstance(evidence.is_professional_account, bool):
                response_professional.add(evidence.is_professional_account)
            if evidence.account_category:
                response_categories[
                    normalize_account_category(evidence.account_category)
                ] = evidence.account_category
            if evidence.external_bio_url:
                response_external_bio_urls.add(evidence.external_bio_url)

        def start_waiting_captures() -> None:
            while (not capture_closed and queued and len(self._profile_capture_tasks) < 4
                   and len(shared_captures) < _PROFILE_CAPTURE_LOOP_LIMIT):
                task = asyncio.create_task(capture(queued.popleft()))
                pending.add(task)
                self._profile_capture_tasks.add(task)
                shared_captures.add(task)
                # Own admitted I/O immediately, including when the observation
                # finishes before a slow response body. Disconnect/lease cleanup
                # already drains this registry; no body read is cancelled/lost.
                self._track_late_lifecycle_task(task)
                task.add_done_callback(capture_finished)

        def capture_finished(task: asyncio.Task[Any]) -> None:
            pending.discard(task)
            self._profile_capture_tasks.discard(task)
            shared_captures.discard(task)
            # Retrieve even cancellation/adapter failures before another event
            # can drop the last task reference.
            if not task.cancelled():
                task.exception()
            start_waiting_captures()

        def on_response(response: Any) -> None:
            if capture_closed:
                return
            try:
                response_url = urlparse(str(response.url))
                path = response_url.path.casefold()
                if (response_url.scheme != "https"
                        or response_url.hostname not in {"instagram.com", "www.instagram.com", "i.instagram.com"}
                        or response_url.username or response_url.password
                        or response_url.port not in {None, 443}):
                    return
                if not (path in {"/graphql", "/api/graphql"}
                        or path.startswith(("/graphql/", "/api/graphql/", "/api/v1/users/"))):
                    return
                status = getattr(response, "status", 200)
                if isinstance(status, int) and not 200 <= status < 300:
                    return
                length = str(response.headers.get("content-length", "")).strip()
                if length.isascii() and length.isdigit() and int(length) > max_body_bytes:
                    return
            except Exception:
                return
            # Keep the newest descriptors on overflow. Missing optional passive
            # evidence is still unknown: the exact-account inline and visible
            # reads below remain authoritative, never an inferred false value.
            queued.append(response)
            start_waiting_captures()

        listener_added = False
        try:
            capture_page.on("response", on_response)
            listener_added = True
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect Instagram post likes")
            pass
        try:
            if reuse_navigation:
                # Only the owning reader may reuse its current exact target. Keep
                # login/challenge and wrong-target guards ahead of any new sample.
                await self._guard()
                if not self._is_current_profile(username_norm):
                    raise WorkerExecutionError(
                        "Instagram did not remain on the requested profile",
                        reason="instagram_content_not_visible", pause_required=False,
                    )
                _, username = normalize_instagram_username(target)
            else:
                username = await self._navigate_profile(target)
            # A few Instagram builds paint the profile shell before their profile JSON
            # request is emitted. Keep the passive listener alive for a short, bounded
            # grace window rather than removing it immediately when no task exists yet.
            if not reuse_navigation:
                await asyncio.sleep(_PROFILE_PRIVACY_RESPONSE_GRACE_SECONDS)
            deadline = asyncio.get_running_loop().time() + 1.25
            while pending:
                remaining_seconds = deadline - asyncio.get_running_loop().time()
                if remaining_seconds <= 0:
                    break
                await asyncio.wait(tuple(pending), timeout=remaining_seconds)
        finally:
            capture_closed = True
            queued.clear()
            if listener_added:
                try:
                    capture_page.remove_listener("response", on_response)
                except Exception:
                    try:
                        capture_page.off("response", on_response)
                    except Exception:
                        pass
            # Passive observation is optional: a slow body cannot delay the
            # exact-account inline/visible fallback or poison a healthy page.
            # Still-running reads remain owned in _late_lifecycle_tasks and
            # consume the shared four-slot allowance until they actually settle.
            # capture_closed prevents those old readers from publishing data.

        inline_evidence = await self._read_inline_profile_evidence(username_norm)
        if isinstance(inline_evidence.is_private, bool):
            response_evidence.add(inline_evidence.is_private)
        if inline_evidence.posts_count is not None:
            response_posts_counts.add(inline_evidence.posts_count)
        response_datetimes.update(inline_evidence.post_datetimes)
        if inline_evidence.instagram_user_id is not None:
            response_user_ids.add(inline_evidence.instagram_user_id)
        if isinstance(inline_evidence.is_verified, bool):
            response_verified.add(inline_evidence.is_verified)
        if isinstance(inline_evidence.is_professional_account, bool):
            response_professional.add(inline_evidence.is_professional_account)
        if inline_evidence.account_category:
            response_categories[
                normalize_account_category(inline_evidence.account_category)
            ] = inline_evidence.account_category
        if inline_evidence.external_bio_url:
            response_external_bio_urls.add(inline_evidence.external_bio_url)
        structured_value = next(iter(response_evidence)) if len(response_evidence) == 1 else None
        if isinstance(structured_value, bool):
            self._remember_privacy(username_norm, structured_value)
        elif len(response_evidence) > 1:
            self._profile_privacy_cache.pop(username_norm, None)
        if len(response_posts_counts) == 1:
            self._profile_posts_count_cache[username_norm] = next(
                iter(response_posts_counts)
            )
        elif len(response_posts_counts) > 1:
            # A post may be created/deleted while the page is loading. Conflicting
            # snapshots are not safe evidence, so leave the rendered DOM authoritative.
            self._profile_posts_count_cache.pop(username_norm, None)
        if response_datetimes:
            self._profile_post_datetime_cache[username_norm] = tuple(
                sorted(response_datetimes, reverse=True)
            )
        if len(response_user_ids) == 1:
            self._profile_user_id_cache[username_norm] = next(iter(response_user_ids))
        elif len(response_user_ids) > 1:
            # Conflicting page payloads are not safe identity evidence.
            self._profile_user_id_cache.pop(username_norm, None)
        if len(response_verified) == 1:
            self._profile_verified_cache[username_norm] = next(iter(response_verified))
        elif len(response_verified) > 1:
            self._profile_verified_cache.pop(username_norm, None)
        if len(response_professional) == 1:
            self._profile_professional_cache[username_norm] = next(
                iter(response_professional)
            )
        elif len(response_professional) > 1:
            self._profile_professional_cache.pop(username_norm, None)
        if len(response_categories) == 1:
            self._profile_category_cache[username_norm] = next(
                iter(response_categories.values())
            )
        elif len(response_categories) > 1:
            self._profile_category_cache.pop(username_norm, None)
        if response_external_bio_urls:
            self._profile_external_bio_url_cache[username_norm] = sorted(
                response_external_bio_urls,
                key=lambda value: (len(value), value),
            )[0]
        self._bound_profile_caches(username_norm)
        return username, structured_value

    async def _first_visible(self, locator: Any, *, maximum: int = 30) -> Any | None:
        try:
            count = min(await self._await_page_probe(
                locator.count(), timeout=self.collection_dom_operation_timeout_seconds
            ), maximum)
        except WorkerExecutionError:
            raise
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect visible Instagram controls")
            return None
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if await self._await_page_probe(
                    candidate.is_visible(), timeout=self.collection_dom_operation_timeout_seconds
                ):
                    return candidate
            except WorkerExecutionError:
                raise
            except Exception as exc:
                self._raise_page_failure(exc, operation="inspect visible Instagram control")
                continue
        return None

    async def _has_visible_private_indicator(self) -> bool:
        snapshot_timeout = min(
            2.0, max(0.05, self.collection_dom_operation_timeout_seconds)
        )

        async def visible_texts(
            locator: Any, *, maximum: int, operation: str, svg_titles: bool = False,
        ) -> list[str]:
            """Read one DOM generation, not a sequence of detachable nth nodes."""
            batch_read = getattr(locator, "evaluate_all", None)
            if callable(batch_read):
                element_expression = "node.parentElement" if svg_titles else "node"
                text_expression = "node.textContent" if svg_titles else "node.innerText"
                script = """
                    nodes => nodes.slice(0, MAXIMUM).flatMap(node => {
                      const element = ELEMENT;
                      if (!element || !element.isConnected) return [];
                      const style = getComputedStyle(element);
                      const rect = element.getBoundingClientRect();
                      if (style.display === "none" || style.visibility === "hidden"
                          || style.visibility === "collapse"
                          || rect.width <= 0 || rect.height <= 0) return [];
                      return [String(TEXT || "")];
                    })
                """.replace("MAXIMUM", str(maximum)).replace(
                    "ELEMENT", element_expression
                ).replace("TEXT", text_expression)
                try:
                    values = await asyncio.wait_for(
                        batch_read(script), timeout=snapshot_timeout,
                    )
                    if isinstance(values, list) and all(
                        isinstance(value, str) for value in values
                    ):
                        return values
                except (AttributeError, NotImplementedError):
                    # Older/custom adapters keep the same visible-node semantics
                    # through a bounded fallback below.
                    pass
                except WorkerExecutionError:
                    raise
                except Exception as exc:
                    self._raise_page_failure(exc, operation=operation)
                    raise WorkerExecutionError(
                        "Instagram privacy evidence could not be read",
                        reason="instagram_profile_not_ready", pause_required=True,
                    ) from exc

            async def legacy_read() -> list[str]:
                texts: list[str] = []
                count = min(await locator.count(), maximum)
                for index in range(count):
                    node = locator.nth(index)
                    try:
                        if svg_titles:
                            text = await node.text_content(timeout=250)
                            visible = await node.locator("xpath=..").is_visible()
                        else:
                            visible = await node.is_visible()
                            if not visible:
                                continue
                            text = await node.inner_text(timeout=250)
                        if visible:
                            texts.append(text or "")
                    except WorkerExecutionError:
                        raise
                    except Exception as exc:
                        self._raise_page_failure(exc, operation=operation)
                        # A detached optional node may be skipped, but the whole
                        # fallback has a deadline so many detached nodes cannot
                        # consume the complete profile-read budget.
                        continue
                return texts

            try:
                return await asyncio.wait_for(legacy_read(), timeout=snapshot_timeout)
            except WorkerExecutionError:
                raise
            except Exception as exc:
                self._raise_page_failure(exc, operation=operation)
                raise WorkerExecutionError(
                    "Instagram privacy evidence could not be read",
                    reason="instagram_profile_not_ready", pause_required=True,
                ) from exc

        indicators = self.page.locator(
            ", ".join(
                (
                    '[aria-label="Private"]',
                    '[aria-label*="private account" i]',
                    '[title*="private account" i]',
                    '[aria-label*="私密"]',
                    '[title*="私密"]',
                    '[aria-label*="非公開"]',
                    '[title*="非公開"]',
                    '[aria-label*="비공개"]',
                    '[data-testid*="private" i]',
                )
            )
        )
        try:
            if await asyncio.wait_for(
                self._first_visible(indicators, maximum=30), timeout=snapshot_timeout,
            ) is not None:
                return True
        except WorkerExecutionError:
            raise
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect Instagram privacy indicators")
            raise WorkerExecutionError(
                "Instagram privacy indicators could not be read",
                reason="instagram_profile_not_ready", pause_required=True,
            ) from exc

        headings = self.page.locator("main h1, main h2, main h3, main [role=\"heading\"]")
        for text in await visible_texts(
            headings, maximum=30, operation="inspect Instagram privacy headings",
        ):
            if " ".join(text.casefold().split()) in _PRIVATE_HEADING_MARKERS:
                return True

        # SVG <title> is not visually laid out. Read the current titles in one DOM
        # snapshot: a recommendation rerender can detach an nth() locator after
        # count(), making text_content() auto-wait up to 30 seconds for a title
        # that will never return. That wait used to precede the zero-post gate.
        icon_titles = self.page.locator("main header svg title, main svg title")
        for text in await visible_texts(
            icon_titles, maximum=40, operation="inspect Instagram privacy icons",
            svg_titles=True,
        ):
            folded_title = " ".join(text.casefold().split())
            if any(marker in folded_title for marker in ("private", "lock", "私密", "非公開", "비공개")):
                return True

        # A pending follow request can only be shown for a private target. Restrict the
        # check to the profile header so unrelated suggested accounts cannot classify it.
        relationship_controls = self.page.locator(
            "main header button, main header [role=\"button\"], header main button"
        )
        for text in await visible_texts(
            relationship_controls, maximum=40,
            operation="inspect Instagram follow request controls",
        ):
            if " ".join(text.casefold().split()) in _PRIVATE_RELATIONSHIP_STATE_MARKERS:
                return True
        return False

    async def _has_stable_private_profile_structure(self, expected_posts: int | None) -> bool:
        """Recognize a stable restricted placeholder without guessing from absence.

        A positive post count plus a missing grid is *not* enough. We additionally
        require stable header metrics, a visible Follow control, no loading/error state,
        and either a visible restricted-content illustration or a second stable DOM
        sample. Instagram no longer renders the illustration in every desktop
        experiment, so requiring that one element incorrectly turned valid private
        profiles into ``unknown``. Silent/incomplete page loads still stay unknown.
        """
        if expected_posts is None or expected_posts <= 0:
            return False
        if await self._first_visible(self.page.locator(self.selectors.post_links), maximum=3):
            return False
        loading = self.page.locator(
            ", ".join(
                (
                    '[role="progressbar"]',
                    '[aria-busy="true"]',
                    'svg[aria-label*="loading" i]',
                    'svg[aria-label*="加载"]',
                    'svg[aria-label*="載入"]',
                )
            )
        )
        if await self._first_visible(loading, maximum=30) is not None:
            return False
        try:
            body_text = (await self.page.locator("body").inner_text(timeout=3_000)).casefold()
        except Exception:
            return False
        if any(marker in body_text for marker in _PROFILE_LOAD_FAILURE_MARKERS):
            return False

        header = self.page.locator("main header, header").first
        try:
            if not await header.count() or not await header.is_visible():
                return False
            header_text = await header.inner_text(timeout=3_000)
            header_box = await header.bounding_box()
        except Exception:
            return False
        _, _, current_posts = extract_visible_metrics(header_text)
        if current_posts != expected_posts or not header_box:
            return False

        controls = self.page.locator(
            "main header button, main header [role=\"button\"], header main button"
        )
        has_follow_control = False
        try:
            control_count = min(await controls.count(), 40)
        except Exception:
            control_count = 0
        for index in range(control_count):
            control = controls.nth(index)
            try:
                if not await control.is_visible():
                    continue
                text = " ".join((await control.inner_text(timeout=2_000)).casefold().split())
            except Exception:
                continue
            if text in _PROFILE_FOLLOW_CONTROL_MARKERS or text in _PRIVATE_RELATIONSHIP_STATE_MARKERS:
                has_follow_control = True
                break
        if not has_follow_control:
            return False

        # Private placeholders consistently render a lock-style illustration below the
        # profile header. We only use geometry here; an unrelated header/status icon is
        # excluded by requiring it to sit below the header and have placeholder size.
        icons = self.page.locator("main svg")
        try:
            icon_count = min(await icons.count(), 100)
        except Exception:
            icon_count = 0
        header_bottom = float(header_box["y"]) + float(header_box["height"])
        for index in range(icon_count):
            icon = icons.nth(index)
            try:
                if not await icon.is_visible():
                    continue
                box = await icon.bounding_box()
            except Exception:
                continue
            if not box:
                continue
            width = float(box["width"])
            height = float(box["height"])
            if (
                40 <= width <= 180
                and 40 <= height <= 180
                and float(box["y"]) >= header_bottom + 12
            ):
                return True

        # Newer desktop layouts sometimes omit the lock illustration altogether.
        # Confirm the same positive post count, follow/request control and missing
        # grid twice before accepting the restricted surface. A partially loaded
        # public profile normally changes one of these signals during this window.
        try:
            await self.page.wait_for_timeout(250)
        except Exception:
            await asyncio.sleep(0.25)
        if await self._first_visible(self.page.locator(self.selectors.post_links), maximum=3):
            return False
        if await self._first_visible(loading, maximum=30) is not None:
            return False
        try:
            second_body = (await self.page.locator("body").inner_text(timeout=3_000)).casefold()
            second_header_text = await header.inner_text(timeout=3_000)
        except Exception:
            return False
        if any(marker in second_body for marker in _PROFILE_LOAD_FAILURE_MARKERS):
            return False
        if any(marker in second_body for marker in _PUBLIC_EMPTY_PROFILE_MARKERS):
            return False
        _, _, second_posts = extract_visible_metrics(second_header_text)
        return second_posts == expected_posts

    async def _visible_relation_count(
        self,
        username_norm: str,
        relation: Literal["followers", "following"],
    ) -> _VisibleRelationCount | int | None:
        # Count and click target are intentionally independent. Current Instagram
        # can render "151 关注" as plain clickable text without an href. The exact
        # current-profile header remains authoritative for the target total.
        for poll in range(17):
            texts: list[str] = []
            try:
                header = await self._await_page_probe(
                    self._visible_profile_header(), timeout=self.collection_dom_operation_timeout_seconds
                )
                if header is not None:
                    texts.append(await self._await_page_probe(header.inner_text(timeout=3_000), timeout=3.0))
                texts.append(await self._await_page_probe(
                    self._visible_profile_stats_text(username_norm),
                    timeout=self.collection_dom_operation_timeout_seconds,
                ))
            except WorkerExecutionError:
                raise
            except Exception as exc:
                self._raise_page_failure(
                    exc, operation="read Instagram relationship count"
                )
            for visible_text in texts:
                followers, following, _ = extract_visible_metrics(visible_text)
                parsed_count = followers if relation == "followers" else following
                if parsed_count is not None:
                    return _visible_relation_count_estimate(
                        visible_text, parsed_count
                    )
            if poll < 16:
                await asyncio.sleep(0.5)
                await self._guard()
        return None

    async def _visible_post_urls(self, *, maximum: int) -> list[str]:
        locator = self.page.locator(self.selectors.post_links)
        try:
            count = min(await locator.count(), maximum * 3)
        except Exception as exc:
            self._raise_page_failure(exc, operation="read visible Instagram posts")
            return []
        urls: list[str] = []
        for index in range(count):
            link = locator.nth(index)
            try:
                if not await link.is_visible():
                    continue
                href = await link.get_attribute("href")
            except Exception as exc:
                self._raise_page_failure(exc, operation="read visible Instagram post")
                continue
            if not href:
                continue
            absolute = href if href.startswith("http") else f"https://www.instagram.com{href}"
            if absolute not in urls:
                urls.append(absolute)
            if len(urls) >= maximum:
                break
        return urls

    async def _visible_unpinned_post_urls(self, *, maximum: int) -> list[str]:
        """Return visible grid posts while excluding Instagram's pinned tiles.

        A pinned tile can occupy the first grid position for years. Activity must
        use the newest ordinary post below it, not the visual first tile. Instagram
        localizes the pin label, so inspect accessible labels/titles and common
        localized marker text inside each post anchor.
        """

        locator = self.page.locator(self.selectors.post_links)
        try:
            count = min(await locator.count(), max(maximum * 3, 18))
        except Exception as exc:
            self._raise_page_failure(exc, operation="read visible Instagram posts")
            return []
        pin_markers = (
            "pinned", "pinned post", "置顶", "已置顶", "固定", "ปักหมุด",
            "fijado", "fixado", "épinglé", "angeheftet", "закреп",
        )
        urls: list[str] = []
        for index in range(count):
            link = locator.nth(index)
            try:
                if not await link.is_visible():
                    continue
                href = str((await link.get_attribute("href")) or "").strip()
                if not href:
                    continue
                accessibility = await link.evaluate(
                    """node => Array.from(node.querySelectorAll('[aria-label], [title]'))
                    .concat([node])
                    .map(item => `${item.getAttribute?.('aria-label') || ''} ${item.getAttribute?.('title') || ''}`)
                    .join(' ')"""
                )
                folded = str(accessibility or "").casefold()
                if any(marker in folded for marker in pin_markers):
                    continue
            except Exception as exc:
                self._raise_page_failure(exc, operation="inspect Instagram pinned post marker")
                continue
            absolute = href if href.startswith("http") else f"https://www.instagram.com{href}"
            if absolute not in urls:
                urls.append(absolute)
            if len(urls) >= maximum:
                break
        return urls

    async def _visible_post_previews(
        self,
        *,
        maximum: int = 6,
        capture_preview_image: bool = False,
    ) -> list[dict[str, str]]:
        """Return exact visible grid post links and their rendered thumbnails.

        Only images nested inside the current profile's visible post anchors are
        accepted. This avoids recommendation images and does not open any post.
        """
        locator = self.page.locator(self.selectors.post_links)
        try:
            count = min(await locator.count(), max(maximum * 3, maximum), 30)
        except Exception as exc:
            self._raise_page_failure(exc, operation="read visible Instagram post previews")
            return []
        previews: list[dict[str, str]] = []
        seen_posts: set[str] = set()
        for index in range(count):
            link = locator.nth(index)
            screenshot: bytes | None = None
            try:
                if not await link.is_visible():
                    continue
                href = str((await link.get_attribute("href")) or "").strip()
                if not href:
                    continue
                post_url = href if href.startswith("http") else f"https://www.instagram.com{href}"
                if post_url in seen_posts:
                    continue
                image = link.locator("img").first
                thumbnail_url = ""
                if await image.count() and await image.is_visible():
                    thumbnail_url = str(
                        await image.evaluate("image => String(image.currentSrc || image.src || '')")
                        or ""
                    ).strip()
                    if capture_preview_image:
                        try:
                            screenshot = await image.screenshot(
                                type="jpeg", quality=38, timeout=5_000
                            )
                        except Exception:
                            screenshot = None
            except Exception as exc:
                self._raise_page_failure(exc, operation="read visible Instagram post preview")
                continue
            # A rendered screenshot is sufficient review evidence even when
            # Instagram exposes the image through a transient blob/currentSrc that
            # cannot safely be persisted as an external URL.
            if not screenshot and (
                not thumbnail_url or not self._is_allowed_avatar_url(thumbnail_url)
            ):
                continue
            seen_posts.add(post_url)
            preview = {"post_url": post_url}
            if thumbnail_url and self._is_allowed_avatar_url(thumbnail_url):
                preview["thumbnail_url"] = thumbnail_url
            if screenshot:
                preview["preview_data_url"] = (
                    "data:image/jpeg;base64," + base64.b64encode(screenshot).decode("ascii")
                )
            previews.append(preview)
            if len(previews) >= maximum:
                break
        return previews

    async def _read_original_post_datetime(
        self,
        post_url: str,
        *,
        now: datetime,
    ) -> datetime | None:
        try:
            await self.page.goto(post_url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as exc:
            try:
                await self._guard()
            except WorkerExecutionError:
                raise
            self._raise_page_failure(
                exc,
                operation="open newest Instagram post",
                navigation_timeout=True,
            )
            raise WorkerExecutionError(
                "Instagram post did not finish loading; check the network and resume the task",
                reason="instagram_network_unavailable",
                pause_required=True,
                status_code=503,
            ) from exc
        await self._guard()
        # Current post layouts place the original timestamp under either article or
        # main. Wait on both at once instead of spending a full timeout on article
        # before trying the same visible node through main.
        timestamps = self.page.locator(
            "article time[datetime], main time[datetime]"
        )
        try:
            await timestamps.first.wait_for(state="visible", timeout=3_000)
        except Exception as exc:
            self._raise_page_failure(exc, operation="wait for Instagram post timestamp")
        raw_values: list[str | None] = []
        try:
            count = min(await timestamps.count(), 60)
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect Instagram post timestamps")
            count = 0
        for index in range(count):
            timestamp = timestamps.nth(index)
            try:
                if await timestamp.is_visible():
                    raw_values.append(await timestamp.get_attribute("datetime"))
            except Exception as exc:
                self._raise_page_failure(exc, operation="read Instagram post timestamp")
                continue
        if not raw_values:
            try:
                body_text = (await self.page.locator("body").inner_text(timeout=3_000)).casefold()
            except Exception as exc:
                self._raise_page_failure(exc, operation="read Instagram post surface")
                body_text = ""
            if not body_text.strip() or any(
                marker in body_text for marker in _PROFILE_LOAD_FAILURE_MARKERS
            ):
                raise WorkerExecutionError(
                    "Instagram post surface is incomplete; retrying without saving unknown activity",
                    reason="instagram_profile_not_ready",
                    pause_required=True,
                    status_code=503,
                )
        return select_original_post_datetime(raw_values, now=now)

    async def _find_relation_trigger(
        self,
        username_norm: str,
        relation: Literal["followers", "following"],
    ) -> Any | None:
        # Read the current DOM in one operation. Hundreds of serial visibility and
        # href calls let recycled links stall this stage long before a list opens.
        labels = {
            "followers": r"(?:\d[\d,.\s]*[kmb万萬千亿億만]?\s*)?(?:followers?|粉丝|粉絲|ผู้ติดตาม)",
            "following": r"(?:\d[\d,.\s]*[kmb万萬千亿億만]?\s*)?(?:following|关注|關注|กำลังติดตาม)",
        }
        deadline = asyncio.get_running_loop().time() + 8.0
        for poll in range(17):
            links = self.page.locator("a[href]")
            try:
                snapshot = getattr(links, "evaluate_all", None)
                if callable(snapshot):
                    indices = await self._await_page_probe(snapshot(r"""(links, expected) => links.flatMap((link, index) => {
                      const style = getComputedStyle(link);
                      if (!link.getClientRects().length || style.visibility === 'hidden' ||
                          style.display === 'none' || link.closest('[role="dialog"], [aria-hidden="true"]')) return [];
                      try {
                        const url = new URL(link.getAttribute('href'), location.href);
                        if (!['www.instagram.com', 'instagram.com'].includes(url.hostname) ||
                            decodeURIComponent(url.pathname).toLowerCase().replace(/\/$/, '') !== expected) return [];
                        return [index];
                      } catch { return []; }
                    }).slice(0, 4)""", f"/{username_norm}/{relation}"), timeout=1.0)
                    if not isinstance(indices, list):
                        indices = []
                    for index in indices:
                        candidate = links.nth(index)
                        href = await self._await_page_probe(candidate.get_attribute("href", timeout=300), timeout=0.5)
                        if is_exact_profile_relation_href(href, username_norm, relation):
                            return candidate
                else:
                    # Older/custom adapters still receive exact-path matching.
                    async def inspect_links() -> Any | None:
                        for index in range(min(await links.count(), 500)):
                            candidate = links.nth(index)
                            if (await candidate.is_visible() and is_exact_profile_relation_href(
                                await candidate.get_attribute("href"), username_norm, relation
                            )):
                                return candidate
                        return None
                    candidate = await self._await_page_probe(inspect_links(), timeout=1.0)
                    if candidate is not None:
                        return candidate
            except WorkerExecutionError:
                raise
            except Exception as exc:
                self._raise_page_failure(exc, operation="read Instagram relationship control")
            # A URL-less statistic is allowed only inside this profile's header.
            # Anchoring prevents the whole statistics container or 'followed by'
            # prose from qualifying as the following/followers control.
            current_url = getattr(self.page, "url", None)
            header_source_matches = current_url is None or (
                self._is_current_profile(username_norm)
                or is_exact_profile_relation_href(str(current_url), username_norm, relation)
            )
            header = None
            if header_source_matches:
                try:
                    header = await self._await_page_probe(self._visible_profile_header(), timeout=1.0)
                except WorkerExecutionError:
                    raise
                except Exception as exc:
                    self._raise_page_failure(exc, operation="read Instagram relationship header")
            if header is not None:
                try:
                    clickables = header.locator(
                        'a, button, [role="link"], [role="button"], li, span'
                    ).filter(has_text=re.compile(r"^\s*" + labels[relation] + r"\s*$", re.IGNORECASE))
                    trigger = await self._await_page_probe(self._first_visible(clickables, maximum=60), timeout=1.0)
                    if trigger is not None:
                        return trigger
                except WorkerExecutionError:
                    raise
                except Exception as exc:
                    self._raise_page_failure(exc, operation="read Instagram relationship control")
            if poll >= 16 or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(min(0.5, max(0.0, deadline - asyncio.get_running_loop().time())))
            await self._guard()
        return None

    async def _select_relation_surface(
        self, username_norm: str, relation: Literal["followers", "following"],
        *, include_main: bool = False,
    ) -> Any | None:
        """Select a visible relation surface using one bounded DOM generation.

        The marker binds the returned locator to the selected element, so insertion
        of a hidden or unrelated last dialog cannot retarget the reader's locator.
        Diagnostics deliberately contain no account names, hrefs or page text.
        """
        selector = self.selectors.dialog + ', [role="dialog"], [aria-modal="true"]'
        if include_main:
            selector += ', main'
        surfaces = self.page.locator(selector)
        evaluate_all = getattr(surfaces, "evaluate_all", None)
        if not callable(evaluate_all):
            self.last_relation_surface_diagnostics = {"stage": "unsupported_surface_probe"}
            return None
        serial = int(getattr(self, "_relation_surface_probe_serial", 0)) + 1
        self._relation_surface_probe_serial = serial
        token = f"relation-{serial}"
        result = await self._await_page_probe(evaluate_all(r"""(surfaces, args) => {
          const norm = text => (text || '').normalize('NFKC').trim().toLowerCase().replace(/\s+/g, ' ');
          const names = {
            followers: ['followers','follower','粉丝','粉絲','追蹤者','追踪者','ผู้ติดตาม','フォロワー','팔로워',
              'seguidores','abonnés','abonnes','follower','pengikut','người theo dõi','takipçiler','подписчики','المتابعون'],
            following: ['following','关注','關注','追蹤中','追踪中','กำลังติดตาม','フォロー中','팔로잉',
              'siguiendo','seguindo','abonnements','gefolgt','seguiti','mengikuti','đang theo dõi','takip','подписки','يتابع']
          };
          for (const key of Object.keys(names)) names[key] = names[key].map(norm);
          const visible = node => {
            if (!node || node.closest('[hidden], [aria-hidden="true"]')) return false;
            const style = getComputedStyle(node);
            if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
            if (Array.from(node.getClientRects()).some(rect => rect.width > 0 && rect.height > 0)) return true;
            if (style.display !== 'contents') return false;
            // Keep title boundaries consistent with the relation-row reader:
            // boxless links may still render their username or avatar. Otherwise
            // a row named "followers" can be mistaken for a dialog title.
            for (const child of node.childNodes) {
              if (child.nodeType === Node.ELEMENT_NODE && visible(child)) return true;
              if (child.nodeType === Node.TEXT_NODE && child.textContent.trim()) {
                const range = document.createRange();
                range.selectNodeContents(child);
                if (Array.from(range.getClientRects()).some(rect => rect.width > 0 && rect.height > 0)) return true;
              }
            }
            return false;
          };
          let path = '';
          try { path = decodeURIComponent(location.pathname).toLowerCase().replace(/\/$/, ''); } catch {}
          const rightHost = ['www.instagram.com', 'instagram.com'].includes(location.hostname);
          const profileRoute = rightHost && path === '/' + args.username;
          const relationRoute = rightHost && path === '/' + args.username + '/' + args.relation;
          const info = {stage:'surface_probe', dialog_count:0, visible_dialog_count:0,
            relation_title_count:0, row_surface_count:0, scroll_surface_count:0,
            ambiguous_surface_count:0, source_matches:profileRoute || relationRoute, selected_kind:null};
          const candidates = [];
          for (const root of surfaces.slice(0, 40)) {
            const main = root.tagName === 'MAIN';
            if (!main) info.dialog_count++;
            if (!visible(root)) continue;
            if (!main) info.visible_dialog_count++;
            if (!(profileRoute || relationRoute) || (main && !relationRoute)) continue;
            const all = Array.from(root.querySelectorAll('*')).slice(0, 1200);
            const titleNodes = all.filter(node => node.matches('h1,h2,h3,[role="heading"],header,span,div') &&
              !node.closest('a,button,[role="button"]') && visible(node));
            const labels = [root.getAttribute('aria-label') || '', ...(root.getAttribute('aria-labelledby') || '')
              .split(/\s+/).map(id => document.getElementById(id)).filter(visible).map(node => node.innerText || '')];
            const profileLinks = Array.from(root.querySelectorAll('a[href]')).filter(link => {
              if (!visible(link)) return false;
              try {
                const url = new URL(link.getAttribute('href'), location.href);
                return ['www.instagram.com','instagram.com'].includes(url.hostname) &&
                  /^\/[a-zA-Z0-9._]{1,30}\/?$/.test(url.pathname) &&
                  !/^\/(?:explore|accounts|direct|reels|reel|stories|p)\/?$/.test(url.pathname);
              } catch { return false; }
            });
            // A relation title belongs above the account rows. A user named
            // 'followers' inside a row cannot turn an unrelated modal into a list.
            const firstLink = profileLinks[0];
            for (const node of titleNodes) {
              if (!firstLink || (!node.contains(firstLink) && (node.compareDocumentPosition(firstLink) & Node.DOCUMENT_POSITION_FOLLOWING))) {
                const text = norm(node.innerText || '');
                if (text.length < 60) labels.push(text);
              }
            }
            const normalized = labels.map(norm);
            const title = normalized.some(text => names[args.relation].includes(text));
            const other = normalized.some(text => names[args.relation === 'followers' ? 'following' : 'followers'].includes(text));
            const heading = Array.from(root.querySelectorAll('h1,h2,h3,[role="heading"]')).some(node => visible(node) && norm(node.innerText));
            const scroll = [root, ...all].some(node => visible(node) && node.clientHeight >= 40 &&
              node.clientWidth >= 80 && /auto|scroll/.test(getComputedStyle(node).overflowY));
            const search = all.some(node => visible(node) && node.matches('input,[role="searchbox"]'));
            const loading = all.some(node => visible(node) && node.matches('[role="progressbar"],[aria-busy="true"]'));
            const emptyText = norm(root.innerText || '');
            const empty = args.empty_markers.some(marker => emptyText.includes(norm(marker)));
            if (title) info.relation_title_count++;
            if (profileLinks.length) info.row_surface_count++;
            if (scroll) info.scroll_surface_count++;
            // An untitled route variant requires all three independent list
            // signals; a random dialog containing profile links is insufficient.
            const qualified = !other && ((title && (profileLinks.length || scroll || search || loading || empty)) ||
              (relationRoute && !heading && profileLinks.length && scroll && search));
            if (!qualified) continue;
            candidates.push({root, main, score:(title ? 20 : 0) + (profileLinks.length ? 3 : 0) + (scroll ? 2 : 0)});
          }
          candidates.sort((a,b) => b.score - a.score || (a.root.contains(b.root) ? 1 : b.root.contains(a.root) ? -1 : 0));
          if (candidates.length) {
            // Two separate qualified lists have no proven ownership tie to this
            // source. Wait for the old layer to disappear instead of guessing.
            const selected = candidates[0].root;
            const conflicts = candidates.filter(value => value.root !== selected &&
              !selected.contains(value.root) && !value.root.contains(selected));
            if (conflicts.length) {
              info.ambiguous_surface_count = conflicts.length + 1;
              return info;
            }
            for (const old of document.querySelectorAll('[data-juxin-relation-surface]')) old.removeAttribute('data-juxin-relation-surface');
            candidates[0].root.setAttribute('data-juxin-relation-surface', args.token);
            info.selected_kind = candidates[0].main ? 'main' : 'dialog';
          }
          return info;
        }""", {"username": username_norm, "relation": relation, "token": token,
               "empty_markers": list(_VISIBLE_COLLECTION_EMPTY_MARKERS)}), timeout=1.5)
        if not isinstance(result, dict):
            return None
        self.last_relation_surface_diagnostics = result
        if result.get("selected_kind"):
            return self.page.locator(f'[data-juxin-relation-surface="{token}"]')
        return None

    async def _wait_for_relation_surface(
        self, username_norm: str, relation: Literal["followers", "following"],
        *, include_main: bool = False,
    ) -> Any | None:
        deadline = asyncio.get_running_loop().time() + max(
            0.0, float(getattr(self, "relation_surface_wait_seconds", 12.0))
        )
        for poll in range(49):
            try:
                surface = await self._select_relation_surface(username_norm, relation, include_main=include_main)
                self.last_relation_surface_diagnostics["polls"] = poll + 1
                if surface is not None:
                    return surface
                if self.last_relation_surface_diagnostics.get("stage") == "unsupported_surface_probe":
                    return None
            except WorkerExecutionError:
                raise
            except Exception as exc:
                self._raise_page_failure(exc, operation="inspect Instagram relationship dialogs")
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(min(0.25, max(0.0, deadline - asyncio.get_running_loop().time())))
            await self._guard()
        return None

    async def _open_relation_surface(
        self,
        username_norm: str,
        relation: Literal["followers", "following"],
    ) -> Any:
        self.last_relation_surface_diagnostics = {"stage": "find_trigger"}
        # Resume may already be on the correct open list. Do not click through an
        # overlay or reload a healthy source merely to open the same dialog again.
        try:
            existing = await self._select_relation_surface(username_norm, relation, include_main=True)
            if existing is not None:
                return existing
        except WorkerExecutionError:
            raise
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect existing Instagram relationship list")
        trigger = await self._find_relation_trigger(username_norm, relation)
        if trigger is not None:
            try:
                await self._await_page_probe(trigger.click(timeout=10_000), timeout=10.0)
            except WorkerExecutionError:
                raise
            except Exception as exc:
                self._raise_page_failure(exc, operation="open Instagram relationship list")
                trigger = None
        if trigger is not None:
            surface = await self._wait_for_relation_surface(username_norm, relation)
            if surface is not None:
                return surface

        # Normal visible Instagram route, with the existing navigation boundary.
        # Source-page identity and permissions are checked independently of titles.
        relation_url = f"https://www.instagram.com/{username_norm}/{relation}/"
        navigation_failure: str | None = None
        last_error: BaseException | None = None
        try:
            await self._await_page_probe(
                self.page.goto(relation_url, wait_until="domcontentloaded", timeout=45_000), timeout=45.0
            )
        except WorkerExecutionError:
            raise
        except Exception as exc:
            last_error = exc
            navigation_failure = self._page_failure_reason(exc, navigation_timeout=True)
            if navigation_failure == "worker_not_connected":
                self._raise_surface_reason(navigation_failure, "Instagram relationship navigation lost its browser connection")
        await self._guard()
        surface = await self._wait_for_relation_surface(username_norm, relation, include_main=True)
        if surface is not None:
            return surface
        if navigation_failure:
            self._raise_surface_reason(navigation_failure, "Instagram relationship navigation was interrupted")
        surface_reason = await self._page_surface_failure()
        if surface_reason:
            self._raise_surface_reason(surface_reason, "Instagram relationship page is unavailable")
        error = WorkerExecutionError(
            f"Instagram 已打开目标主页，但没有显示{ '粉丝' if relation == 'followers' else '关注' }列表",
            reason=f"instagram_{relation}_list_not_rendered", pause_required=False,
        )
        error.details["surface_diagnostics"] = dict(self.last_relation_surface_diagnostics)
        raise error from last_error

    async def collect_followers(
        self,
        target: str,
        *,
        limit: int | None,
        candidate_sink: CandidateBatchSink | None = None,
        initial_candidate_count: int = 0,
        progress_sink: CollectionProgressSink | None = None,
        initial_resume_tail: Iterable[str] = (),
        initial_pending_relation_usernames: Iterable[str] = (),
        hover_precheck: bool = False,
    ) -> CollectionOutcome:
        return await self._collect_relation(
            target,
            relation="followers",
            limit=limit,
            candidate_sink=candidate_sink,
            initial_candidate_count=initial_candidate_count,
            progress_sink=progress_sink,
            initial_resume_tail=initial_resume_tail,
            initial_pending_relation_usernames=initial_pending_relation_usernames,
            hover_precheck=hover_precheck,
        )

    async def collect_following(
        self,
        target: str,
        *,
        limit: int | None,
        strict_source_total: bool = False,
        monitor_observation: bool = False,
        candidate_sink: CandidateBatchSink | None = None,
        initial_candidate_count: int = 0,
        progress_sink: CollectionProgressSink | None = None,
        initial_resume_tail: Iterable[str] = (),
        initial_pending_relation_usernames: Iterable[str] = (),
        hover_precheck: bool = False,
    ) -> CollectionOutcome:
        return await self._collect_relation(
            target,
            relation="following",
            limit=limit,
            strict_source_total=strict_source_total,
            **({"monitor_observation": True} if monitor_observation else {}),
            candidate_sink=candidate_sink,
            initial_candidate_count=initial_candidate_count,
            progress_sink=progress_sink,
            initial_resume_tail=initial_resume_tail,
            initial_pending_relation_usernames=initial_pending_relation_usernames,
            hover_precheck=hover_precheck,
        )

    def _remember_privacy(self, username_norm: str, value: bool) -> None:
        self._profile_privacy_cache[username_norm] = value
        self._bound_profile_caches(username_norm)

    def _profile_cache_estimated_bytes(self) -> int:
        return sum(cache.estimated_bytes() for name in self._PROFILE_CACHE_NAMES
                   if isinstance(cache := getattr(self, name), _ProfileMetadataCache)) + sys.getsizeof(self._profile_cache_recency)

    def _bound_profile_caches(self, current_username: str) -> None:
        """Bound reusable metadata, retaining required evidence for this attempt.

        Each worker (parent or child) owns its own caches. The current identity's
        partial evidence can still be needed by profile/activity assembly, so it
        is never truncated or discarded to satisfy an optional-cache budget.
        Oversized previous identities are discarded on the next identity; an
        oversized completed profile is not admitted to the reusable base cache.
        """
        self._profile_cache_recency.pop(current_username, None)
        present = False
        for name in self._PROFILE_CACHE_NAMES:
            cache = getattr(self, name)
            if current_username in cache:
                present = True
                if isinstance(cache, _ProfileMetadataCache):
                    cache.move_to_end(current_username)
                else:  # Preserve lightweight adapter compatibility.
                    value = cache.pop(current_username)
                    cache[current_username] = value
            if isinstance(cache, _ProfileMetadataCache):
                for username in tuple(cache._oversized):
                    if username != current_username:
                        cache.pop(username, None)
            while len(cache) > _PROFILE_REUSABLE_IDENTITIES:
                cache.pop(next(iter(cache)))
        if present:
            self._profile_cache_recency[current_username] = None
        while self._profile_cache_recency and (
            len(self._profile_cache_recency) > _PROFILE_REUSABLE_IDENTITIES
            or self._profile_cache_estimated_bytes() > _PROFILE_REUSABLE_CACHE_BYTES
        ):
            oldest = next(iter(self._profile_cache_recency))
            if oldest == current_username:
                break  # Required in-flight evidence, not reusable history.
            self._profile_cache_recency.pop(oldest, None)
            for name in self._PROFILE_CACHE_NAMES:
                getattr(self, name).pop(oldest, None)

    def _remember_profile(
        self,
        username_norm: str,
        profile: VisibleProfile,
        post_urls: Iterable[str] = (),
    ) -> None:
        # The current caller may consume the rendered avatar immediately, but cached
        # profile metadata must stay small and must never retain image bytes.
        cached = replace(profile, avatar_image_bytes=None)
        if _profile_metadata_size(cached) <= _PROFILE_REUSABLE_ENTRY_BYTES:
            # The returned profile remains untouched. A caller adding review
            # pixels later must not grow a previously admitted cache entry.
            self._profile_base_cache[username_norm] = replace(
                cached, recent_posts=copy.deepcopy(cached.recent_posts),
                review_cache=copy.deepcopy(cached.review_cache),
            )
        else:
            self._profile_base_cache.pop(username_norm, None)
        self._profile_post_url_cache[username_norm] = tuple(dict.fromkeys(post_urls))
        # The same bound applies to complete and incomplete profile observations.
        self._bound_profile_caches(username_norm)

    def _is_current_profile(self, username_norm: str) -> bool:
        return self._page_matches_profile(self.page, username_norm)

    async def _visible_profile_header(self) -> Any | None:
        """Return only the profile header at the top of the main target surface."""
        header = await self._first_visible(self.page.locator("main header"), maximum=3)
        if header is not None:
            return header
        # Some desktop experiments place the profile header directly under body.
        return await self._first_visible(self.page.locator("body > header"), maximum=2)

    async def _profile_metadata_snapshot(self) -> tuple[str, str | None]:
        """Optional metadata must never wait for a node removed by a rerender."""
        selectors = (
            'meta[property="og:description"], meta[name="description"]',
            'meta[property="og:image"]',
        )
        values: list[str] = []
        for selector in selectors:
            nodes = self.page.locator(selector)
            evaluate_all = getattr(nodes, "evaluate_all", None)
            if callable(evaluate_all):
                try:
                    captured = await asyncio.wait_for(
                        evaluate_all("nodes => nodes.slice(0, 1).map(node => String(node.getAttribute('content') || ''))"),
                        timeout=1.5,
                    )
                    if isinstance(captured, list) and all(isinstance(value, str) for value in captured):
                        values.append(captured[0] if captured else "")
                        continue
                except (AttributeError, NotImplementedError):
                    pass
                except Exception as exc:
                    self._raise_page_failure(exc, operation="read optional profile metadata")
                    values.append("")
                    continue
            # Compatibility for older/custom DOM adapters. The element may detach
            # between count and attribute lookup; its absence is not a failed page.
            value = ""
            try:
                if await nodes.count():
                    try:
                        value = (await nodes.first.get_attribute("content", timeout=300)) or ""
                    except TypeError:
                        value = (await asyncio.wait_for(nodes.first.get_attribute("content"), 0.3)) or ""
            except Exception as exc:
                self._raise_page_failure(exc, operation="read optional profile metadata")
            values.append(value)
        return values[0], values[1] or None

    async def _visible_profile_relation_counts(
        self, username_norm: str,
    ) -> tuple[int | None, int | None]:
        """Snapshot exact-target relation links without detached-nth auto waits."""
        values: list[int | None] = []
        fallback_deadline = asyncio.get_running_loop().time() + 2.0
        for relation in ("followers", "following"):
            links = self.page.locator(f'a[href*="/{relation}"]')
            captured = None
            evaluate_all = getattr(links, "evaluate_all", None)
            if callable(evaluate_all):
                try:
                    snapshot = await asyncio.wait_for(evaluate_all("""nodes => nodes.slice(0, 40).flatMap(node => {
                      const style = getComputedStyle(node), rect = node.getBoundingClientRect();
                      if (!node.isConnected || style.display === 'none'
                          || style.visibility === 'hidden' || style.visibility === 'collapse'
                          || rect.width <= 0 || rect.height <= 0) return [];
                      return [{href: String(node.getAttribute('href') || ''),
                               text: String(node.innerText || '')}];
                    })"""), timeout=1.5)
                    if isinstance(snapshot, list) and all(
                        isinstance(item, dict) and isinstance(item.get("href"), str)
                        and isinstance(item.get("text"), str) for item in snapshot
                    ):
                        captured = snapshot
                except (AttributeError, NotImplementedError):
                    pass
                except Exception as exc:
                    self._raise_page_failure(exc, operation="read optional profile relation counts")
                    captured = []
            if captured is None:
                captured = []
                try:
                    count = min(await links.count(), 40)
                except Exception as exc:
                    self._raise_page_failure(exc, operation="read profile relation counts")
                    count = 0
                for index in range(count):
                    remaining = fallback_deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    node = links.nth(index)
                    try:
                        async def read_node() -> dict[str, str] | None:
                            if not await node.is_visible():
                                return None
                            return {"href": (await node.get_attribute("href")) or "",
                                    "text": await node.inner_text()}
                        item = await asyncio.wait_for(read_node(), timeout=min(0.3, remaining))
                        if item is not None:
                            captured.append(item)
                    except Exception as exc:
                        self._raise_page_failure(exc, operation="read optional profile relation count")
            value = None
            for item in captured:
                if not is_exact_profile_relation_href(item["href"], username_norm, relation):
                    continue
                value = parse_visible_count(item["text"])
                if value is not None:
                    break
            values.append(value)
        return values[0], values[1]

    async def _visible_external_bio_url(
        self,
        header: Any,
        username_norm: str,
    ) -> str | None:
        """Return one visible off-Instagram link from the exact profile header."""

        if not self._is_current_profile(username_norm):
            return None
        try:
            links = header.locator("a[href]")
            hrefs = await links.evaluate_all(
                """
                nodes => nodes.slice(0, 40).flatMap(node => {
                  const style = window.getComputedStyle(node);
                  const rect = node.getBoundingClientRect();
                  if (
                    style.display === "none" || style.visibility === "hidden"
                    || Number(style.opacity || 1) === 0
                    || rect.width <= 0 || rect.height <= 0
                  ) return [];
                  return [String(node.getAttribute("href") || node.href || "")];
                })
                """
            )
            if isinstance(hrefs, list):
                for href in hrefs:
                    resolved = external_profile_link_url(href)
                    if resolved is not None:
                        return resolved
                return None
        except Exception:
            # Lightweight test doubles and older browser shims may not provide
            # evaluate_all; the bounded locator fallback keeps the same semantics.
            pass

        try:
            links = header.locator("a[href]")
            count = min(await links.count(), 40)
        except Exception:
            return None
        for index in range(count):
            link = links.nth(index)
            try:
                if not await link.is_visible():
                    continue
                resolved = external_profile_link_url(
                    await link.get_attribute("href")
                )
            except Exception as exc:
                self._raise_page_failure(
                    exc, operation="inspect visible Instagram biography link"
                )
                continue
            if resolved is not None:
                return resolved
        return None

    async def _visible_profile_stats_text(self, username_norm: str) -> str:
        """Read the exact target's visible statistics when outside its header.

        Instagram may render the posts/followers/following list as a sibling of the
        semantic header. Require both relationship links in the same visible list so
        a suggested-account card cannot be mistaken for the current profile.
        """

        if not self._is_current_profile(username_norm):
            return ""
        try:
            clusters = self.page.locator(
                ", ".join(
                    (
                        f'main ul:has(a[href$="/{username_norm}/followers/"]):has(a[href$="/{username_norm}/following/"])',
                        f'main [role="list"]:has(a[href$="/{username_norm}/followers/"]):has(a[href$="/{username_norm}/following/"])',
                        "main ul",
                        'main [role="list"]',
                    )
                )
            )
            count = min(await clusters.count(), 8)
        except Exception as exc:
            self._raise_page_failure(exc, operation="read Instagram profile statistics")
            return ""
        texts: list[str] = []
        for index in range(count):
            cluster = clusters.nth(index)
            try:
                if not await cluster.is_visible():
                    continue
                visible_text = await cluster.inner_text(timeout=3_000)
            except Exception as exc:
                self._raise_page_failure(
                    exc, operation="read visible Instagram profile statistics"
                )
                continue
            # A recommendation list can contain relationship words for many
            # accounts. Only a compact cluster containing all three profile
            # metrics is accepted as the current profile's statistics row.
            metrics = extract_visible_metrics(visible_text)
            if visible_text.strip() and all(value is not None for value in metrics):
                texts.append(visible_text)
        return "\n".join(texts)

    async def _source_profile_metrics(
        self, username_norm: str, *, relation: str | None = None,
        source_total: int | None = None,
    ) -> dict[str, Any]:
        """Best-effort source metadata from the already open exact profile.

        This snapshot never navigates or runs candidate screening. Optional DOM
        failures cannot delay collection indefinitely or turn it into a failure.
        """
        empty = {"username": username_norm, "followers": None, "following": None, "posts": None}
        values = dict(empty)

        def count(value: Any) -> int | None:
            return value if type(value) is int and value >= 0 else None

        async def read() -> None:
            if not self._is_current_profile(username_norm):
                return
            if relation in {"followers", "following"}:
                values[relation] = count(source_total)
            try:
                header = await self._visible_profile_header()
                header_text = await header.inner_text(timeout=1_000) if header is not None else ""
                for name, value in zip(("followers", "following", "posts"), extract_visible_metrics(header_text)):
                    if values[name] is None:
                        values[name] = count(value)
            except WorkerExecutionError:
                raise
            except Exception:
                pass
            if any(values[name] is None for name in ("followers", "following", "posts")):
                try:
                    stats = await self._visible_profile_stats_text(username_norm)
                    for name, value in zip(("followers", "following", "posts"), extract_visible_metrics(stats)):
                        if values[name] is None:
                            values[name] = count(value)
                except WorkerExecutionError:
                    raise
                except Exception:
                    pass
            try:
                followers, following = await self._visible_profile_relation_counts(username_norm)
                for name, value in (("followers", followers), ("following", following)):
                    if count(value) is not None and not (name == relation and count(source_total) is not None):
                        values[name] = value
            except WorkerExecutionError:
                raise
            except Exception:
                pass
            if values["posts"] is None:
                values["posts"] = count(self._profile_posts_count_cache.get(username_norm))

        try:
            await self._await_page_probe(read(), timeout=2.0)
            if not self._is_current_profile(username_norm):
                return {}
        except Exception:
            if self._page_stage_abandoned:
                raise
            # A timeout may retain already read metrics, but only on the same
            # exact source page. Cancellation still propagates normally.
            try:
                if not self._is_current_profile(username_norm):
                    return {}
            except Exception:
                return {}
        return values

    async def _capture_visible_profile_avatar(
        self,
        header: Any,
        username_norm: str,
    ) -> bytes | None:
        """Compatibility wrapper returning only the ephemeral avatar bytes."""

        captured = await self._capture_visible_profile_avatar_detailed(
            header,
            username_norm,
        )
        return captured.payload

    async def _capture_visible_profile_avatar_detailed(
        self,
        header: Any,
        username_norm: str,
    ) -> AvatarCaptureResult:
        """Get the exact target's highest-quality visible avatar with one fallback."""

        try:
            images = header.locator("img")
            count = min(await images.count(), 10)
        except Exception:
            return AvatarCaptureResult(None, "avatar_locator_failed")
        plausible: list[tuple[float, bool, Any, dict[str, Any]]] = []
        for index in range(count):
            candidate = images.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                alt = str((await candidate.get_attribute("alt")) or "").casefold()
                dimensions = await candidate.evaluate(
                    """image => ({
                        naturalWidth: Number(image.naturalWidth || 0),
                        naturalHeight: Number(image.naturalHeight || 0),
                        width: Number(image.getBoundingClientRect().width || 0),
                        height: Number(image.getBoundingClientRect().height || 0)
                    })"""
                )
                width = float(dimensions.get("width") or 0)
                height = float(dimensions.get("height") or 0)
                natural_width = float(dimensions.get("naturalWidth") or 0)
                natural_height = float(dimensions.get("naturalHeight") or 0)
            except Exception:
                continue
            if min(width, height, natural_width, natural_height) < 24:
                continue
            square_ratio = min(width, height) / max(width, height)
            if square_ratio < 0.65:
                continue
            # Resolve the exact target before looking for recommendation words:
            # valid Instagram usernames can themselves start with
            # ``recommended`` or ``suggested``.
            exact_username = bool(
                re.search(
                    rf"(?<![a-z0-9._])@?{re.escape(username_norm)}(?![a-z0-9._])",
                    alt,
                    re.IGNORECASE,
                )
            )
            if not exact_username and any(
                marker in alt
                for marker in (
                    "recommended",
                    "recommendation",
                    "suggested",
                    "suggestion",
                    "推荐",
                    "建議",
                )
            ):
                # Never infer a target from an explicitly labelled recommendation,
                # even when an Instagram experiment happens to place it in header.
                continue
            score = width * height * square_ratio
            # Instagram's accessible avatar label normally contains the exact
            # username plus a localized profile-picture word. Keep dimensions as a
            # fallback for experiments that omit or localize that label differently.
            if exact_username:
                score += 1_000_000
            if any(
                marker in alt
                for marker in (
                    "profile picture",
                    "profile photo",
                    "头像",
                    "頭像",
                    "foto del perfil",
                    "foto de perfil",
                    "รูปโปรไฟล์",
                )
            ):
                score += 500_000
            plausible.append((score, exact_username, candidate, dimensions))
        exact_matches = [item for item in plausible if item[1]]
        if exact_matches:
            _, _, selected, selected_dimensions = max(
                exact_matches,
                key=lambda item: item[0],
            )
        elif len(plausible) == 1:
            # A layout without localized alt text is safe only when the exact main
            # profile header exposes one plausible image. Multiple unlabelled images
            # may include recommendations, so ambiguity becomes unknown.
            _, _, selected, selected_dimensions = plausible[0]
        else:
            return AvatarCaptureResult(
                None,
                "avatar_target_ambiguous" if plausible else "avatar_not_found",
            )

        natural_width = int(float(selected_dimensions.get("naturalWidth") or 0))
        natural_height = int(float(selected_dimensions.get("naturalHeight") or 0))
        rendered_width = int(float(selected_dimensions.get("width") or 0))
        rendered_height = int(float(selected_dimensions.get("height") or 0))
        source_url = ""
        try:
            loaded = await asyncio.wait_for(
                selected.evaluate(
                    """async image => {
                        if (!image.complete || !image.naturalWidth || !image.naturalHeight) {
                            try { await image.decode(); } catch (_) {}
                        }
                        return {
                            complete: Boolean(image.complete),
                            currentSrc: String(image.currentSrc || image.src || ''),
                            sourceCandidates: [image.srcset || '', ...Array.from(
                                image.closest('picture')?.querySelectorAll('source[srcset]') || [],
                                source => source.srcset || ''
                            )].flatMap(srcset => String(srcset).split(',')).map(entry => {
                                const match = entry.trim().match(/^([^ ]+)[ ]+([0-9]+(?:[.][0-9]+)?)(w|x)$/);
                                if (!match) return null;
                                const amount = Number(match[2]);
                                return {
                                    url: match[1],
                                    score: match[3] === 'w' ? amount : amount * Number(image.naturalWidth || 1)
                                };
                            }).filter(Boolean),
                            naturalWidth: Number(image.naturalWidth || 0),
                            naturalHeight: Number(image.naturalHeight || 0)
                        };
                    }"""
                ),
                timeout=3.0,
            )
            if isinstance(loaded, dict):
                natural_width = int(float(loaded.get("naturalWidth") or natural_width))
                natural_height = int(float(loaded.get("naturalHeight") or natural_height))
                source_url = self._best_avatar_source_url(loaded)
                if (
                    "complete" in loaded
                    and (
                        not loaded.get("complete")
                        or min(natural_width, natural_height) < 24
                    )
                ):
                    return AvatarCaptureResult(
                        None,
                        "avatar_not_loaded",
                        natural_width=natural_width,
                        natural_height=natural_height,
                    )
        except Exception:
            # Some Playwright test doubles and older Chromium variants cannot expose
            # currentSrc. The already-rendered screenshot remains a bounded fallback.
            source_url = ""

        if source_url and self._is_allowed_avatar_url(source_url):
            downloaded = await self._download_visible_avatar(source_url)
            if downloaded is not None:
                payload, image_width, image_height = downloaded
                return AvatarCaptureResult(
                    payload,
                    None,
                    source="current_src",
                    image_width=image_width,
                    image_height=image_height,
                    natural_width=natural_width,
                    natural_height=natural_height,
                    source_url=source_url,
                )
        try:
            payload = await selected.screenshot(
                type="png",
                animations="disabled",
                timeout=5_000,
            )
        except Exception:
            return AvatarCaptureResult(
                None,
                "avatar_capture_timeout",
                natural_width=natural_width,
                natural_height=natural_height,
            )
        if not payload or len(payload) > 5 * 1024 * 1024:
            return AvatarCaptureResult(None, "avatar_capture_failed")
        return AvatarCaptureResult(
            bytes(payload),
            None,
            source="element_screenshot",
            image_width=rendered_width,
            image_height=rendered_height,
            natural_width=natural_width,
            natural_height=natural_height,
            source_url=source_url if self._is_allowed_avatar_url(source_url) else None,
        )

    @staticmethod
    def _best_avatar_source_url(loaded: dict[str, Any]) -> str:
        """Prefer the largest exact-image srcset candidate over rendered currentSrc."""

        ranked: list[tuple[float, str]] = []
        current = str(loaded.get("currentSrc") or "").strip()
        if current:
            ranked.append((float(loaded.get("naturalWidth") or 0), current))
        candidates = loaded.get("sourceCandidates")
        if isinstance(candidates, list):
            for candidate in candidates[:20]:
                if not isinstance(candidate, dict):
                    continue
                url = str(candidate.get("url") or "").strip()
                try:
                    score = float(candidate.get("score") or 0)
                except (TypeError, ValueError):
                    continue
                if url and math.isfinite(score) and score > 0:
                    ranked.append((score, url))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return next(
            (url for _, url in ranked if PlaywrightWorker._is_allowed_avatar_url(url)),
            current,
        )

    @staticmethod
    def _is_allowed_avatar_url(value: str) -> bool:
        try:
            parsed = urlparse(value)
        except Exception:
            return False
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        host = parsed.hostname.casefold().rstrip(".")
        return any(
            host == suffix or host.endswith(f".{suffix}")
            for suffix in ("cdninstagram.com", "fbcdn.net", "instagram.com")
        )

    async def _download_visible_avatar(
        self,
        source_url: str,
    ) -> tuple[bytes, int, int] | None:
        """Download only a URL already exposed by the exact visible avatar node."""

        response = None
        try:
            request_context = self.page.context.request
            response = await request_context.get(
                source_url,
                timeout=5_000,
                fail_on_status_code=False,
                headers={"Referer": "https://www.instagram.com/"},
            )
            if not response.ok:
                return None
            content_type = str(response.headers.get("content-type") or "").casefold()
            if not content_type.startswith("image/"):
                return None
            payload = bytes(await response.body())
            if not payload or len(payload) > 5 * 1024 * 1024:
                return None
            width, height = await asyncio.to_thread(self._decoded_image_size, payload)
            if min(width, height) < 24:
                return None
            return payload, width, height
        except Exception:
            return None
        finally:
            # APIRequestContext retains every response body until disposal or
            # context shutdown, even after body() returns copied image bytes.
            # Release this response on success, rejection and cancellation; the
            # shared request context belongs to the browser profile, not here.
            dispose = getattr(response, "dispose", None)
            if callable(dispose):
                try:
                    await finish_owned(self._await_lifecycle_operation(
                        dispose(), timeout=self.disconnect_timeout_seconds,
                    ))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass

    async def read_visible_person_evidence_images(
        self,
        target: str,
        *,
        limit: int = 3,
    ) -> list[bytes]:
        """Return a few exact-profile grid thumbnails for ambiguous avatar review.

        This bounded fallback never opens posts, never follows recommendation links,
        and is called only after avatar evidence is insufficient.  A single post can
        never decide the account category; the execution layer requires repeated,
        consistent evidence before using these bytes.
        """

        if self.page is None or limit <= 0:
            return []
        username_norm, _ = normalize_instagram_username(target)
        if not self._is_current_profile(username_norm):
            return []
        try:
            links = self.page.locator(self.selectors.post_links)
            count = min(await links.count(), max(3, limit * 3), 12)
        except Exception:
            return []
        urls: list[str] = []
        for index in range(count):
            link = links.nth(index)
            try:
                if not await link.is_visible():
                    continue
                image = link.locator("img").first
                if not await image.is_visible():
                    continue
                source_url = str(
                    await image.evaluate(
                        "image => String(image.currentSrc || image.src || '')"
                    )
                    or ""
                ).strip()
            except Exception:
                continue
            if (
                source_url
                and source_url not in urls
                and self._is_allowed_avatar_url(source_url)
            ):
                urls.append(source_url)
            if len(urls) >= limit:
                break
        payloads: list[bytes] = []
        for source_url in urls:
            downloaded = await self._download_visible_avatar(source_url)
            if downloaded is not None:
                payloads.append(downloaded[0])
        return payloads

    async def read_visible_profile_recovery_evidence(
        self, target: str, *, include_avatar_image: bool = False,
    ) -> dict[str, Any]:
        return await self._await_page_stage(
            self._read_visible_profile_recovery_evidence_once(target, include_avatar_image=include_avatar_image),
            timeout=5.0,
        )

    async def _read_visible_profile_recovery_evidence_once(
        self,
        target: str,
        *,
        include_avatar_image: bool = False,
    ) -> dict[str, Any]:
        """Salvage exact-profile avatar/privacy after the full profile read failed.

        Count selectors and the post grid can fail independently while the target
        header is already visible.  This method never navigates away, never opens a
        post and never inspects recommendations: evidence is accepted only while the
        current URL is the exact requested profile.
        """

        username_norm, _ = normalize_instagram_username(target)
        if self.page is None or not self._is_current_profile(username_norm):
            return {}
        header = await self._visible_profile_header()
        header_text = ""
        if header is not None:
            try:
                if await header.count() and await header.is_visible():
                    header_text = await header.inner_text(timeout=3_000)
            except Exception:
                header_text = ""
        meta_description = ""
        avatar_url = ""
        try:
            description_meta = self.page.locator(
                'meta[property="og:description"], meta[name="description"]'
            ).first
            if await description_meta.count():
                meta_description = str(
                    (await description_meta.get_attribute("content")) or ""
                )
            avatar_meta = self.page.locator('meta[property="og:image"]').first
            if await avatar_meta.count():
                avatar_url = str(
                    (await avatar_meta.get_attribute("content")) or ""
                ).strip()
        except Exception:
            pass
        followers, following, posts = extract_visible_metrics(
            "\n".join(part for part in (header_text, meta_description) if part)
        )
        if followers is None or following is None or posts is None:
            stats_followers, stats_following, stats_posts = extract_visible_metrics(
                await self._visible_profile_stats_text(username_norm)
            )
            if followers is None:
                followers = stats_followers
            if following is None:
                following = stats_following
            if posts is None:
                posts = stats_posts
        if posts is None:
            posts = self._profile_posts_count_cache.get(username_norm)
        for relation in ("followers", "following"):
            try:
                links = self.page.locator(f'a[href*="/{relation}"]')
                link_count = min(await links.count(), 40)
            except Exception:
                continue
            # Recovery must use the same exact-target relation evidence as a
            # normal read. A suggested account or stale dialog may precede the
            # target's own links, including when the target link has query params.
            for index in range(link_count):
                locator = links.nth(index)
                try:
                    if not is_exact_profile_relation_href(
                        await locator.get_attribute("href"), username_norm, relation
                    ) or not await locator.is_visible():
                        continue
                    parsed = parse_visible_count(await locator.inner_text())
                except Exception:
                    continue
                if parsed is not None:
                    if relation == "followers":
                        followers = parsed
                    else:
                        following = parsed
                    break
        avatar_capture = AvatarCaptureResult(None, "avatar_not_found")
        if include_avatar_image and header is not None:
            avatar_capture = await self._capture_visible_profile_avatar_detailed(
                header, username_norm
            )
        # The exact current profile's OpenGraph image is a safe second source when a
        # localized/experimental header prevents locating its rendered img element.
        if include_avatar_image and avatar_capture.payload is None:
            try:
                meta_url = avatar_url
            except Exception:
                meta_url = ""
            if meta_url and self._is_allowed_avatar_url(meta_url):
                downloaded = await self._download_visible_avatar(meta_url)
                if downloaded is not None:
                    payload, width, height = downloaded
                    avatar_capture = AvatarCaptureResult(
                        payload,
                        None,
                        source="exact_profile_og_image",
                        image_width=width,
                        image_height=height,
                        natural_width=width,
                        natural_height=height,
                    )
        visibility: Literal["public", "private", "unknown"] = "unknown"
        try:
            inline_evidence = await self._read_inline_profile_evidence(username_norm)
            structured_is_private = inline_evidence.is_private
            if inline_evidence.instagram_user_id is not None:
                self._profile_user_id_cache[username_norm] = (
                    inline_evidence.instagram_user_id
                )
            if posts is None and inline_evidence.posts_count is not None:
                posts = inline_evidence.posts_count
                self._profile_posts_count_cache[username_norm] = posts
            if isinstance(structured_is_private, bool):
                self._remember_privacy(username_norm, structured_is_private)
            if structured_is_private is True or await self._has_visible_private_indicator():
                visibility = "private"
            elif self._profile_privacy_cache.get(username_norm) is True:
                visibility = "private"
            elif self._profile_privacy_cache.get(username_norm) is False:
                visibility = "public"
            elif await self._first_visible(
                self.page.locator(self.selectors.post_links), maximum=3
            ) is not None:
                visibility = "public"
            elif await self._has_stable_private_profile_structure(posts):
                visibility = "private"
        except Exception:
            pass
        return {
            "visibility": visibility,
            "instagram_user_id": self._profile_user_id_cache.get(username_norm),
            "followers": followers,
            "following": following,
            "posts": posts,
            "avatar_url": avatar_url or None,
            "avatar_image_bytes": avatar_capture.payload,
            "avatar_capture_reason": avatar_capture.reason,
            "avatar_capture_source": avatar_capture.source,
            "avatar_capture_width": avatar_capture.image_width,
            "avatar_capture_height": avatar_capture.image_height,
            "recovery_evidence": True,
        }

    async def capture_visible_review_snapshot(
        self, target: str, *, include_post_previews: bool,
    ) -> dict[str, Any]:
        return await self._await_page_stage(
            self._capture_visible_review_snapshot_once(target, include_post_previews=include_post_previews),
            timeout=10.0,
        )

    async def _capture_visible_review_snapshot_once(
        self,
        target: str,
        *,
        include_post_previews: bool,
    ) -> dict[str, Any]:
        """Capture bounded review images only after screening retained the account."""

        username_norm, _ = normalize_instagram_username(target)
        if self.page is None:
            return {}
        if not self._is_current_profile(username_norm):
            await self._navigate_profile_with_privacy(username_norm)
        header = await self._visible_profile_header()
        if header is None or not self._is_current_profile(username_norm):
            return {}
        avatar_capture = await self._capture_visible_profile_avatar_detailed(
            header, username_norm
        )
        recent_posts = (
            await self._visible_post_previews(
                maximum=6,
                capture_preview_image=True,
            )
            if include_post_previews
            else []
        )
        review_cache: dict[str, Any] = {}
        if avatar_capture.payload:
            try:
                import io

                from PIL import Image  # type: ignore[import-not-found]

                with Image.open(io.BytesIO(avatar_capture.payload)) as source:
                    image = source.convert("RGB")
                    image.thumbnail((320, 320))
                    output = io.BytesIO()
                    image.save(output, format="JPEG", quality=58, optimize=True)
                    compact_avatar = output.getvalue()
                if len(compact_avatar) <= 80 * 1024:
                    review_cache["avatar_preview"] = (
                        "data:image/jpeg;base64,"
                        + base64.b64encode(compact_avatar).decode("ascii")
                    )
            except Exception:
                pass
        cached_posts: list[dict[str, str]] = []
        cached_post_bytes = 0
        for item in recent_posts:
            preview = item.get("preview_data_url")
            if not preview:
                continue
            preview_bytes = len(preview.encode("ascii"))
            if preview_bytes > 48 * 1024 or cached_post_bytes + preview_bytes > 150 * 1024:
                continue
            cached_posts.append(
                {"post_url": item["post_url"], "preview_data_url": preview}
            )
            cached_post_bytes += preview_bytes
        if cached_posts:
            review_cache["recent_posts"] = cached_posts
        durable_posts = [
            {key: value for key, value in item.items() if key != "preview_data_url"}
            for item in recent_posts
        ]
        return {
            "avatar_url": avatar_capture.source_url,
            "avatar_image_bytes": avatar_capture.payload,
            "avatar_capture_reason": avatar_capture.reason,
            "avatar_capture_source": avatar_capture.source,
            "avatar_capture_width": avatar_capture.image_width,
            "avatar_capture_height": avatar_capture.image_height,
            "recent_posts": durable_posts,
            "review_cache": review_cache,
            "review_snapshot_captured": True,
        }

    @staticmethod
    def _decoded_image_size(payload: bytes) -> tuple[int, int]:
        import io

        from PIL import Image  # type: ignore[import-not-found]

        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
            return int(image.width), int(image.height)

    async def _find_exact_profile_username_trigger(self, username_norm: str) -> Any | None:
        """Find the top username for the exact URL target, excluding suggestions."""
        if not self._is_current_profile(username_norm):
            return None
        header = await self._visible_profile_header()
        if header is None:
            return None
        exact_text = re.compile(rf"^@?{re.escape(username_norm)}$", re.IGNORECASE)

        # Prefer the visible heading/control that owns the large username shown at
        # the top of the profile. Some layouts also include a same-profile anchor;
        # clicking that anchor only navigates back to the profile and does not open
        # the information surface required by the agreed location flow.
        primary_nodes = header.locator(
            'h1, h2, button, [role="button"]'
        ).filter(has_text=exact_text)
        trigger = await self._first_visible(primary_nodes, maximum=8)
        if trigger is not None:
            return trigger

        # The username is a span in some Instagram experiments. Clicking the exact
        # visible text bubbles to its owning control, and restricting the lookup to
        # the main profile header excludes recommendations elsewhere on the page.
        try:
            exact_nodes = header.get_by_text(exact_text, exact=True)
        except TypeError:
            exact_nodes = header.get_by_text(exact_text)
        trigger = await self._first_visible(exact_nodes, maximum=6)
        if trigger is not None:
            return trigger

        # Keep an exact same-profile link only as a final DOM variant of the account
        # name itself. No generic header control and no options/three-dot button is
        # ever eligible here.
        exact_links = header.locator(
            ", ".join(
                (
                    f'a[href="/{username_norm}/"]',
                    f'a[href="/{username_norm}"]',
                    f'a[href="https://www.instagram.com/{username_norm}/"]',
                    f'a[href="https://www.instagram.com/{username_norm}"]',
                )
            )
        ).filter(has_text=exact_text)
        return await self._first_visible(exact_links, maximum=4)

    async def _has_visible_target_story(self, username_norm: str) -> bool:
        """Recognize only an exact target's visibly accessible Story control."""
        if self.page is None:
            return False
        if not self._is_current_profile(username_norm):
            await self._navigate_profile(username_norm)
        header = await self._visible_profile_header()
        if header is None:
            return False
        story_links = header.locator(
            ", ".join(
                (
                    f'a[href^="/stories/{username_norm}/"]',
                    f'a[href="/stories/{username_norm}"]',
                    f'a[href^="https://www.instagram.com/stories/{username_norm}/"]',
                    f'a[href="https://www.instagram.com/stories/{username_norm}"]',
                )
            )
        )
        if await self._first_visible(story_links, maximum=4) is not None:
            return True

        controls = header.locator(
            'button[aria-label], [role="button"][aria-label], '
            'button[title], [role="button"][title]'
        )
        try:
            count = min(await controls.count(), 30)
        except Exception:
            count = 0
        username_pattern = re.compile(
            rf"(?<![a-z0-9._])@?{re.escape(username_norm)}(?![a-z0-9._])",
            re.IGNORECASE,
        )
        story_markers = ("story", "stories", "快拍", "限时动态", "限時動態")
        for index in range(count):
            candidate = controls.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                label = " ".join(
                    value
                    for value in (
                        await candidate.get_attribute("aria-label"),
                        await candidate.get_attribute("title"),
                    )
                    if value
                )
            except Exception:
                continue
            folded = label.casefold()
            if username_pattern.search(folded) and any(marker in folded for marker in story_markers):
                return True
        return False

    @staticmethod
    def _apply_activity_datetime(
        profile: VisibleProfile,
        parsed_post_times: Iterable[datetime],
        *,
        reference: datetime,
    ) -> VisibleProfile:
        values = list(parsed_post_times)
        if not values:
            return profile
        newest = max(values)
        delta_seconds = (reference - newest).total_seconds()
        return replace(
            profile,
            recent_post_datetime=newest.isoformat(),
            activity_days=max(0, int(delta_seconds // 86_400)),
            activity_status="identified",
        )

    async def _read_profile_activity(
        self,
        username_norm: str,
        profile: VisibleProfile,
        *,
        include_post_activity: bool = False,
    ) -> VisibleProfile:
        result = replace(profile, recent_post_datetime=None, activity_days=None)
        has_story = False

        def finish(value: VisibleProfile, post_urls: Iterable[str] | None = None) -> VisibleProfile:
            if include_post_activity:
                value = replace(
                    value,
                    post_activity_days=value.activity_days,
                    post_activity_status=value.activity_status,
                )
                if has_story:
                    value = replace(value, activity_days=0, activity_status="story_today")
            if post_urls is not None:
                self._remember_profile(username_norm, value, post_urls)
            return value

        if result.visibility == "not_visible":
            return finish(replace(result, activity_status="profile_not_visible"))
        if result.visibility == "unknown":
            return finish(replace(result, activity_status="visibility_unknown"))

        # A post-only policy never waits for a Story/grid on a terminal profile.
        # Private accounts are outside that policy even when cached media exists.
        if include_post_activity and result.visibility == "private":
            return finish(replace(result, activity_status="private_not_visible"), ())
        if include_post_activity and result.posts == 0:
            return finish(replace(result, activity_status="no_posts"), ())

        # A Story is visible only when this signed-in window may open it. It is exact
        # target evidence and represents activity today, so no post needs to be opened.
        has_story = await self._has_visible_target_story(username_norm)
        if has_story and not include_post_activity:
            result = replace(result, activity_days=0, activity_status="story_today")
            return finish(result, self._profile_post_url_cache.get(username_norm, ()))
        if result.posts == 0:
            result = replace(result, activity_status="no_posts")
            return finish(result, ())

        cached_post_urls = list(self._profile_post_url_cache.get(username_norm, ()))
        # A restricted private account has no visible grid to inspect. Preserve the
        # explicit private outcome without navigating away from the verified surface.
        if result.visibility == "private" and not cached_post_urls:
            return finish(replace(result, activity_status="private_not_visible"))

        reference = datetime.now(timezone.utc)
        structured_times = [
            parsed
            for value in self._profile_post_datetime_cache.get(username_norm, ())
            if (parsed := _coerce_embedded_post_datetime(value, now=reference)) is not None
        ]
        if structured_times and not include_post_activity:
            result = self._apply_activity_datetime(result, structured_times, reference=reference)
            return finish(result, self._profile_post_url_cache.get(username_norm, ()))

        # The visual first tile may be an old pinned post. Re-read the current grid
        # and select the first *non-pinned* tile; its original post timestamp is the
        # activity value. Scan several tiles because multiple posts may be pinned.
        if not self._is_current_profile(username_norm):
            await self._navigate_profile_with_privacy(username_norm)
        post_urls = await self._visible_unpinned_post_urls(maximum=1)
        self._profile_post_url_cache[username_norm] = tuple(post_urls)
        # A private account that this window cannot open must not gain an activity
        # value merely because a response happens to contain hidden media metadata.
        if result.visibility == "private" and not post_urls:
            return finish(replace(result, activity_status="private_not_visible"))

        if not post_urls and structured_times and (
            not include_post_activity or (
                isinstance(result.posts, int) and not isinstance(result.posts, bool)
                and 0 < result.posts <= len(structured_times)
            )
        ):
            # With no ordinary tile, only a complete set of post timestamps proves
            # the latest date. A partial payload can contain only old pinned posts.
            result = self._apply_activity_datetime(result, structured_times, reference=reference)
            return finish(result, ())

        if not post_urls:
            # Profile headers commonly become ready several seconds before the grid.
            # Poll both structured timestamps and the visible grid so a slow page does
            # not become a false ``post_grid_unavailable`` result. Fast pages return
            # on the first sample and pay no fixed delay.
            for attempt in range(_ACTIVITY_GRID_POLL_ATTEMPTS):
                if await self._first_visible(
                    self.page.locator(self.selectors.post_links), maximum=3
                ) is not None:
                    break
                transient_reason = await self._profile_transport_failure(username_norm)
                if transient_reason and transient_reason != "instagram_profile_not_ready":
                    raise WorkerExecutionError(
                        "Instagram activity surface disconnected while waiting for posts",
                        reason=transient_reason,
                        pause_required=transient_reason != "instagram_content_not_visible",
                        status_code=503 if transient_reason != "instagram_content_not_visible" else 409,
                    )
                if attempt < _ACTIVITY_GRID_POLL_ATTEMPTS - 1:
                    try:
                        await self.page.wait_for_timeout(
                            _ACTIVITY_GRID_POLL_MILLISECONDS
                        )
                    except Exception:
                        await asyncio.sleep(
                            _ACTIVITY_GRID_POLL_MILLISECONDS / 1000
                        )
            # Initial profile classification already scanned the inline bootstrap.
            # Probe it only once more after the short grid window, rather than parsing
            # up to 150 script nodes on every polling round.
            refreshed = await self._read_inline_profile_evidence(username_norm)
            if refreshed.post_datetimes:
                self._profile_post_datetime_cache[username_norm] = (
                    refreshed.post_datetimes
                )
                structured_times = [
                    parsed
                    for value in refreshed.post_datetimes
                    if (parsed := _coerce_embedded_post_datetime(value, now=reference))
                    is not None
                ]
                if structured_times and not include_post_activity:
                    result = self._apply_activity_datetime(
                        result, structured_times, reference=reference
                    )
                    return finish(result, ())
            post_urls = await self._visible_unpinned_post_urls(maximum=1)
            self._profile_post_url_cache[username_norm] = tuple(post_urls)
        if not post_urls and structured_times:
            if include_post_activity and not (
                isinstance(result.posts, int) and not isinstance(result.posts, bool)
                and 0 < result.posts <= len(structured_times)
            ):
                return finish(replace(result, activity_status="latest_post_unconfirmed"), ())
            result = self._apply_activity_datetime(result, structured_times, reference=reference)
            return finish(result, ())
        if not post_urls:
            try:
                body_text = (await self.page.locator("body").inner_text(timeout=3_000))[:100_000]
            except Exception:
                body_text = ""
            transient_reason = await self._profile_transport_failure(
                username_norm,
                body_text=body_text,
            )
            if transient_reason:
                raise WorkerExecutionError(
                    "Instagram activity surface is not ready; retrying without saving unknown activity",
                    reason=transient_reason,
                    pause_required=transient_reason != "instagram_content_not_visible",
                    status_code=503 if transient_reason != "instagram_content_not_visible" else 409,
                )
            return finish(replace(result, activity_status="post_grid_unavailable"))

        # Inspect only the first non-pinned grid item. General account activity may
        # stop at a Story; an explicit post-age request always keeps the two separate.
        # Combine exact post evidence so an older pinned timestamp cannot win.
        parsed_datetime = await self._read_original_post_datetime(post_urls[0], now=reference)
        if parsed_datetime is not None:
            result = self._apply_activity_datetime(result, (*structured_times, parsed_datetime), reference=reference)
        else:
            result = replace(result, activity_status="timestamp_unavailable")
        return finish(result, post_urls)

    async def _profile_surface_is_transient(
        self,
        username_norm: str,
        *,
        body_text: str = "",
        metrics: tuple[int | None, int | None, int | None] = (None, None, None),
        confirmed_public_empty: bool = False,
        confirmed_private: bool = False,
    ) -> str | None:
        """Return a retryable reason only for an incomplete/wrong profile surface.

        A rendered profile with counts but ambiguous privacy remains a legitimate
        business ``unknown``.  In contrast, a wrong URL, an Instagram load-error page,
        or a shell with no header and no metrics is transport/UI readiness failure and
        must never be persisted as account data.
        """
        transport_reason = await self._profile_transport_failure(
            username_norm,
            body_text=body_text,
        )
        if transport_reason:
            # A fully rendered exact profile can coexist with a global progress
            # indicator from its post grid, recommendations or another page region.
            # Once all three header metrics are known, that unrelated loader is not
            # network evidence for an explicitly private profile (or the established
            # public-empty terminal surface). Real login, network, load-error and
            # wrong-target outcomes retain their precise transport reason.
            if (
                transport_reason == "instagram_profile_not_ready"
                and self._is_current_profile(username_norm)
                and (
                    confirmed_public_empty
                    or (
                        confirmed_private
                        and all(value is not None for value in metrics)
                    )
                    or (
                        all(value is not None for value in metrics)
                        and metrics[2] is not None
                        and metrics[2] > 0
                    )
                )
            ):
                return None
            return transport_reason
        if any(value is not None for value in metrics):
            return None
        # A non-empty, non-loading target page with none of the expected visible count
        # labels is not proof of a network outage. It is normally a localized/new DOM
        # experiment. Retry it briefly in read_visible_profile(), then surface a finite
        # selector diagnostic instead of entering the hours-long network loop.
        return "instagram_profile_dom_unrecognized"

    async def _profile_transport_failure(
        self,
        username_norm: str,
        *,
        body_text: str | None = None,
    ) -> str | None:
        """Distinguish transport, manual, and target states from a rendered DOM."""
        if body_text is None:
            try:
                body_text = (await self.page.locator("body").inner_text(timeout=3_000))[:100_000]
            except Exception as exc:
                reason = self._page_failure_reason(exc)
                if reason:
                    return reason
                body_text = ""
        folded = " ".join((body_text or "").casefold().split())
        try:
            current_url = str(self.page.url)
        except Exception as exc:
            return self._page_failure_reason(exc) or "worker_not_connected"
        guard_reason = await self._classify_page_guard(self.page, current_url, body_text or "")
        if guard_reason:
            return guard_reason
        transport_reason = await self._visible_transport_failure(self.page, body_text or "")
        if transport_reason:
            return transport_reason
        if await self._has_visible_loading_indicator():
            return "instagram_profile_not_ready"
        # An empty Instagram shell after a completed navigation is the most common
        # symptom of a short network/proxy interruption.
        if not folded or is_instagram_navigation_shell(body_text or ""):
            self._profile_shell_detected = True
            return "instagram_profile_not_ready"
        if not self._is_current_profile(username_norm):
            # A fully rendered redirect is account/permission evidence, not an
            # internet outage. Login/challenge routes were classified above.
            return "instagram_content_not_visible"
        return None

    def _forget_profile_attempt(self, username_norm: str) -> None:
        # A failed current-page read is not a reusable completed observation.
        # Forget every ephemeral projection for just this identity; durable
        # identity claims, saved results and history are owned by the service.
        for name in self._PROFILE_CACHE_NAMES:
            getattr(self, name).pop(username_norm, None)

    async def read_visible_profile(
        self,
        target: str,
        *,
        include_activity: bool = False,
        include_post_activity: bool = False,
        include_avatar_image: bool = False,
    ) -> VisibleProfile:
        """Read once, then try one fresh same-account page on a page failure.

        The old page stays open until the replacement yields a usable profile.
        A failed replacement pauses with its original checkpoint and error.
        """
        username_norm, _ = normalize_instagram_username(target)
        self._profile_shell_detected = False
        read_kwargs: dict[str, Any] = {"include_activity": include_activity}
        if include_post_activity:
            read_kwargs["include_post_activity"] = True
        if include_avatar_image:
            read_kwargs["include_avatar_image"] = True
        fresh_reason = self._fresh_page_retry_targets.pop(username_norm, None)
        if fresh_reason:
            last_error = WorkerExecutionError("显式重试新的资料页", reason="instagram_profile_not_ready")
        else:
            try:
                return await self._await_page_stage(
                    self._read_visible_profile_once(target, **read_kwargs),
                    timeout=self.profile_read_timeout_seconds,
                )
            except WorkerExecutionError as exc:
                if exc.code not in _TRANSIENT_PROFILE_REASONS:
                    raise
                last_error = exc
                self._forget_profile_attempt(username_norm)
        if last_error is not None and last_error.code in _PROFILE_TAB_RECOVERY_REASONS:
            await self._replace_stuck_page_once(username_norm, last_error)
            self._forget_profile_attempt(username_norm)
            progressed = False
            try:
                read_kwargs = {"include_activity": include_activity}
                if include_post_activity:
                    read_kwargs["include_post_activity"] = True
                if include_avatar_image:
                    read_kwargs["include_avatar_image"] = True
                result = await self._await_page_stage(
                    self._read_visible_profile_once(target, **read_kwargs),
                    timeout=self.profile_read_timeout_seconds,
                )
                progressed = True
                return result
            except WorkerExecutionError as exc:
                if exc.code in _PROFILE_RECOVERY_AUTHORITATIVE_REASONS:
                    raise
                raise self._page_recovery_exhausted(exc, username_norm) from exc
            finally:
                await self._finish_page_recovery(progressed=progressed)
        raise self._page_recovery_exhausted(last_error, username_norm) from last_error

    async def _read_visible_profile_once(
        self,
        target: str,
        *,
        include_activity: bool = False,
        include_post_activity: bool = False,
        include_avatar_image: bool = False,
    ) -> VisibleProfile:
        username_norm, _ = normalize_instagram_username(target)
        cached_profile = self._profile_base_cache.get(username_norm)
        if cached_profile is not None:
            if include_avatar_image and cached_profile.avatar_image_bytes is None:
                # Cached metadata intentionally excludes image bytes. Reopen the exact
                # profile only for an explicitly enabled local-recognition request.
                self._profile_base_cache.pop(username_norm, None)
                cached_profile = None
        if cached_profile is not None:
            self._bound_profile_caches(username_norm)
            cached_profile = replace(
                cached_profile, recent_posts=copy.deepcopy(cached_profile.recent_posts),
                review_cache=copy.deepcopy(cached_profile.review_cache),
            )
            if include_post_activity and cached_profile.post_activity_status in {"not_checked", "timestamp_unavailable", "post_grid_unavailable", "latest_post_unconfirmed"}:
                return await self._read_profile_activity(
                    username_norm, replace(cached_profile), include_post_activity=True,
                )
            if include_activity and cached_profile.activity_status in {"not_checked", "timestamp_unavailable", "post_grid_unavailable"}:
                return await self._read_profile_activity(username_norm, replace(cached_profile))
            return replace(cached_profile)

        username, structured_is_private = await self._navigate_profile_with_privacy(target)
        deadline = asyncio.get_running_loop().time() + _PROFILE_DATA_SETTLE_SECONDS
        while True:
            try:
                return await self._read_current_profile_snapshot(
                    username_norm, username, structured_is_private,
                    include_activity=include_activity,
                    include_post_activity=include_post_activity,
                    include_avatar_image=include_avatar_image,
                )
            except WorkerExecutionError as exc:
                # These two outcomes describe an incomplete render, not proof of a
                # broken tab. All other errors keep their original recovery policy.
                if (exc.code not in {"instagram_profile_not_ready", "instagram_profile_dom_unrecognized"}
                        or not self._is_current_profile(username_norm)):
                    raise
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise
                await asyncio.sleep(min(_PROFILE_DATA_POLL_SECONDS, remaining))
                username, structured_is_private = await self._navigate_profile_with_privacy(
                    target, reuse_navigation=True,
                )

    async def _read_current_profile_snapshot(
        self,
        username_norm: str,
        username: str,
        structured_is_private: bool | None,
        *,
        include_activity: bool = False,
        include_post_activity: bool = False,
        include_avatar_image: bool = False,
    ) -> VisibleProfile:
        """Read one settled-data attempt without navigating or reopening a tab."""
        header = await self._visible_profile_header()
        if header is None:
            # Keep a locator-shaped fallback so the following optional reads remain
            # best-effort, while readiness validation still sees an empty header.
            header = self.page.locator("main header").first
        header_text = ""
        if await header.count() and await header.is_visible():
            try:
                header_text = await header.inner_text(timeout=5_000)
            except Exception:
                header_text = ""
        verified_badge = header.locator(
            'svg[aria-label="Verified"], [role="img"][aria-label="Verified"], '
            'svg[aria-label="Verified badge"], [role="img"][aria-label="Verified badge"], '
            'svg[aria-label="已验证"], [role="img"][aria-label="已验证"], '
            'svg[aria-label="已驗證"], [role="img"][aria-label="已驗證"], '
            'svg[aria-label="已认证"], [role="img"][aria-label="已认证"], '
            'svg[aria-label="已認證"], [role="img"][aria-label="已認證"], '
            '[title="Verified"], [aria-label="认证账号"], [aria-label="認證帳號"]'
        )
        try:
            visible_verified_badge = bool(
                await verified_badge.count() and await verified_badge.first.is_visible()
            )
        except Exception:
            visible_verified_badge = False
        structured_verified = self._profile_verified_cache.get(username_norm)
        is_verified: bool | None = (
            True
            if visible_verified_badge
            else structured_verified
            if isinstance(structured_verified, bool)
            else None
        )
        account_category = self._profile_category_cache.get(username_norm)
        structured_professional = self._profile_professional_cache.get(username_norm)
        is_professional_account: bool | None = (
            True
            if account_category
            else structured_professional
            if isinstance(structured_professional, bool)
            else None
        )
        external_bio_url = self._profile_external_bio_url_cache.get(username_norm)
        if external_bio_url is None:
            external_bio_url = await self._visible_external_bio_url(
                header,
                username_norm,
            )
            if external_bio_url is not None:
                self._profile_external_bio_url_cache[username_norm] = external_bio_url
        meta_description, avatar_url = await self._profile_metadata_snapshot()
        # Always resolve the exact visible avatar URL for the review UI. Raw image
        # bytes remain ephemeral and are retained only when local recognition is on.
        # Downloading and decoding the avatar is required only when local person
        # recognition is enabled.  The normal collection path keeps the exact
        # profile OpenGraph URL for the review UI and avoids a blocking image
        # request/screenshot for every candidate.
        avatar_capture = AvatarCaptureResult(None, "avatar_capture_deferred")
        if include_avatar_image:
            avatar_capture = await self._capture_visible_profile_avatar_detailed(
                header, username_norm
            )
        avatar_image_bytes = avatar_capture.payload if include_avatar_image else None
        if avatar_capture.source_url:
            avatar_url = avatar_capture.source_url
        combined = "\n".join(part for part in (header_text, meta_description) if part)

        # Classify privacy before reading the three account metrics. Structured
        # exact-target evidence, the visible private marker, a visible grid, or a
        # localized empty-public marker is sufficient and uses this same navigation.
        # Ambiguous layouts remain UNKNOWN until the metric/stable-structure pass
        # below; no account is guessed public merely because counts are visible.
        try:
            body_text = (await self.page.locator("body").inner_text(timeout=5_000))[:100_000]
        except Exception:
            body_text = combined
        visible_post = None
        has_private_indicator = False
        if structured_is_private is not True:
            # Exact-target JSON is the strongest and cheapest privacy evidence. Only
            # a positive private value can skip the larger rendered-DOM scan. A
            # structured public value still permits a visible private marker to win
            # when Instagram exposes conflicting or stale bootstrap state.
            visible_post, has_private_indicator = await asyncio.gather(
                self._first_visible(
                    self.page.locator(self.selectors.post_links), maximum=3
                ),
                self._has_visible_private_indicator(),
            )
        visibility: Literal["public", "private", "unknown", "not_visible"] = classify_profile_visibility(
            body_text,
            has_visible_posts=visible_post is not None,
            has_private_indicator=has_private_indicator,
            structured_is_private=structured_is_private,
        )

        # Keep rendered counters ahead of metadata. A stale OpenGraph description
        # may still advertise posts after the visible page has become an empty
        # grid; parsing both strings together can even prefer its English labels
        # over the current Chinese header and incorrectly start post-grid reads.
        followers, following, posts = extract_visible_metrics(header_text)
        visible_header_zero = (
            posts == 0 and followers is not None and following is not None
        )

        # A fully rendered public zero-post page is terminal, even when Instagram
        # moved its counters outside the semantic header. This avoids classifying
        # the latest Chinese "这里空荡荡~" layout as an incomplete shell and repeatedly
        # reloading the same account.
        public_empty_metrics = (
            extract_public_empty_profile_metrics(body_text)
            if visibility == "public"
            else None
        )
        private_metrics = (
            extract_private_profile_metrics(body_text)
            if visibility == "private"
            else None
        )
        terminal_metrics = public_empty_metrics or private_metrics
        if terminal_metrics is not None:
            terminal_followers, terminal_following, terminal_posts = terminal_metrics
            if followers is None:
                followers = terminal_followers
            if following is None:
                following = terminal_following
            if posts is None:
                posts = terminal_posts

        meta_followers, meta_following, meta_posts = extract_visible_metrics(meta_description)
        if followers is None:
            followers = meta_followers
        if following is None:
            following = meta_following
        if posts is None:
            posts = meta_posts

        # Newer private-profile layouts put the statistics list outside ``header``.
        # Prefer that exact-profile cluster, then the passive exact-target JSON that
        # Instagram already loaded for this rendered page.
        if followers is None or following is None or posts is None:
            stats_followers, stats_following, stats_posts = extract_visible_metrics(
                await self._visible_profile_stats_text(username_norm)
            )
            if followers is None:
                followers = stats_followers
            if following is None:
                following = stats_following
            if posts is None:
                posts = stats_posts
        if posts is None:
            posts = self._profile_posts_count_cache.get(username_norm)

        # Prefer counts attached to this exact account's visible relationship
        # links. The first suffix match on the whole page can belong to a stale
        # dialog/recommendation and must never overwrite the target's metrics.
        relation_followers, relation_following = await self._visible_profile_relation_counts(username_norm)
        if relation_followers is not None:
            followers = relation_followers
        if relation_following is not None:
            following = relation_following

        transient_reason = await self._profile_surface_is_transient(
            username_norm,
            body_text=body_text,
            metrics=(followers, following, posts),
            confirmed_public_empty=(public_empty_metrics is not None or (
                structured_is_private is False and visibility == "public"
                and visible_header_zero
            )),
            confirmed_private=visibility == "private",
        )
        if transient_reason:
            raise WorkerExecutionError(
                "Instagram profile surface is not ready; retrying without saving unknown data",
                reason=transient_reason,
                pause_required=transient_reason != "instagram_content_not_visible",
                status_code=503 if transient_reason != "instagram_content_not_visible" else 409,
            )
        if visibility == "unknown":
            # Instagram often paints the header before the post grid. Give the rendered
            # surface one short chance to expose a post/private/empty-state signal before
            # conservatively keeping the account as unknown.
            try:
                await self.page.locator(self.selectors.post_links).first.wait_for(
                    state="visible", timeout=_UNKNOWN_VISIBILITY_WAIT_MILLISECONDS
                )
            except Exception:
                pass
            try:
                body_text = (await self.page.locator("body").inner_text(timeout=5_000))[:100_000]
            except Exception:
                pass
            # Counts and the terminal grid/private notice can settle independently.
            # If this second snapshot now supplies missing target metrics, restart
            # the same-page sample instead of combining its new privacy signal with
            # old None counts (or reporting its recommendation loader as a failure).
            refreshed_terminal_metrics = (
                extract_private_profile_metrics(body_text)
                or extract_public_empty_profile_metrics(body_text)
            )
            if (refreshed_terminal_metrics is not None
                    and any(value is None for value in (followers, following, posts))):
                raise WorkerExecutionError(
                    "Instagram profile data settled after the initial snapshot",
                    reason="instagram_profile_not_ready", pause_required=True,
                )
            transient_reason = await self._profile_surface_is_transient(
                username_norm,
                body_text=body_text,
                metrics=(followers, following, posts),
            )
            if transient_reason:
                raise WorkerExecutionError(
                    "Instagram profile surface became incomplete; retrying without saving unknown data",
                    reason=transient_reason,
                    pause_required=transient_reason != "instagram_content_not_visible",
                    status_code=503 if transient_reason != "instagram_content_not_visible" else 409,
                )
            visible_post = await self._first_visible(
                self.page.locator(self.selectors.post_links), maximum=3
            )
            has_private_indicator = await self._has_visible_private_indicator()
            refreshed_evidence = await self._read_inline_profile_evidence(username_norm)
            if refreshed_evidence.instagram_user_id is not None:
                self._profile_user_id_cache[username_norm] = (
                    refreshed_evidence.instagram_user_id
                )
            if isinstance(refreshed_evidence.is_private, bool):
                structured_is_private = refreshed_evidence.is_private
                self._remember_privacy(username_norm, refreshed_evidence.is_private)
            if posts is None and refreshed_evidence.posts_count is not None:
                posts = refreshed_evidence.posts_count
                self._profile_posts_count_cache[username_norm] = posts
            if refreshed_evidence.post_datetimes:
                self._profile_post_datetime_cache[username_norm] = refreshed_evidence.post_datetimes
            if isinstance(refreshed_evidence.is_verified, bool):
                self._profile_verified_cache[username_norm] = refreshed_evidence.is_verified
            if isinstance(refreshed_evidence.is_professional_account, bool):
                self._profile_professional_cache[username_norm] = (
                    refreshed_evidence.is_professional_account
                )
            if refreshed_evidence.account_category:
                self._profile_category_cache[username_norm] = (
                    refreshed_evidence.account_category
                )
            if refreshed_evidence.external_bio_url:
                self._profile_external_bio_url_cache[username_norm] = (
                    refreshed_evidence.external_bio_url
                )
            has_stable_private_structure = False
            if structured_is_private is None and visible_post is None:
                has_stable_private_structure = await self._has_stable_private_profile_structure(posts)
            visibility = classify_profile_visibility(
                body_text,
                has_visible_posts=visible_post is not None,
                has_private_indicator=has_private_indicator,
                structured_is_private=structured_is_private,
                has_stable_private_structure=has_stable_private_structure,
            )

        # Cache only explicit privacy evidence. Visible posts alone do not prove that
        # the account itself is public because a signed-in window may already follow a
        # private account and therefore be allowed to see its grid.
        if visibility == "private":
            self._remember_privacy(username_norm, True)
        elif structured_is_private is False:
            self._remember_privacy(username_norm, False)

        if account_category is None:
            account_category = self._profile_category_cache.get(username_norm)
        if account_category:
            is_professional_account = True
        elif is_professional_account is None:
            refreshed_professional = self._profile_professional_cache.get(username_norm)
            if isinstance(refreshed_professional, bool):
                is_professional_account = refreshed_professional
        if external_bio_url is None:
            external_bio_url = self._profile_external_bio_url_cache.get(username_norm)
        if not visible_verified_badge and is_verified is None:
            refreshed_verified = self._profile_verified_cache.get(username_norm)
            if isinstance(refreshed_verified, bool):
                is_verified = refreshed_verified

        # OpenGraph often contains counts only, while the rendered profile bio can
        # provide optional visible context. Keep only these public text sources for
        # a user-enabled review without requesting hidden profile data.
        description = "\n".join(
            part for part in (header_text, meta_description) if part.strip()
        )[:1000] or None
        # Images are intentionally deferred. The execution layer requests one final
        # bounded review snapshot only after every applicable gate has retained the
        # account. Until then keep only the newest visible post URL for activity.
        recent_posts: list[dict[str, str]] = []
        # Do not scan the post grid for accounts that may fail counts/location or for
        # private accounts. The later activity gate reads one non-pinned post only
        # when a public account has actually reached that stage.
        post_urls: list[str] = []
        review_cache: dict[str, Any] = {}
        profile = VisibleProfile(
            username=username,
            visibility=visibility,
            followers=followers,
            following=following,
            posts=posts,
            instagram_user_id=self._profile_user_id_cache.get(username_norm),
            recent_post_datetime=None,
            activity_days=None,
            activity_status="not_checked",
            visible_description=description,
            avatar_url=avatar_url,
            recent_posts=recent_posts,
            review_cache=review_cache,
            avatar_image_bytes=avatar_image_bytes,
            avatar_capture_reason=avatar_capture.reason,
            avatar_capture_source=avatar_capture.source,
            avatar_capture_width=avatar_capture.image_width,
            avatar_capture_height=avatar_capture.image_height,
            is_verified=is_verified,
            is_professional_account=is_professional_account,
            account_category=account_category,
            external_bio_url=external_bio_url,
        )
        self._remember_profile(username_norm, profile, post_urls)
        if include_post_activity:
            return await self._read_profile_activity(username_norm, profile, include_post_activity=True)
        if include_activity:
            return await self._read_profile_activity(username_norm, profile)
        return profile

    def get_cached_instagram_user_id(self, target: str) -> str | None:
        """Return only stable-id evidence already exposed on the current profile.

        This accessor never performs navigation or another profile read.  It lets
        the screening path collapse a renamed duplicate before an
        exclusion/history row is written.
        """
        username_norm, _ = normalize_instagram_username(target)
        return self._profile_user_id_cache.get(username_norm)

    async def _dismiss_profile_information_surfaces(self) -> None:
        """Close every About/menu layer before the next profile read.

        Instagram may stack an action menu below the final “About this account”
        dialog.  A single Escape can close only the top layer (or be consumed while
        the dialog is animating), leaving the profile header covered.  Subsequent
        privacy/count detection would then incorrectly become UNKNOWN.  Dismiss and
        verify boundedly; never navigate away from the exact candidate profile.
        """

        for _ in range(4):
            visible_surface = False
            for selector in (
                self.selectors.dialog,
                '[role="alertdialog"]',
                '[aria-modal="true"]',
                '[role="menu"]',
            ):
                try:
                    surfaces = self.page.locator(selector)
                    for index in range(min(await surfaces.count(), 8)):
                        if await surfaces.nth(index).is_visible():
                            visible_surface = True
                            break
                except Exception:
                    continue
                if visible_surface:
                    break
            if not visible_surface:
                return
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            try:
                await self.page.wait_for_timeout(180)
            except Exception:
                await asyncio.sleep(0.18)

        # Some Instagram variants ignore Escape and expose a labelled Close button.
        # Restrict the fallback to the visible dialog so Follow/Message controls in
        # the underlying profile can never be clicked.
        try:
            dialogs = self.page.locator(self.selectors.dialog)
            for index in range(min(await dialogs.count(), 8) - 1, -1, -1):
                dialog = dialogs.nth(index)
                if not await dialog.is_visible():
                    continue
                close_buttons = dialog.locator(
                    'button[aria-label="Close"], button[aria-label="关闭"], '
                    '[role="button"][aria-label="Close"], '
                    '[role="button"][aria-label="关闭"]'
                )
                close_button = await self._first_visible(close_buttons, maximum=8)
                if close_button is not None:
                    await close_button.click(timeout=3_000)
                    await self.page.wait_for_timeout(180)
                    break
        except Exception:
            pass

    @asynccontextmanager
    async def _location_request_lease(self) -> Any:
        """Run one complete location flow at a time per BitBrowser context."""

        coordinator = self._location_request_coordinator
        async with coordinator.lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if coordinator.blocked_until > now:
                remaining = max(1, math.ceil(coordinator.blocked_until - now))
                raise WorkerExecutionError(
                    f"Instagram 所在地详情仍在冷却中（约 {remaining} 秒），"
                    "已保留当前账号并将在冷却结束后自动重试",
                    reason="instagram_location_temporarily_unavailable",
                    pause_required=True,
                    status_code=503,
                    retry_after_seconds=remaining,
                )
            wait_seconds = max(0.0, coordinator.next_allowed_at - now)
            if wait_seconds:
                await asyncio.sleep(wait_seconds)
            try:
                yield coordinator
            finally:
                minimum = max(0.0, float(self.location_request_min_interval_seconds))
                maximum = max(
                    minimum, float(self.location_request_max_interval_seconds)
                )
                spacing = random.uniform(minimum, maximum) if maximum else 0.0
                coordinator.next_allowed_at = max(
                    coordinator.next_allowed_at,
                    loop.time() + spacing,
                )

    async def read_visible_account_location(self, target: str) -> str | None:
        try:
            return await self._read_visible_account_location_attempts(target)
        except WorkerExecutionError as exc:
            if exc.code not in _PROFILE_TAB_RECOVERY_REASONS | {"instagram_location_load_failed"}:
                raise
            recovery_error = exc
        username_norm, _ = normalize_instagram_username(target)
        await self._replace_stuck_page_once(username_norm, recovery_error)
        progressed = False
        try:
            result = await self._read_visible_account_location_attempts(target)
            progressed = True
            return result
        except WorkerExecutionError as exc:
            if exc.code in _PROFILE_RECOVERY_AUTHORITATIVE_REASONS:
                raise
            if exc.code == "instagram_location_load_failed":
                self._location_request_coordinator.blocked_until = max(
                    self._location_request_coordinator.blocked_until,
                    asyncio.get_running_loop().time() + max(0.0, float(self.location_failure_circuit_seconds)),
                )
            raise self._page_recovery_exhausted(exc, username_norm) from exc
        finally:
            await self._finish_page_recovery(progressed=progressed)

    async def _read_visible_account_location_attempts(self, target: str) -> str | None:
        """Read public country without overlapping sensitive Instagram requests.

        A rendered panel without a country remains unknown for review. A failed
        panel goes immediately to the outer fresh-page recovery path. Re-reading
        a valid panel must not reload the profile or bypass the shared cooldown.
        """
        async with self._location_request_lease() as coordinator:
            username_norm, _ = normalize_instagram_username(target)
            location_attempt = 0
            while location_attempt < _LOCATION_FLOW_ATTEMPTS:
                self._location_terminal_unknown_target = None
                location = await self._await_page_stage(self._read_visible_account_location_once(
                    target, force_reload=False,
                ), timeout=self.location_read_timeout_seconds)
                if location is not None:
                    coordinator.blocked_until = 0.0
                    return location
                if self._location_terminal_unknown_target == username_norm:
                    # A rendered, valid About panel may intentionally omit country.
                    # Reopening it cannot make that omission into a transport error.
                    coordinator.blocked_until = 0.0
                    return None
                location_attempt += 1
                if location_attempt < _LOCATION_FLOW_ATTEMPTS:
                    try:
                        await self.page.wait_for_timeout(
                            int(_LOCATION_FLOW_RETRY_DELAY_SECONDS * 1000)
                        )
                    except Exception:
                        await asyncio.sleep(_LOCATION_FLOW_RETRY_DELAY_SECONDS)
            coordinator.blocked_until = 0.0
            return None

    async def _validate_location_target(self, username_norm: str) -> None:
        """Fence every location result against login changes and target switches."""
        await self._guard()
        if not self._is_current_profile(username_norm):
            raise WorkerExecutionError(
                "Instagram profile changed while reading account location",
                reason="instagram_wrong_profile_target", pause_required=True,
                status_code=503,
            )

    async def _finish_unknown_location(self, username_norm: str) -> None:
        """Finish only a verified, visible About outcome on the exact target."""
        await self._validate_location_target(username_norm)
        self._location_terminal_unknown_target = username_norm

    async def _location_surface_snapshot(self, surface: Any) -> tuple[str, bool]:
        """Read text and loading state from one still-visible About surface."""
        try:
            result = await surface.evaluate(
                """(element, selector) => {
                    const visible = node => node.isConnected && node.getClientRects().length > 0
                        && getComputedStyle(node).visibility !== 'hidden'
                        && getComputedStyle(node).visibility !== 'collapse';
                    if (!visible(element)) throw new Error('About surface disappeared');
                    return {text: element.innerText, loading: [...element.querySelectorAll(selector)].some(visible)};
                }""",
                _VISIBLE_LOADING_SELECTOR, timeout=1_500,
            )
            if not isinstance(result, dict) or not isinstance(result.get("text"), str) or not isinstance(result.get("loading"), bool):
                raise ValueError("About surface snapshot is incomplete")
            return result["text"], result["loading"]
        except WorkerExecutionError:
            raise
        except Exception as exc:
            raise WorkerExecutionError(
                "Instagram 账户地区面板暂时无法读取",
                reason="instagram_location_load_failed", pause_required=True,
                status_code=503,
            ) from exc

    async def _read_visible_account_location_once(
        self,
        target: str,
        *,
        force_reload: bool = False,
    ) -> str | None:
        """Best-effort read of Instagram's visible “About this account” location.

        UI variants that do not expose this field return ``None``; the worker never
        reads hidden metadata or treats an unavailable field as a known location.
        """
        username_norm, _ = normalize_instagram_username(target)
        self._location_terminal_unknown_target = None
        if self._is_current_profile(username_norm) and not force_reload:
            # Screening has just read this exact profile. Reuse that verified DOM
            # instead of issuing a second network navigation solely for location.
            await self._ensure_window_surface_stable()
            await self._guard()
        else:
            await self._navigate_profile(username_norm)
        # The same already-rendered header/bootstrap data can expose Instagram's
        # stable user id without reading counts, posts, activity, or another page.
        # Cache it while reading the mandatory location screening operation.
        if username_norm not in self._profile_user_id_cache:
            try:
                identity_evidence = await self._read_inline_profile_evidence(username_norm)
                if identity_evidence.instagram_user_id is not None:
                    self._profile_user_id_cache[username_norm] = (
                        identity_evidence.instagram_user_id
                    )
            except Exception:
                # Stable-id evidence is optional. Location errors retain their own
                # explicit handling and are never fabricated from this probe.
                pass
        try:
            # The required entry point is the exact target username at the top of its
            # own profile. Do not click recommendation usernames or generic menus.
            username_trigger = None
            for attempt in range(_LOCATION_TRIGGER_POLL_ATTEMPTS):
                username_trigger = await self._find_exact_profile_username_trigger(username_norm)
                if username_trigger is not None:
                    break
                transient_reason = await self._profile_transport_failure(username_norm)
                if transient_reason and transient_reason != "instagram_profile_not_ready":
                    raise WorkerExecutionError(
                        "Instagram profile is unavailable while waiting for location control",
                        reason=transient_reason,
                        pause_required=transient_reason != "instagram_content_not_visible",
                        status_code=503 if transient_reason != "instagram_content_not_visible" else 409,
                    )
                if attempt < _LOCATION_TRIGGER_POLL_ATTEMPTS - 1:
                    try:
                        await self.page.wait_for_timeout(
                            _LOCATION_TRIGGER_POLL_MILLISECONDS
                        )
                    except Exception:
                        await asyncio.sleep(
                            _LOCATION_TRIGGER_POLL_MILLISECONDS / 1000
                        )
            if username_trigger is None:
                transient_reason = await self._profile_transport_failure(username_norm)
                if transient_reason:
                    raise WorkerExecutionError(
                        "Instagram profile is unavailable while reading location",
                        reason=transient_reason,
                        pause_required=transient_reason != "instagram_content_not_visible",
                        status_code=503 if transient_reason != "instagram_content_not_visible" else 409,
                    )
            else:
                await username_trigger.click(timeout=10_000)

            # Clicking the exact top username can render an intermediate ARIA menu
            # and then the About dialog. Treat both as visible surfaces, but accept
            # a country only from the labelled location row parsed below. The
            # profile options/three-dot control is deliberately never used.
            clicked_about_entry = False
            for attempt in range(_LOCATION_DIALOG_POLL_ATTEMPTS):
                settled_about_surface = False
                loading_about_surface = False
                visible_surfaces: list[Any] = []
                # Count both kinds independently. A hidden dialog commonly remains
                # mounted while the real, visible About entry is rendered as a menu;
                # choosing one locator merely because its raw count is non-zero loses
                # every location in that Instagram layout.
                for selector in (
                    self.selectors.dialog,
                    '[role="alertdialog"]',
                    '[aria-modal="true"]',
                    '[role="menu"]',
                ):
                    try:
                        surfaces = self.page.locator(selector)
                        for index in range(min(await surfaces.count(), 8) - 1, -1, -1):
                            surface = surfaces.nth(index)
                            if await surface.is_visible():
                                visible_surfaces.append(surface)
                    except Exception:
                        continue
                for dialog in visible_surfaces:
                    try:
                        text = await dialog.inner_text(timeout=2_000)
                    except Exception:
                        continue
                    folded = " ".join(text.casefold().split())
                    if is_about_account_load_failure(text):
                        raise WorkerExecutionError(
                            "Instagram 未能加载账户所在地详情",
                            reason="instagram_location_load_failed",
                            pause_required=True,
                            status_code=503,
                        )
                    if _about_account_location_is_undisclosed(text):
                        await self._finish_unknown_location(username_norm)
                        return None
                    # An exact visible location label is sufficient evidence even in
                    # variants that omit the About title from the accessible text.
                    location = extract_about_account_location(text)
                    if location is not None:
                        await self._validate_location_target(username_norm)
                        return location
                    is_about_surface = any(
                        marker in folded for marker in _ABOUT_ACCOUNT_TITLE_MARKERS
                    )
                    # Some current desktop layouts render the label and country in
                    # separate nested flex nodes. ``inner_text`` can then flatten
                    # them without a reliable line break. Read only the exact
                    # labelled row and its immediate DOM neighbours as a second,
                    # still-visible source of evidence.
                    try:
                        label_pattern = re.compile(
                            r"^(?:" + "|".join(
                                re.escape(label) for label in _ACCOUNT_LOCATION_LABELS
                            ) + r")$",
                            re.IGNORECASE,
                        )
                        labels = dialog.get_by_text(label_pattern, exact=True)
                        label_count = min(await labels.count(), 8)
                        for label_index in range(label_count):
                            label_node = labels.nth(label_index)
                            if not await label_node.is_visible():
                                continue
                            nearby = (
                                label_node.locator("xpath=following-sibling::*[1]"),
                                label_node.locator("xpath=..").locator(
                                    "xpath=following-sibling::*[1]"
                                ),
                                label_node.locator("xpath=.."),
                            )
                            for node in nearby:
                                if await node.count() < 1:
                                    continue
                                candidate_text = await node.first.inner_text(timeout=1_500)
                                if _is_undisclosed_location_value(candidate_text):
                                    await self._finish_unknown_location(username_norm)
                                    return None
                                candidate_location = (
                                    extract_about_account_location(candidate_text)
                                    or _translate_labelled_location_value(candidate_text)
                                )
                                if candidate_location is not None:
                                    await self._validate_location_target(username_norm)
                                    return candidate_location
                    except WorkerExecutionError:
                        raise
                    except Exception:
                        # DOM variants without these relationships continue through
                        # the normal retry loop; they are never treated as non-US.
                        pass
                    # In several current Instagram layouts clicking the username
                    # opens an intermediate action menu whose item is named “About
                    # this account”. The old reader mistook that menu text for the
                    # final About panel and waited until UNKNOWN.
                    if is_about_surface and not clicked_about_entry:
                        entry_pattern = re.compile(
                            r"^(?:" + "|".join(
                                re.escape(label) for label in _ABOUT_ACCOUNT_ENTRY_LABELS
                            ) + r")$",
                            re.IGNORECASE,
                        )
                        entries = dialog.locator(
                            'button, [role="button"], [role="menuitem"]'
                        ).filter(has_text=entry_pattern)
                        entry = await self._first_visible(entries, maximum=12)
                        if entry is not None:
                            try:
                                await entry.click(timeout=5_000)
                                clicked_about_entry = True
                                break
                            except Exception:
                                pass
                    # The dialog title often paints before its location row. Keep the
                    # same dialog open and wait for that row instead of saving unknown.
                    if is_about_surface:
                        current_text, panel_loading = await self._location_surface_snapshot(dialog)
                        if is_about_account_load_failure(current_text):
                            raise WorkerExecutionError(
                                "Instagram 未能加载账户所在地详情",
                                reason="instagram_location_load_failed", pause_required=True,
                                status_code=503,
                            )
                        current_location = extract_about_account_location(current_text)
                        if current_location is not None:
                            await self._validate_location_target(username_norm)
                            return current_location
                        if _about_account_location_is_undisclosed(current_text):
                            await self._finish_unknown_location(username_norm)
                            return None
                        panel_loading = panel_loading or any(
                            line.casefold().strip(" .…:：") in {
                                "loading", "loading account information", "正在加载", "加载中",
                                "正在載入", "載入中",
                            }
                            for line in current_text.splitlines()
                        )
                        loading_about_surface = loading_about_surface or panel_loading
                        settled_about_surface = settled_about_surface or not panel_loading
                if attempt < _LOCATION_DIALOG_POLL_ATTEMPTS - 1:
                    try:
                        await self.page.wait_for_timeout(
                            _LOCATION_DIALOG_POLL_MILLISECONDS
                        )
                    except Exception:
                        await asyncio.sleep(
                            _LOCATION_DIALOG_POLL_MILLISECONDS / 1000
                        )
            if settled_about_surface and not loading_about_surface:
                # Only the About panel's loading state matters here. A recommendation
                # spinner in the underlying profile is unrelated to an absent country.
                await self._finish_unknown_location(username_norm)
                return None
            if loading_about_surface:
                raise WorkerExecutionError(
                    "Instagram 账户地区详情仍未加载完成",
                    reason="instagram_location_load_failed", pause_required=True,
                    status_code=503,
                )
            transient_reason = await self._profile_transport_failure(username_norm)
            if transient_reason:
                raise WorkerExecutionError(
                    "Instagram profile disconnected while reading location",
                    reason=transient_reason,
                    pause_required=transient_reason != "instagram_content_not_visible",
                    status_code=503 if transient_reason != "instagram_content_not_visible" else 409,
                )
            return None
        except WorkerExecutionError:
            raise
        except Exception as exc:
            transient_reason = await self._profile_transport_failure(username_norm)
            if transient_reason:
                raise WorkerExecutionError(
                    "Instagram profile failed while reading location",
                    reason=transient_reason,
                    pause_required=transient_reason != "instagram_content_not_visible",
                    status_code=503 if transient_reason != "instagram_content_not_visible" else 409,
                ) from exc
            return None
        finally:
            await self._dismiss_profile_information_surfaces()

    async def _collect_relation(
        self, target: str, *, relation: Literal["followers", "following"],
        limit: int | None, strict_source_total: bool = False, monitor_observation: bool = False,
        candidate_sink: CandidateBatchSink | None = None,
        initial_candidate_count: int = 0, progress_sink: CollectionProgressSink | None = None,
        initial_resume_tail: Iterable[str] = (),
        initial_pending_relation_usernames: Iterable[str] = (),
        hover_precheck: bool = False,
    ) -> CollectionOutcome:
        # Carry the durable source cursor into the one replacement attempt.
        count = initial_candidate_count
        tail = list(initial_resume_tail)
        pending_names = list(initial_pending_relation_usernames)
        replacing = False
        made_progress = False

        async def tracked_progress(payload: dict[str, Any]) -> None:
            nonlocal count, tail, pending_names, made_progress
            observed = payload.get("discovered_count")
            advanced = isinstance(observed, int) and not isinstance(observed, bool) and observed > count
            if progress_sink is not None:
                await progress_sink(payload)
            if isinstance(observed, int) and not isinstance(observed, bool):
                count = max(count, observed)
            if isinstance(payload.get("resume_tail"), (list, tuple)):
                tail = list(payload["resume_tail"])[-5:]
            if isinstance(payload.get("pending_relation_usernames"), list):
                pending_names = list(payload["pending_relation_usernames"])
            if replacing and advanced:
                made_progress = True
                await self._finish_page_recovery(progressed=True)

        async def attempt() -> CollectionOutcome:
            self.last_relation_surface_diagnostics = {}
            self.last_relation_row_diagnostics = {}
            self.last_relation_scroll_diagnostics = {}
            self.last_relation_hover_diagnostics = {}
            try:
                return await self._collect_relation_once(
                    target, relation=relation, limit=limit,
                    strict_source_total=strict_source_total,
                    **({"monitor_observation": True} if monitor_observation else {}),
                    candidate_sink=candidate_sink,
                    initial_candidate_count=count, progress_sink=tracked_progress,
                    initial_resume_tail=tail,
                    initial_pending_relation_usernames=pending_names,
                    hover_precheck=hover_precheck,
                    capture_source_profile=progress_sink is not None,
                )
            except WorkerExecutionError as exc:
                for key, value in self._relation_diagnostics().items():
                    exc.details.setdefault(key, value)
                raise

        if monitor_observation:
            # The monitor manager alone owns its optional second read.
            await self._monitor_checkpoint()
            return await attempt()
        username_norm, _ = normalize_instagram_username(target)
        list_stalls = {"instagram_followers_list_incomplete", "instagram_following_list_incomplete",
                       "instagram_followers_list_not_rendered", "instagram_following_list_not_rendered"}
        fresh_reason = self._fresh_page_retry_targets.pop(username_norm, None)
        if fresh_reason:
            recovery_error = WorkerExecutionError("尝试新的同账号采集页", reason=fresh_reason)
        else:
            try:
                return await attempt()
            except WorkerExecutionError as exc:
                # Diagnose a stalled list in a new same-account page before any
                # in-place retry. Login/challenge/rate-limit guards are unchanged.
                if exc.details.get('recovery_scope') == 'screening_child':
                    raise
                if exc.code not in _PROFILE_TAB_RECOVERY_REASONS | list_stalls:
                    raise
                recovery_error = exc
        await self._replace_stuck_page_once(username_norm, recovery_error)
        self._forget_profile_attempt(username_norm)
        replacing = True
        try:
            result = await attempt()
            made_progress = True
            return result
        except WorkerExecutionError as exc:
            if exc.details.get('recovery_scope') == 'screening_child' or exc.code in _PROFILE_RECOVERY_AUTHORITATIVE_REASONS or (made_progress and exc.code in list_stalls):
                raise
            raise self._page_recovery_exhausted(exc, username_norm) from exc
        finally:
            await self._finish_page_recovery(progressed=made_progress)

    async def _reconcile_pending_relation_usernames(self, names: Iterable[str]) -> list[str]:
        """Recover the sink-ack/checkpoint-clear crash window without counting twice."""
        pending = list(dict.fromkeys(name for value in names if isinstance(value, str)
            and re.fullmatch(r'[a-zA-Z0-9._]{1,30}', name := value.strip().lstrip('@').casefold())))
        confirmed_check = getattr(self, 'relation_pending_confirmed_usernames', None)
        if pending and callable(confirmed_check):
            confirmed = await self._await_collection_callback(confirmed_check, pending)
            if not isinstance(confirmed, (list, tuple, set)) or any(not isinstance(name, str) for name in confirmed):
                raise RuntimeError('Pending relation confirmation did not return usernames')
            confirmed = set(confirmed)
            pending = [name for name in pending if name not in confirmed]
        duplicate_check = getattr(self, 'hover_duplicate_check', None)
        if callable(duplicate_check):
            unresolved = []
            for name in pending:
                if not await self._await_collection_callback(duplicate_check, name):
                    unresolved.append(name)
            pending = unresolved
        return pending

    async def _collect_relation_once(
        self,
        target: str,
        *,
        relation: Literal["followers", "following"],
        limit: int | None,
        strict_source_total: bool = False,
        monitor_observation: bool = False,
        candidate_sink: CandidateBatchSink | None = None,
        initial_candidate_count: int = 0,
        progress_sink: CollectionProgressSink | None = None,
        initial_resume_tail: Iterable[str] = (),
        initial_pending_relation_usernames: Iterable[str] = (),
        hover_precheck: bool = False,
        capture_source_profile: bool = True,
    ) -> CollectionOutcome:
        initial_pending_relation_usernames = tuple(initial_pending_relation_usernames)
        if initial_pending_relation_usernames and not monitor_observation:
            reconciled = await self._reconcile_pending_relation_usernames(initial_pending_relation_usernames)
            if tuple(reconciled) != initial_pending_relation_usernames and progress_sink is not None:
                await progress_sink({'pending_relation_usernames': reconciled})
            initial_pending_relation_usernames = tuple(reconciled)
        if monitor_observation:
            # A failed read may still have confirmed rows. Keep only this attempt's
            # observation for the manager's one supplemental read; it is not a
            # completed baseline and must still pass the manager's completion gate.
            self.last_relation_partial_usernames: list[str] = []
            self.last_relation_source_total: int | None = None
        if limit is not None and limit < 1:
            raise ValidationError("Collection limit must be at least 1")
        initial_candidate_count = max(0, int(initial_candidate_count))
        resume_tail = tuple(
            dict.fromkeys(
                value
                for raw_value in initial_resume_tail
                if (value := extract_instagram_profile_username(str(raw_value)))
                or (value := str(raw_value).strip().lstrip("@").casefold())
            )
        )[-5:]
        if (
            candidate_sink is not None
            and limit is not None
            and initial_candidate_count >= limit
        ):
            return CollectionOutcome(
                mode=relation,
                usernames=[],
                candidate_count=initial_candidate_count,
            )
        username_norm, _ = normalize_instagram_username(target)
        paused = self._paused_relation_surface
        self._paused_relation_surface = None
        resume_in_place = bool(
            paused and paused[0] is self.page and paused[1:3] == (username_norm, relation)
            and candidate_sink is not None and not hover_precheck and not monitor_observation
        )
        if resume_in_place:
            # Child failure is not a failed source list. Keep its exact live
            # surface and reread the current frame through durable deduplication.
            await self._guard()
            self._assert_relation_source(username_norm, relation)
            visible_count = paused[4]
        else:
            await self._await_page_stage(self._navigate_profile(target), timeout=self.profile_read_timeout_seconds)
            self._assert_relation_source(username_norm, relation)
            visible_count_result = await self._visible_relation_count(username_norm, relation)
            visible_count = (visible_count_result.count if isinstance(visible_count_result, _VisibleRelationCount)
                             else visible_count_result)
        if monitor_observation:
            self.last_relation_source_total = visible_count
        if progress_sink is not None:
            initial_progress: dict[str, Any] = {
                "mode": relation,
                "source_total": visible_count,
                "discovered_count": initial_candidate_count,
            }
            if capture_source_profile and not resume_in_place:
                source_profile = await self._source_profile_metrics(
                    username_norm, relation=relation, source_total=visible_count,
                )
                if source_profile:
                    initial_progress["source_profile"] = source_profile
            if resume_tail:
                initial_progress["resume_tail"] = list(resume_tail)
            try:
                await progress_sink(initial_progress)
            except Exception:
                if resume_in_place:
                    self._paused_relation_surface = paused
                raise
        if visible_count == 0:
            # A rendered zero count is explicit evidence of a genuinely empty list.
            self._assert_relation_source(username_norm, relation)
            if initial_pending_relation_usernames and not monitor_observation:
                raise WorkerExecutionError(
                    '列表显示为空，但仍有已看到且未确认的账号，已保留进度等待恢复',
                    reason=f'instagram_{relation}_list_incomplete',
                    pause_required=True, status_code=503,
                )
            return CollectionOutcome(
                mode=relation,
                usernames=[],
                candidate_count=initial_candidate_count if candidate_sink else None,
                source_total=0,
            )
        effective_limit = (
            int(visible_count)
            if strict_source_total and visible_count is not None
            else limit
        )
        if monitor_observation:
            await self._monitor_checkpoint()
        surface = paused[3] if resume_in_place else await self._open_relation_surface(username_norm, relation)
        self._assert_relation_source(username_norm, relation)
        if monitor_observation:
            # Let the newly opened following dialog render before touching its
            # first batch. Applied to both the initial and optional second read.
            await asyncio.sleep(3.0)
            await self._monitor_checkpoint()
            await self._guard()
        if not resume_in_place:
            # Start from the real first row only after Instagram has had time to
            # render its initial virtualized batch.  A reused dialog can otherwise
            # retain an intermediate scroll offset and silently omit early accounts.
            try:
                reset_deadline = self._collection_monotonic() + self.collection_loading_grace_seconds
                while True:
                    if monitor_observation:
                        await self._monitor_checkpoint()
                    await self._collection_checkpoint()
                    reset_result = await self._await_page_stage(
                        surface.evaluate(relation_scroll_script("reset")),
                        timeout=self.collection_dom_operation_timeout_seconds,
                    )
                    self.last_relation_scroll_diagnostics = {
                        **self._safe_relation_diagnostics(reset_result), "stage": "reset",
                    }
                    invalid_reset = isinstance(reset_result, dict) and (
                        reset_result.get("valid") is False
                        or (
                            isinstance(reset_result.get("top"), (int, float))
                            and reset_result["top"] > 3
                        )
                    )
                    if not invalid_reset:
                        break
                    # Skeleton rows do not yet reveal their scrolling ancestor. Give
                    # them the normal loading grace without reading from the middle.
                    if self._collection_monotonic() >= reset_deadline:
                        raise WorkerExecutionError(
                            "关注/粉丝列表未能回到顶部，请重新检查",
                            reason=f"instagram_{relation}_list_not_rendered",
                            pause_required=False,
                        )
                    await asyncio.sleep(self.collection_poll_interval_seconds)
                    await self._guard()
            except WorkerExecutionError:
                raise
            except Exception as exc:
                self._raise_page_failure(
                    exc, operation="reset Instagram relationship list to first row"
                )
                raise WorkerExecutionError(
                    "关注/粉丝列表未能回到顶部，请重新检查",
                    reason=f"instagram_{relation}_list_not_rendered",
                    pause_required=False,
                ) from exc
            await asyncio.sleep(
                self.collection_poll_interval_seconds if monitor_observation
                else max(0.2, min(1.2, self.collection_poll_interval_seconds * 2))
            )
        await self._guard()
        observed_total = initial_candidate_count

        def preserve_callback_failure(exc: Exception) -> None:
            # Local queue/storage/child callbacks do not invalidate the source
            # DOM. Resume still verifies this page's identity and source URL.
            if not monitor_observation and (
                not hover_precheck or isinstance(exc, WorkerExecutionError)
                and exc.details.get('recovery_scope') == 'screening_child'
            ):
                self._paused_relation_surface = (self.page, username_norm, relation, surface, visible_count)

        async def tracking_sink(batch: list[str], previews: dict[str, dict] | None = None) -> int | dict[str, Any]:
            nonlocal observed_total
            assert candidate_sink is not None
            try:
                acknowledgement = await candidate_sink(batch, previews) if hover_precheck else await candidate_sink(batch)
            except Exception as exc:
                preserve_callback_failure(exc)
                raise
            candidate_total = (
                acknowledgement.get("total")
                if isinstance(acknowledgement, dict)
                else acknowledgement
            )
            if isinstance(candidate_total, bool) or not isinstance(candidate_total, int):
                raise RuntimeError("Candidate batch sink did not return an integer total")
            observed_total = max(observed_total, candidate_total)
            if progress_sink is not None:
                try:
                    await progress_sink(
                        {
                            "mode": relation,
                            "source_total": visible_count,
                            "discovered_count": observed_total,
                        }
                    )
                except Exception as exc:
                    preserve_callback_failure(exc)
                    raise
            return acknowledgement

        async def scan_progress_sink(payload: dict[str, Any]) -> None:
            if progress_sink is None:
                return
            update = {
                "mode": relation,
                "source_total": visible_count,
                "discovered_count": observed_total,
                **payload,
            }
            try:
                await progress_sink(update)
            except Exception as exc:
                preserve_callback_failure(exc)
                raise

        usernames = await self._read_visible_account_dialog(
            surface,
            effective_limit,
            exclude={username_norm},
            **({"monitor_observation": True} if monitor_observation else {}),
            known_zero=visible_count == 0,
            surface_kind=relation,
            expected_source_username=username_norm,
            candidate_sink=tracking_sink if candidate_sink is not None else None,
            hover_precheck=hover_precheck,
            initial_candidate_count=initial_candidate_count,
            candidate_total_limit=limit,
            expected_minimum=(
                None if monitor_observation else
                int(visible_count)
                if strict_source_total and visible_count is not None
                else min(limit, visible_count)
                if visible_count is not None and limit is not None
                else visible_count
                if visible_count is not None
                else None
            ),
            initial_resume_tail=resume_tail,
            initial_pending_relation_usernames=initial_pending_relation_usernames,
            scan_progress_sink=scan_progress_sink if progress_sink is not None else None,
        )
        await self._guard()
        self._assert_relation_source(username_norm, relation)
        if candidate_sink is None and progress_sink is not None:
            await progress_sink(
                {
                    "mode": relation,
                    "source_total": visible_count,
                    "discovered_count": len(usernames),
                }
            )
        return CollectionOutcome(
            mode=relation,
            usernames=usernames,
            candidate_count=observed_total if candidate_sink is not None else None,
            source_total=visible_count,
        )

    @staticmethod
    def _names_after_resume_tail(
        usernames: list[str], resume_tail: tuple[str, ...]
    ) -> list[str]:
        """Replay visible rows through durable deduplication.

        A saved tail is a location hint, not a boundary: newly inserted or reordered
        accounts before it still need to reach the candidate spool.
        """
        return usernames

    def _collection_monotonic(self) -> float:
        """Clock seam used by accelerated long-duration relation tests."""

        return asyncio.get_running_loop().time() - self._monitor_paused_seconds - self._collection_paused_seconds

    async def _await_collection_callback(self, callback, *args):
        # SQLite writes and operator pauses are not website loading time.
        before = asyncio.get_running_loop().time()
        try:
            return await callback(*args)
        finally:
            self._collection_paused_seconds += asyncio.get_running_loop().time() - before

    async def _collection_checkpoint(self):
        if self.collection_checkpoint is not None:
            await self._await_collection_callback(self.collection_checkpoint)

    async def _monitor_checkpoint(self) -> None:
        if self.monitor_checkpoint is not None:
            before = asyncio.get_running_loop().time()
            await self.monitor_checkpoint()
            # User pauses must not consume the list-loading or row-render budget.
            self._monitor_paused_seconds += asyncio.get_running_loop().time() - before

    async def _read_monitor_following_hrefs(self, dialog: Any, *, exclude: set[str]) -> list[str]:
        """Read each account and its own button atomically; no naked-href fallback."""
        deadline = self._collection_monotonic() + 3.0
        while True:
            await self._monitor_checkpoint()
            try:
                rows = await self._await_page_stage(
                    dialog.evaluate(_MONITOR_FOLLOWING_ROWS_SCRIPT),
                    timeout=self.collection_dom_operation_timeout_seconds,
                )
            except Exception as exc:
                self._raise_page_failure(exc, operation="read following row buttons")
                raise WorkerExecutionError(
                    "无法读取关注列表的账号与按钮，请重新检查",
                    reason="instagram_following_list_not_rendered", pause_required=False,
                ) from exc
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise WorkerExecutionError(
                    "关注列表按钮数据无效，请重新检查",
                    reason="instagram_following_list_not_rendered", pause_required=False,
                )
            confirmed: dict[str, str] = {}
            pending: set[str] = set()
            resolved: set[str] = set()
            for row in rows:
                href = str(row.get("href") or "")
                username = extract_instagram_profile_username(href)
                if not username or username in exclude or row.get("recommended"):
                    continue
                state = following_row_button_state(row.get("actions") or [])
                if state == "following" and row.get("has_row"):
                    confirmed[username] = href
                    resolved.add(username)
                elif state == "not_following":
                    resolved.add(username)
                elif row.get("has_row"):
                    pending.add(username)
            # A late button in a partly loaded batch gets time to render before
            # scrolling; unrecognised buttons never qualify as a following row.
            if not pending - resolved or self._collection_monotonic() >= deadline:
                self._monitor_recommendations_reached = any(row.get("recommended") for row in rows)
                # The monitor may read preloaded rows outside the viewport.
                # Keep its button-qualified results, but acknowledge scrolling
                # only using identities actually visible at this position.
                self._monitor_visible_relation_names = {
                    name for row in rows
                    if row.get('viewport_visible') and not row.get('recommended')
                    and (name := extract_instagram_profile_username(str(row.get('href') or '')))
                    and name not in exclude
                } if any('viewport_visible' in row for row in rows) else None
                return list(confirmed.values())
            await asyncio.sleep(self.collection_poll_interval_seconds)

    async def _read_visible_account_hrefs(
        self,
        dialog: Any,
        *,
        surface_kind: Literal["followers", "following", "post_likers"],
        monitor_observation: bool = False,
        exclude: set[str] | None = None,
    ) -> list[str]:
        """Read one rendered relation batch in one CDP round-trip.

        The previous per-anchor get_attribute loop issued thousands of sequential
        protocol calls as a large list grew.  Production locators use evaluate_all;
        the bounded fallback keeps compatibility with lightweight custom/test
        locators that implement only count/nth/get_attribute.
        """

        self._relation_positioned_rows = None
        if monitor_observation and surface_kind == "following":
            return await self._read_monitor_following_hrefs(dialog, exclude=exclude or set())
        self._relation_recommendations_reached = False
        # Read labels and links atomically, before the generic href compatibility
        # path. Instagram appends suggested accounts to the same follower dialog;
        # those are not source candidates, even though their hrefs look identical.
        if surface_kind in {"followers", "following"}:
            evaluate = getattr(dialog, "evaluate", None)
            if callable(evaluate):
                try:
                    snapshot = await self._await_page_stage(
                        evaluate(RELATION_ROWS_SCRIPT),
                        timeout=self.collection_dom_operation_timeout_seconds,
                    )
                except (AttributeError, NotImplementedError):
                    snapshot = None
                except WorkerExecutionError as exc:
                    if exc.code != "browser_window_surface_unstable":
                        raise
                    raise WorkerExecutionError(
                        "Instagram relationship DOM stopped responding; progress was preserved",
                        reason=f"instagram_{surface_kind}_list_incomplete",
                        pause_required=True, status_code=503,
                    ) from exc
                except Exception as exc:
                    self._raise_page_failure(exc, operation="read Instagram relationship recommendations")
                    raise WorkerExecutionError(
                        "Instagram relationship rows could not be inspected",
                        reason=f"instagram_{surface_kind}_list_not_rendered",
                        pause_required=False,
                    ) from exc
                if isinstance(snapshot, dict) and isinstance(snapshot.get("hrefs"), list):
                    positions = snapshot.get("positioned_rows")
                    if isinstance(positions, list):
                        self._relation_positioned_rows = positions
                    self.last_relation_row_diagnostics = {
                        **self._safe_relation_diagnostics(snapshot.get("diagnostics")), "stage": "rows",
                    }
                    if any(not isinstance(href, str) for href in snapshot["hrefs"]):
                        raise WorkerExecutionError(
                            "Instagram relationship row data is invalid",
                            reason=f"instagram_{surface_kind}_list_not_rendered",
                            pause_required=False,
                        )
                    self._relation_recommendations_reached = snapshot.get("recommendations_reached") is True
                    return [href.strip() for href in snapshot["hrefs"] if href.strip()]
                if snapshot is not None:
                    raise WorkerExecutionError(
                        "Instagram relationship row snapshot is invalid",
                        reason=f"instagram_{surface_kind}_list_not_rendered",
                        pause_required=False,
                    )
                # Older/custom adapters may expose only the pre-existing locator
                # projection. They retain its count/physical-bottom requirements;
                # an unsupported snapshot can never establish a recommendation end.
        try:
            links = dialog.locator(self.selectors.account_links)
        except Exception as exc:
            self._raise_page_failure(exc, operation="read Instagram relationship rows")
            raise WorkerExecutionError(
                "Instagram relationship DOM could not be inspected",
                reason=f"instagram_{surface_kind}_list_not_rendered",
                pause_required=False,
            ) from exc

        evaluate_all = getattr(links, "evaluate_all", None)
        if callable(evaluate_all):
            try:
                values = await self._await_page_stage(
                    evaluate_all(
                        """nodes => nodes.map(node =>
                          String(node.getAttribute('href') || node.href || ''))"""
                    ),
                    timeout=self.collection_dom_operation_timeout_seconds,
                )
            except WorkerExecutionError as exc:
                if exc.code != "browser_window_surface_unstable":
                    raise
                reason = (
                    f"instagram_{surface_kind}_list_incomplete"
                    if surface_kind in {"followers", "following"}
                    else "instagram_post_likers_list_not_rendered"
                )
                raise WorkerExecutionError(
                    "Instagram relationship DOM stopped responding; progress was preserved",
                    reason=reason,
                    pause_required=True,
                    status_code=503,
                ) from exc
            except Exception as exc:
                self._raise_page_failure(
                    exc, operation="read Instagram relationship rows"
                )
                raise WorkerExecutionError(
                    "Instagram relationship DOM could not be inspected",
                    reason=f"instagram_{surface_kind}_list_not_rendered",
                    pause_required=False,
                ) from exc
            return [str(value).strip() for value in values or [] if str(value).strip()]

        try:
            link_count = await links.count()
        except Exception as exc:
            self._raise_page_failure(exc, operation="read Instagram relationship rows")
            link_count = 0
        hrefs: list[str] = []
        for index in range(link_count):
            try:
                href = await links.nth(index).get_attribute("href")
            except Exception as exc:
                self._raise_page_failure(
                    exc, operation="read Instagram relationship row"
                )
                continue
            if href:
                hrefs.append(str(href).strip())
        return hrefs

    async def _read_relation_hover_preview(self, dialog: Any, username: str) -> dict | None:
        """Hover the exact visible row; use only a matching, complete profile card."""
        diagnostics = {"stage": "find_hover_row", "attempts": 0, "matched_links": 0,
                       "card_polls": 0, "cdp_dispatches": 0, "row_missing": 0,
                       "hover_input_failures": 0, "row_recycled": 0,
                       "stale_cards": 0}
        self.last_relation_hover_diagnostics = diagnostics

        async def matches_row(row: Any) -> bool:
            href = await self._await_page_stage(
                row.get_attribute('href'), timeout=self.cdp_command_timeout_seconds
            )
            return extract_instagram_profile_username(str(href or '')) == username

        async def clear_existing_card() -> bool:
            # The preceding account's portal can overlap this row. Move the
            # source page's pointer off the list before every new hover. A card
            # showing this or the preceding username must disappear before the
            # next row is hovered; an old portal can still intercept its input.
            pointer = getattr(getattr(self.page, 'mouse', None), 'move', None)
            session = self._cdp_session
            if session is None and not callable(pointer):
                return True
            previous = getattr(self, '_last_relation_hover_username', None)
            probe_names = list(dict.fromkeys(name for name in (previous, username) if name))

            async def visible_prior_card() -> bool:
                for name in probe_names:
                    existing = await self._await_page_stage(
                        self.page.evaluate(HOVER_PREVIEW_SCRIPT, name), timeout=0.8
                    )
                    if isinstance(existing, dict) and existing.get('username') == name:
                        return True
                return False

            prior_card = await visible_prior_card()
            if prior_card:
                diagnostics['stale_cards'] += 1
            if session is not None:
                diagnostics['cdp_dispatches'] += 1
                try:
                    await self._await_page_probe(
                        session.send('Input.dispatchMouseEvent', {
                            'type': 'mouseMoved', 'x': 1, 'y': 1,
                        }), timeout=self.cdp_command_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except WorkerExecutionError:
                    # A command that ignored cancellation can still move the
                    # pointer later. Reconnect before sending more page input.
                    raise
                except Exception:
                    if not callable(pointer):
                        raise
                    await self._await_page_stage(pointer(1, 1), timeout=2.0)
            else:
                await self._await_page_stage(pointer(1, 1), timeout=2.0)
            if not prior_card:
                return True
            # Inspect immediately after the pointer move. Ready portals need no
            # fixed 150ms delay; delayed ones still receive all eight waits and
            # the same .6s fallback pointer move as before.
            for poll in range(9):
                await self._collection_checkpoint()
                if not await visible_prior_card():
                    return True
                if poll == 4 and session is not None and callable(pointer):
                    # A popup may still be catching an older mouse location;
                    # retry through Playwright's pointer state as well.
                    await self._await_page_stage(pointer(1, 1), timeout=2.0)
                if poll < 8:
                    await asyncio.sleep(0.15)
            return False

        async def poll_card(attempts: int, row: Any) -> dict | None:
            for _ in range(attempts):
                await self._collection_checkpoint()
                diagnostics["card_polls"] += 1
                preview = await self._await_page_stage(
                    self.page.evaluate(HOVER_PREVIEW_SCRIPT, username), timeout=0.8
                )
                if isinstance(preview, dict) and preview.get('username') == username:
                    # The relation list may recycle the hovered element during
                    # a delayed popup. Never credit a card after losing its row.
                    if not await matches_row(row):
                        diagnostics['row_recycled'] += 1
                        return None
                    diagnostics["stage"] = "confirmed"
                    return preview
                await asyncio.sleep(0.18)
            return None

        for attempt in range(2):
            diagnostics["attempts"] = attempt + 1
            pinned_handles: list[Any] = []
            try:
                # Child screening tabs may be navigating concurrently. Keep the
                # source page's own renderer active without switching the user's
                # foreground tab or touching another task's window.
                await self._ensure_window_surface_stable()
                # Instagram sometimes appends a query string or omits the last slash.
                # Match the normalized href before moving the pointer, not a single
                # exact CSS attribute that may miss a visibly rendered list row.
                links = dialog.locator(f'a[href*="/{username}" i]')
                row = None
                if callable(getattr(links, 'evaluate_all', None)):
                    all_hrefs = await self._await_page_stage(
                        links.evaluate_all('nodes => {' + _RELATION_VISIBLE + r'''
                          // Keep indices stable while excluding hidden duplicate
                          // avatar/name anchors. Hovering one would auto-scroll
                          // away from the frame that discovery actually read.
                          return nodes.map(node => visibleRow(node) ? node.getAttribute('href') : null);
                        }'''), timeout=self.collection_dom_operation_timeout_seconds,
                    )
                    diagnostics["matched_links"] = len(all_hrefs)
                    for index, href in enumerate(all_hrefs):
                        if extract_instagram_profile_username(str(href or '')) == username:
                            candidate = links.nth(index)
                            # A virtual list can repaint between snapshot and
                            # locator binding. Verify before dispatching input.
                            # A locator's nth(index) can resolve to a different
                            # account on the next call when the list virtualizes.
                            # Pin the physical element through hover and bounds.
                            handle_method = getattr(candidate, 'element_handle', None)
                            pinned = await self._await_page_stage(
                                handle_method(timeout=1800), timeout=2.1
                            ) if callable(handle_method) else candidate
                            if pinned is not candidate and pinned is not None:
                                pinned_handles.append(pinned)
                            if pinned is not None and await matches_row(pinned):
                                row = pinned
                                break
                else:
                    count = await self._await_page_stage(
                        links.count(), timeout=self.cdp_command_timeout_seconds
                    )
                    diagnostics["matched_links"] = count
                    for index in range(count):
                        candidate = links.nth(index)
                        handle_method = getattr(candidate, 'element_handle', None)
                        pinned = await self._await_page_stage(
                            handle_method(timeout=1800), timeout=2.1
                        ) if callable(handle_method) else candidate
                        if pinned is not candidate and pinned is not None:
                            pinned_handles.append(pinned)
                        if pinned is not None and await matches_row(pinned):
                            row = pinned
                            break
                if row is None:
                    diagnostics["row_missing"] += 1
                    if not attempt:
                        await asyncio.sleep(0.18)
                    continue
                if not await clear_existing_card():
                    # A matching card already existed and did not react to a
                    # pointer move. Its counts cannot certify this row's hover.
                    continue
                diagnostics["stage"] = "hover_input"
                try:
                    if not await matches_row(row):
                        diagnostics['row_recycled'] += 1
                        continue
                    await self._await_page_stage(row.hover(timeout=1800), timeout=2.1)
                    if not await matches_row(row):
                        diagnostics['row_recycled'] += 1
                        continue
                except Exception as exc:
                    self._raise_page_failure(exc, operation="hover Instagram relationship row")
                    diagnostics["hover_input_failures"] += 1
                else:
                    diagnostics["stage"] = "read_hover_card"
                    preview = await poll_card(16, row)
                    if preview is not None:
                        self._last_relation_hover_username = username
                        return preview
                # Playwright hovered the correct row but its background tab may
                # not have delivered the mouse event to Instagram. Dispatch once
                # through this page's own CDP session.
                session = self._cdp_session
                if session is not None:
                    if not await matches_row(row):
                        diagnostics['row_recycled'] += 1
                        continue
                    bounds = await self._await_page_stage(
                        row.bounding_box(), timeout=self.cdp_command_timeout_seconds
                    )
                    if not bounds and callable(getattr(row, 'evaluate', None)):
                        # display:contents anchors can expose a visible avatar or
                        # text without an anchor box of their own.
                        bounds = await self._await_page_stage(row.evaluate("""link => {
                          for (const child of link.querySelectorAll('*')) {
                            const r = child.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0)
                              return {x:r.x, y:r.y, width:r.width, height:r.height};
                          }
                          const r = document.createRange();
                          r.selectNodeContents(link);
                          const box = [...r.getClientRects()].find(box => box.width > 0 && box.height > 0);
                          return box ? {x:box.x, y:box.y, width:box.width, height:box.height} : null;
                        }"""), timeout=self.cdp_command_timeout_seconds)
                    if bounds and bounds['width'] > 0 and bounds['height'] > 0:
                        if not await matches_row(row):
                            diagnostics['row_recycled'] += 1
                            continue
                        diagnostics["stage"] = "cdp_hover_input"
                        diagnostics["cdp_dispatches"] += 1
                        await self._await_page_probe(
                            session.send('Input.dispatchMouseEvent', {
                                'type': 'mouseMoved',
                                'x': bounds['x'] + bounds['width'] / 2,
                                'y': bounds['y'] + bounds['height'] / 2,
                            }), timeout=self.cdp_command_timeout_seconds,
                        )
                        preview = await poll_card(6, row)
                        if preview is not None:
                            self._last_relation_hover_username = username
                            return preview
            except asyncio.CancelledError:
                raise
            except WorkerExecutionError:
                raise
            except Exception as exc:
                self._raise_page_failure(exc, operation="read Instagram relationship hover card")
                # A virtualized row can detach between the list snapshot and
                # hover. Rebind it once against the live list before pausing.
                if attempt:
                    break
            finally:
                for handle in pinned_handles:
                    dispose = getattr(handle, 'dispose', None)
                    if callable(dispose):
                        try:
                            await self._await_lifecycle_operation(
                                dispose(), timeout=self.cdp_command_timeout_seconds,
                            )
                        except Exception:
                            # A closed page has already released its JS handles.
                            pass
        diagnostics["stage"] = "unavailable"
        return None

    async def _read_visible_account_dialog(
        self,
        dialog: Any,
        limit: int | None,
        *,
        exclude: set[str] | None = None,
        monitor_observation: bool = False,
        known_zero: bool = False,
        surface_kind: Literal["followers", "following", "post_likers"] = "followers",
        expected_source_username: str | None = None,
        candidate_sink: CandidateBatchSink | None = None,
        initial_candidate_count: int = 0,
        candidate_total_limit: int | None = None,
        surface_unique_limit: int | None = None,
        expected_minimum: int | None = None,
        initial_resume_tail: Iterable[str] = (),
        initial_pending_relation_usernames: Iterable[str] = (),
        scan_progress_sink: CollectionProgressSink | None = None,
        hover_precheck: bool = False,
    ) -> list[str]:
        collected: dict[str, None] = {}
        if monitor_observation:
            self._monitor_recommendations_reached = False
            self._monitor_visible_relation_names: set[str] | None = None
            self.last_relation_partial_usernames = []
        self._relation_recommendations_reached = False
        self._relation_end_measurement: dict[str, Any] | None = None
        excluded = {value.casefold() for value in (exclude or set())}
        pending_relation_names = dict.fromkeys(
            name for value in initial_pending_relation_usernames
            if isinstance(value, str)
            and re.fullmatch(r'[a-zA-Z0-9._]{1,30}', name := value.strip().lstrip('@').casefold())
            and name not in excluded
        )
        track_pending_relations = (candidate_sink is not None
            and surface_kind in {'followers', 'following'} and not monitor_observation
            and (hover_precheck or bool(pending_relation_names)))
        pending_signature: tuple[str, ...] | None = None

        async def publish_pending_relations() -> None:
            nonlocal pending_signature
            signature = tuple(pending_relation_names)
            if track_pending_relations and signature != pending_signature:
                if scan_progress_sink is not None:
                    await self._await_collection_callback(scan_progress_sink,
                        {'pending_relation_usernames': list(signature)})
                pending_signature = signature

        if track_pending_relations and pending_relation_names:
            pending_relation_names = dict.fromkeys(
                await self._reconcile_pending_relation_usernames(pending_relation_names))
            await publish_pending_relations()

        unchanged_rounds = 0
        spooled_total = max(0, int(initial_candidate_count))
        total_limit = (
            max(spooled_total + 1, int(candidate_total_limit))
            if candidate_total_limit is not None
            else None
        )
        normalized_resume_tail: list[str] = []
        for raw_value in initial_resume_tail:
            raw_text = str(raw_value or "").strip()
            normalized = extract_instagram_profile_username(raw_text)
            if normalized is None:
                normalized = raw_text.lstrip("@").casefold()
            if normalized and normalized not in normalized_resume_tail:
                normalized_resume_tail.append(normalized)
        last_tail_signature: tuple[str, ...] | None = (
            tuple(normalized_resume_tail[-5:]) or None
        )
        saw_account_this_scan = False
        confirmed_end_of_list = False
        progress_epoch = 0
        highest_scroll_top = 0.0
        row_settle_until = 0.0
        scroll_frame_names: set[str] | None = None
        self.last_relation_scroll = {"attempts": 0, "movements": 0, "invalid": 0}
        # Post-liker limits are per post, not one global allowance consumed by the
        # first post.  Only that surface opts into this bounded set. Relationship
        # lists use the durable total/tail signature and retain O(batch) memory.
        surface_seen: set[str] = set()
        # Only suppress rows acknowledged in the immediately preceding DOM frame.
        # A cumulative list otherwise re-submits its whole growing prefix on every
        # poll. This cache is local to one scan: restart/recovery still replays the
        # first frame through durable deduplication, and inserted/reappearing rows
        # are never hidden behind a saved tail position.
        acknowledged_frame: set[str] = set()
        pending_end_frame: list[str] | None = None
        end_refreshes_without_progress = 0
        end_budget_exhausted_after_frame = False
        hover_frame_repaints = 0
        position_guard: dict[str, Any] | None = None
        pending_neighbour_windows: dict[str, list[str]] = {}
        scroll_position_confirmed = False
        scroll_tail_clipped = False

        def assert_source() -> None:
            if expected_source_username is not None and surface_kind in {"followers", "following"}:
                self._assert_relation_source(expected_source_username, surface_kind)

        async def read_frame() -> list[str]:
            nonlocal position_guard, scroll_position_confirmed
            while True:
                hrefs = await self._read_visible_account_hrefs(
                    dialog, surface_kind=surface_kind,
                    **({"monitor_observation": True, "exclude": excluded} if monitor_observation else {}),
                )
                # Verify both source identity and the retained adjacent rows before
                # admitting this frame. New text alone cannot acknowledge a scroll.
                assert_source()
                if position_guard is None:
                    return hrefs
                result = relation_position_status(position_guard["anchors"],
                    getattr(self, "_relation_positioned_rows", None), position_guard["shift"])
                position_guard["checks"] += 1
                self.last_relation_scroll_diagnostics = {
                    "stage": "position_check", "continuity_status": result,
                    "position_checks": position_guard["checks"],
                    "expected_anchors": len(position_guard["anchors"]),
                }
                if result == "confirmed":
                    position_guard = None
                    scroll_position_confirmed = True
                    return hrefs
                if self._collection_monotonic() >= position_guard["deadline"]:
                    error = WorkerExecutionError(
                        "滚动前后的相邻账号位置无法对齐；已停止继续滚动并保留采集进度",
                        reason=f"instagram_{surface_kind}_list_incomplete",
                        pause_required=True, status_code=503,
                    )
                    error.details.update(self._relation_diagnostics())
                    raise error
                await self._collection_checkpoint()
                await asyncio.sleep(self.collection_poll_interval_seconds)
                await self._guard()

        def below_collection_limit() -> bool:
            if candidate_sink is None:
                return limit is None or len(collected) < limit
            if total_limit is not None and spooled_total >= total_limit:
                return False
            return surface_unique_limit is None or len(surface_seen) < surface_unique_limit

        wait_deadline = (
            self._collection_monotonic() + self.collection_loading_grace_seconds
        )
        while below_collection_limit():
            if monitor_observation:
                await self._monitor_checkpoint()
            await self._collection_checkpoint()
            assert_source()
            before = len(collected)
            before_total = spooled_total
            pending_batch: list[str] = []
            pending_batch_seen: set[str] = set()
            item_frame_names: list[str] | None = None
            neighbour_observed_hrefs: dict[str, None] = {}
            neighbour_observed_windows: dict[str, list[str]] = {}
            item_neighbour_windows, pending_neighbour_windows = pending_neighbour_windows, {}
            confirmed_item_names: set[str] = set()

            async def remember_pending_relations(hrefs: list[str]) -> None:
                if not track_pending_relations:
                    return
                for href in hrefs:
                    name = extract_instagram_profile_username(href)
                    if name and name not in excluded and name not in acknowledged_frame and name not in confirmed_item_names:
                        pending_relation_names.setdefault(name, None)
                await publish_pending_relations()

            async def confirm_item_neighbours(username: str) -> None:
                if item_frame_names is None:
                    return  # Legacy/text-only adapters have no positional snapshot.
                deadline = self._collection_monotonic() + max(1.5, self.collection_loading_grace_seconds)
                previous_changed = None
                while True:
                    await self._collection_checkpoint()
                    current_hrefs = await read_frame()
                    await remember_pending_relations(current_hrefs)
                    neighbour_observed_hrefs.update(dict.fromkeys(current_hrefs))
                    positions = [row for row in (getattr(self, '_relation_positioned_rows', None) or [])
                        if isinstance(row, dict) and row.get('recommended') is not True]
                    status = relation_neighbour_status(
                        item_neighbour_windows.get(username, item_frame_names), username, positions)
                    signature = tuple(row['username'] for row in positions
                        if isinstance(row, dict) and isinstance(row.get('username'), str))
                    for index, name in enumerate(signature):
                        neighbour_observed_windows.setdefault(name, list(signature[max(0, index - 1):index + 2]))
                    # A stable changed order is legitimate, provided every known
                    # neighbour survived. Newly seen identities are consumed in
                    # place after this batch, even if a later snapshot hides them.
                    if status == 'confirmed' or (status == 'neighbours_changed'
                            and signature == previous_changed):
                        return
                    previous_changed = signature if status == 'neighbours_changed' else None
                    if self._collection_monotonic() >= deadline:
                        raise WorkerExecutionError(
                            f'@{username} 的相邻账号暂时无法确认，已保留进度等待恢复',
                            reason=f'instagram_{surface_kind}_list_incomplete',
                            pause_required=True, status_code=503,
                        )
                    await asyncio.sleep(self.collection_poll_interval_seconds)
                    await self._guard()

            async def flush_pending_batch() -> None:
                nonlocal spooled_total
                if candidate_sink is None or not pending_batch:
                    return
                batch = list(pending_batch)
                pending_batch.clear()
                pending_batch_seen.clear()
                previews: dict[str, dict] = {}

                async def persist_confirmed(names: list[str]) -> None:
                    nonlocal spooled_total
                    evidence = {name: previews[name] for name in names} if hover_precheck else {}
                    acknowledgement = await self._await_collection_callback(
                        candidate_sink, names, evidence
                    ) if hover_precheck else await self._await_collection_callback(
                        candidate_sink, names
                    )
                    candidate_total = (
                        acknowledgement.get("total")
                        if isinstance(acknowledgement, dict)
                        else acknowledgement
                    )
                    if isinstance(candidate_total, bool) or not isinstance(candidate_total, int):
                        raise RuntimeError("Candidate batch sink did not return an integer total")
                    if candidate_total < 0:
                        raise RuntimeError("Candidate batch sink returned a negative total")
                    spooled_total = max(spooled_total, candidate_total)
                    confirmed_item_names.update(names)
                    for name in names:
                        pending_relation_names.pop(name, None)
                    await publish_pending_relations()

                if hover_precheck and surface_kind in {'followers', 'following'}:
                    capacity_check = getattr(self, 'hover_capacity_checkpoint', None)
                    for username in batch:
                        await self._collection_checkpoint()
                        assert_source()
                        duplicate_check = getattr(self, 'hover_duplicate_check', None)
                        if callable(duplicate_check) and await self._await_collection_callback(
                            duplicate_check, username
                        ):
                            await confirm_item_neighbours(username)
                            previews[username] = {'duplicate': True}
                            continue
                        if callable(capacity_check):
                            if previews:
                                await persist_confirmed(list(previews))
                                previews.clear()
                            # Pool waits are not Instagram loading time. The
                            # callback remains interruptible by Pause and Stop.
                            await self._await_collection_callback(capacity_check)
                            assert_source()
                        await confirm_item_neighbours(username)
                        preview = await self._read_relation_hover_preview(dialog, username)
                        assert_source()
                        if preview is None:
                            # The previously confirmed rows in this same DOM
                            # frame must reach the durable queue before pausing.
                            # Otherwise one missing card discards all their
                            # completed hover work and no child can start them.
                            if previews:
                                await persist_confirmed(list(previews))
                            raise WorkerExecutionError(
                                f'@{username} 的悬浮卡未能确认；已保留采集进度，请重试',
                                reason='instagram_hover_card_unavailable',
                                pause_required=True, status_code=503,
                            )
                        previews[username] = preview
                        # Commit every confirmed card before the next row can
                        # disconnect, be recycled or observe Stop. This also
                        # protects single-page/compatibility collection, which
                        # has no screening-capacity callback.
                        await persist_confirmed([username])
                        previews.pop(username, None)
                        # Hovering and durable writes yield to a virtualized DOM.
                        # Preserve the confirmed account first, then verify its
                        # neighbours again, including the final item in a batch.
                        await confirm_item_neighbours(username)
                    if previews:
                        await persist_confirmed(list(previews))
                else:
                    await persist_confirmed(batch)
                assert_source()

            if pending_end_frame is not None:
                # Consume the exact frame observed while confirming the bottom;
                # another DOM read could already have recycled these virtual rows.
                hrefs, pending_end_frame = pending_end_frame, None
            else:
                hrefs = await read_frame()
            await remember_pending_relations(hrefs)
            positions = getattr(self, '_relation_positioned_rows', None)
            if isinstance(positions, list) and hover_precheck and surface_kind in {'followers', 'following'}:
                item_frame_names = [row['username'] for row in positions
                    if isinstance(row, dict) and row.get('recommended') is not True
                    and isinstance(row.get('username'), str)]
            rendered_usernames: list[str] = []
            rendered_seen: set[str] = set()
            for href in hrefs:
                username_norm = extract_instagram_profile_username(href)
                if username_norm is None:
                    continue
                if username_norm in excluded:
                    continue
                if username_norm in rendered_seen:
                    continue
                rendered_seen.add(username_norm)
                rendered_usernames.append(username_norm)

            viewport_names = (
                self._monitor_visible_relation_names
                if monitor_observation and self._monitor_visible_relation_names is not None
                else rendered_seen
            )
            # Suggested accounts never enter the candidate batch or totals, but
            # their positioned rows can prove that a virtual frame repainted.
            # Near the real tail a forward move may reveal only suggestions while
            # clipping old real rows. Ignoring those rows would wait forever for
            # a new real identity even after the retained anchors have aligned.
            positioned_recommendation_names = {
                row["username"] for row in (getattr(self, "_relation_positioned_rows", None) or [])
                if isinstance(row, dict) and row.get("recommended") is True
                and isinstance(row.get("username"), str)
            } if not monitor_observation and surface_kind in {"followers", "following"} else set()

            if monitor_observation and not saw_account_this_scan and not rendered_usernames:
                # Skeleton rows are not an empty first batch to scroll past.
                # Keep the list at its initial position until real accounts
                # appear, bounded by the existing loading grace period.
                if self._collection_monotonic() >= wait_deadline:
                    break
                await asyncio.sleep(self.collection_poll_interval_seconds)
                await self._guard()
                continue

            if rendered_usernames:
                saw_account_this_scan = True
            tail_signature = tuple(rendered_usernames[-5:])
            tail_moved = bool(tail_signature) and tail_signature != last_tail_signature
            if candidate_sink is not None and surface_kind in {"followers", "following"}:
                rows_to_submit = [
                    username for username in self._names_after_resume_tail(
                        rendered_usernames, last_tail_signature or ()
                    ) if username not in acknowledged_frame
                ]
            else:
                rows_to_submit = rendered_usernames

            for username_norm in rows_to_submit:
                if candidate_sink is None:
                    if monitor_observation and username_norm not in collected:
                        self.last_relation_partial_usernames.append(username_norm)
                    collected.setdefault(username_norm, None)
                    if limit is not None and len(collected) >= limit:
                        break
                else:
                    if surface_unique_limit is not None:
                        if username_norm in surface_seen:
                            continue
                        surface_seen.add(username_norm)
                    if username_norm in pending_batch_seen:
                        continue
                    pending_batch.append(username_norm)
                    pending_batch_seen.add(username_norm)
                    # Keep I/O bounded without shrinking to one-row transactions.
                    # Post-liker callers may supply a finite total; relationship
                    # callers deliberately carry no maximum total.
                    if len(pending_batch) >= 100:
                        await flush_pending_batch()
                        if total_limit is not None and spooled_total >= total_limit:
                            break
                    if (
                        surface_unique_limit is not None
                        and len(surface_seen) >= surface_unique_limit
                    ):
                        break
            await flush_pending_batch()
            hover_repaint_pending = False
            if candidate_sink is not None and surface_kind in {"followers", "following"}:
                # Reached only after all submitted batches received valid durable
                # acknowledgements. A failed/cancelled write cannot confirm rows.
                acknowledged_frame = rendered_seen
            if candidate_sink is None:
                made_progress = len(collected) > before
                unchanged_rounds = (
                    unchanged_rounds + 1 if not made_progress else 0
                )
            else:
                made_progress = spooled_total > before_total or (
                    surface_kind == "post_likers" and tail_moved and bool(rows_to_submit)
                )
                unchanged_rounds = (
                    unchanged_rounds + 1
                    if not made_progress
                    else 0
                )
            if made_progress:
                end_refreshes_without_progress = 0
                end_budget_exhausted_after_frame = False
                # The grace period belongs to the most recent real DOM/list advance,
                # not to the moment the dialog first opened. A large healthy list can
                # therefore run for hours without aging out its one loading grace.
                wait_deadline = (
                    self._collection_monotonic()
                    + self.collection_loading_grace_seconds
                )
                progress_epoch += 1
                if scan_progress_sink is not None and tail_signature:
                    await self._await_collection_callback(scan_progress_sink,
                        {
                            "resume_tail": list(tail_signature),
                            "rendered_count": len(rendered_usernames),
                            "progress_epoch": progress_epoch,
                        }
                    )
            elif end_budget_exhausted_after_frame:
                # The final observed frame has now passed through the durable
                # sink. Only then may an exhausted replay budget stop the scan;
                # a genuinely new saved row above renews the ordinary budget.
                confirmed_end_of_list = False
                break
            if tail_signature:
                last_tail_signature = tail_signature
            if (
                candidate_sink is not None
                and surface_kind in {"followers", "following"}
                and rows_to_submit
                and below_collection_limit()
            ):
                # Both hover reads and durable queue writes can yield while
                # Instagram replaces virtual rows. Confirm the current frame
                # before scrolling even when hover filtering is disabled.
                # This consumes repainted rows in place, never by rewinding.
                # Progress callbacks may request Pause/Stop. Honor that request
                # before any extra DOM read, excluding pause time from the grace.
                await self._collection_checkpoint()
                assert_source()
                latest_hrefs = await read_frame()
                await remember_pending_relations(latest_hrefs)
                latest_names = {
                    name for href in latest_hrefs
                    if (name := extract_instagram_profile_username(href)) is not None
                    and name not in excluded
                }
                if latest_names != rendered_seen:
                    # Productive frames are normal in a virtualized list.
                    # Bound only consecutive replays of already saved rows.
                    hover_frame_repaints = (
                        0 if spooled_total > before_total else hover_frame_repaints + 1
                    )
                    if hover_frame_repaints > 3:
                        await self._raise_incomplete_relation_list(
                            surface_kind,
                            observed=spooled_total,
                            expected_minimum=max(spooled_total + 1, expected_minimum or 0),
                            surface=dialog,
                        )
                    pending_end_frame = latest_hrefs
                    hover_repaint_pending = True
                else:
                    hover_frame_repaints = 0
                # Do not lose an insertion seen between A and B just because it
                # disappeared again before the final batch refresh. Replay the
                # observed frame through the ordinary durable/hover checks before
                # any scroll; an unavailable pending row must pause, never vanish.
                unseen_hrefs = [href for href in neighbour_observed_hrefs
                    if (name := extract_instagram_profile_username(href)) is not None
                    and name not in excluded and name not in rendered_seen]
                if unseen_hrefs:
                    pending_end_frame = list(dict.fromkeys(latest_hrefs + unseen_hrefs))
                    pending_neighbour_windows = {
                        name: neighbour_observed_windows[name] for href in unseen_hrefs
                        if (name := extract_instagram_profile_username(href)) in neighbour_observed_windows
                    }
                    hover_repaint_pending = True
            if hover_repaint_pending:
                # Publish the durable cursor and renew the progress deadline
                # before consuming the replacement frame without scrolling.
                continue
            if monitor_observation and self._monitor_recommendations_reached:
                # All real rows above the heading have been consumed. Do not
                # scroll into recommendations after their heading is virtualized away.
                confirmed_end_of_list = True
                break
            if scroll_frame_names is not None:
                if (viewport_names - scroll_frame_names or (scroll_position_confirmed
                        and positioned_recommendation_names - scroll_frame_names)):
                    scroll_frame_names = None
                    scroll_position_confirmed = False
                elif below_collection_limit() and self._collection_monotonic() < row_settle_until:
                    # Scroll clipping can remove old rows before the virtual
                    # list paints replacements. A shortened/reordered tail is
                    # not a new frame. Keep reading at this position, and do
                    # this before idle/end checks so a shorter loading grace
                    # cannot abort the outstanding repaint wait.
                    await asyncio.sleep(self.collection_poll_interval_seconds)
                    await self._guard()
                    continue
                elif scroll_position_confirmed and scroll_tail_clipped:
                    # A small clamped move can retain exactly the same accounts
                    # at the inner bottom, with unread rows still clipped by an
                    # outer viewport. After waiting for repaint, the verified
                    # neighbour displacement permits that next forward move.
                    # Text-only adapters cannot use this positional evidence.
                    scroll_frame_names = None
                    scroll_position_confirmed = False
                    wait_deadline = max(wait_deadline, self._collection_monotonic()
                        + max(1.5, self.collection_loading_grace_seconds))
            # Use the same deadline decision for semantic-tail and generic idle
            # checks. Crossing the deadline between those checks must not route
            # a healthy recommendation-only tail through the blank-frame guard.
            no_progress_expired = self._collection_monotonic() >= wait_deadline
            if (
                not monitor_observation
                and surface_kind in {"followers", "following"}
                and self._relation_recommendations_reached
                and saw_account_this_scan
                and (
                    expected_minimum is None
                    or (spooled_total if candidate_sink is not None else len(collected)) >= expected_minimum
                    or no_progress_expired
                )
                and not await self._has_visible_relation_loading_indicator(dialog)
                and await self._confirm_relation_list_end(dialog)
            ):
                # Recommendations do not cancel the loading grace when source
                # rows are still missing. Stay at this position for delayed rows,
                # then accept a stable short tail without any automatic rewind.
                # Re-read after both measurements:
                # a virtualized repaint may expose final real rows during the wait.
                final_hrefs = await read_frame()
                await remember_pending_relations(final_hrefs)
                final_names = {
                    name for href in final_hrefs
                    if (name := extract_instagram_profile_username(href)) is not None
                    and name not in excluded
                }
                if (
                    self._relation_recommendations_reached
                    and final_names == rendered_seen
                    and await self._relation_list_end_is_current(dialog)
                ):
                    confirmed_end_of_list = True
                    break
                pending_end_frame = final_hrefs
                end_budget_exhausted_after_frame = bool(
                    self._collection_monotonic() >= wait_deadline
                    and end_refreshes_without_progress
                )
                end_refreshes_without_progress += 1
                continue
            idle_limit = (
                self.collection_initial_idle_rounds
                if not collected and not saw_account_this_scan
                else self.collection_settled_idle_rounds
            )
            if unchanged_rounds >= idle_limit:
                loading = await self._has_visible_relation_loading_indicator(dialog)
                observed = spooled_total if candidate_sink is not None else len(collected)
                enough_rows = expected_minimum is not None and observed >= expected_minimum
                expired = no_progress_expired
                # Missing/sporadic loaders must not shorten the loading grace.
                # A known count permits an early, measured natural-end check.
                if not loading and (enough_rows or expired):
                    confirmed_end_of_list = await self._confirm_relation_list_end(dialog)
                    # Counts are hints, not a completion quota. A short list has
                    # already used the full no-progress grace before this check;
                    # two healthy, spinner-free bottom measurements and the final
                    # durable frame below decide whether discovery has finished.
                    # Rejecting 164 at a 167 header here reopened the same finished
                    # list indefinitely, even after both measurements succeeded.
                    if confirmed_end_of_list:
                        if surface_kind == "post_likers":
                            break
                        # Geometry can stay fixed while virtual rows repaint during
                        # the two bottom measurements. Do not finish using names
                        # read before that wait: persist the final observed frame
                        # first, then prove the bottom again. Replayed old rows may
                        # consume one final refresh but cannot renew the no-progress
                        # grace forever by alternating at a stable scroll position.
                        final_hrefs = await read_frame()
                        await remember_pending_relations(final_hrefs)
                        final_names = {
                            name for href in final_hrefs
                            if (name := extract_instagram_profile_username(href)) is not None
                            and name not in excluded
                        }
                        if not final_names:
                            if monitor_observation and self._monitor_recommendations_reached:
                                # An explicit recommendation section is the
                                # monitor's established semantic end boundary,
                                # even if its final real rows were virtualized out.
                                break
                            confirmed_end_of_list = False
                            break
                        if final_names != rendered_seen:
                            confirmed_end_of_list = False
                            pending_end_frame = final_hrefs
                            end_budget_exhausted_after_frame = bool(
                                expired and end_refreshes_without_progress
                            )
                            end_refreshes_without_progress += 1
                            continue
                        confirmed_end_of_list = await self._relation_list_end_is_current(dialog)
                        if confirmed_end_of_list:
                            break
                if expired:
                    break
                unchanged_rounds = max(0, idle_limit - 1)
            if below_collection_limit():
                try:
                    self.last_relation_scroll["attempts"] += 1
                    position_rows = getattr(self, "_relation_positioned_rows", None)
                    positioned_collection = (not monitor_observation and
                        surface_kind in {"followers", "following"} and isinstance(position_rows, list))
                    acknowledged_names = ([row["username"] for row in position_rows
                        if isinstance(row, dict) and isinstance(row.get("username"), str)]
                        if positioned_collection else None)
                    movement = await self._await_page_stage(
                        dialog.evaluate(
                            relation_scroll_script("advance", acknowledged_names)
                        ),
                        timeout=self.collection_dom_operation_timeout_seconds,
                    )
                    self.last_relation_scroll_diagnostics = {
                        **self._safe_relation_diagnostics(movement), "stage": "advance",
                    }
                    if isinstance(movement, dict) and movement.get("valid") is False:
                        self.last_relation_scroll["invalid"] += 1
                        raise WorkerExecutionError(
                            "未找到关注/粉丝列表内部的滚动区域；已保留读取结果",
                            reason=f"instagram_{surface_kind}_list_not_rendered",
                            pause_required=False,
                        )
                    # Replaying saved rows is allowed while the actual viewport
                    # moves forward, but alternating old text is not new progress.
                    if isinstance(movement, dict) and movement.get("valid") is True and movement.get("moved") is True:
                        self.last_relation_scroll["movements"] += 1
                        scroll_tail_clipped = (movement.get("tail_clipped") is True
                            and all(type(movement.get(key)) in (int, float) for key in ("top", "client", "height"))
                            and movement["top"] + movement["client"] >= movement["height"] - 3)
                        row_settle_until = self._collection_monotonic() + max(
                            1.5, self.collection_loading_grace_seconds,
                        )
                        if positioned_collection:
                            scroll_position_confirmed = False
                            position_guard = {"anchors": movement.get("anchor_rows", []),
                                "shift": movement.get("scroll_shift", 0),
                                "deadline": row_settle_until, "checks": 0}
                        scroll_frame_names = set(viewport_names) | positioned_recommendation_names
                        top = movement.get("top")
                        if isinstance(top, (int, float)) and not isinstance(top, bool) and top > highest_scroll_top + 1:
                            highest_scroll_top = top
                            wait_deadline = max(
                                row_settle_until,
                                self._collection_monotonic() + self.collection_loading_grace_seconds,
                            )
                except WorkerExecutionError as exc:
                    if exc.code != "browser_window_surface_unstable":
                        raise
                    raise WorkerExecutionError(
                        "Instagram relationship list stopped responding while scrolling; progress was preserved",
                        reason=f"instagram_{surface_kind}_list_incomplete",
                        pause_required=True,
                        status_code=503,
                    ) from exc
                except asyncio.TimeoutError as exc:
                    raise WorkerExecutionError(
                        "Instagram relationship list stopped responding while scrolling; progress was preserved",
                        reason=f"instagram_{surface_kind}_list_incomplete",
                        pause_required=True,
                        status_code=503,
                    ) from exc
                except Exception as exc:
                    self._raise_page_failure(exc, operation="scroll Instagram relationship list")
                    raise WorkerExecutionError(
                        "Instagram relationship list could not scroll; progress was preserved",
                        reason=f"instagram_{surface_kind}_list_incomplete",
                        pause_required=True,
                        status_code=503,
                    ) from exc
                await asyncio.sleep(self.collection_poll_interval_seconds)
                await self._guard()
        assert_source()
        if track_pending_relations and pending_relation_names and confirmed_end_of_list:
            raise WorkerExecutionError(
                '仍有已看到但尚未确认的相邻账号，已保留进度等待恢复',
                reason=f'instagram_{surface_kind}_list_incomplete',
                pause_required=True, status_code=503,
            )
        if collected:
            if (
                surface_kind in {"followers", "following"}
                and (limit is None or len(collected) < limit)
                and not confirmed_end_of_list
            ):
                await self._raise_incomplete_relation_list(
                    surface_kind, observed=len(collected),
                    expected_minimum=max(len(collected) + 1, expected_minimum or 0),
                    surface=dialog,
                )
            if (
                expected_minimum is not None
                and len(collected) < expected_minimum
                and not confirmed_end_of_list
            ):
                await self._raise_incomplete_relation_list(
                    surface_kind,
                    observed=len(collected),
                    expected_minimum=expected_minimum,
                    surface=dialog,
                )
            return list(collected) if limit is None else list(collected)[:limit]
        if candidate_sink is not None and saw_account_this_scan:
            if (
                surface_kind in {"followers", "following"}
                and (total_limit is None or spooled_total < total_limit)
                and not confirmed_end_of_list
            ):
                # A quiet/unchanged virtualized list is not completion evidence.
                # Before the requested cap is reached, only a confirmed list end
                # can finish discovery; retain the durable spool and retry otherwise.
                await self._raise_incomplete_relation_list(
                    surface_kind,
                    observed=spooled_total,
                    expected_minimum=max(
                        spooled_total + 1,
                        expected_minimum or 0,
                    ),
                    surface=dialog,
                )
            if (
                expected_minimum is not None
                and spooled_total < expected_minimum
                and not confirmed_end_of_list
            ):
                await self._raise_incomplete_relation_list(
                    surface_kind,
                    observed=spooled_total,
                    expected_minimum=expected_minimum,
                    surface=dialog,
                )
            return []
        try:
            visible_text = await dialog.inner_text(timeout=5_000)
        except Exception as exc:
            self._raise_page_failure(exc, operation="read Instagram relationship surface")
            visible_text = ""
        if surface_kind == "post_likers":
            state = "empty" if known_zero or has_visible_zero_likes(visible_text) else "unrecognized"
        else:
            state = classify_visible_collection_surface(
                visible_text,
                parsed_accounts=0,
                known_zero=known_zero,
            )
        if state == "empty" and surface_kind == "post_likers":
            return []
        surface_reason = (
            await self._relation_surface_failure(dialog)
            if surface_kind in {"followers", "following"}
            else await self._page_surface_failure()
        )
        if surface_reason:
            self._raise_surface_reason(
                surface_reason,
                "Instagram relationship surface is incomplete; waiting without losing progress",
            )
        if state == "empty" and not await self._has_visible_relation_loading_indicator(dialog):
            assert_source()
            if track_pending_relations and pending_relation_names:
                raise WorkerExecutionError(
                    '列表显示为空，但仍有已看到且未确认的账号，已保留进度等待恢复',
                    reason=f'instagram_{surface_kind}_list_incomplete',
                    pause_required=True, status_code=503,
                )
            return []
        labels = {
            "followers": "粉丝",
            "following": "关注",
            "post_likers": "帖子点赞用户",
        }
        raise WorkerExecutionError(
            f"Instagram 页面已打开，但没有识别到{labels[surface_kind]}列表；任务已保留，可刷新后继续",
            reason=f"instagram_{surface_kind}_list_not_rendered",
            pause_required=False,
        )

    def _assert_relation_source(
        self, username_norm: str, relation: Literal["followers", "following"]
    ) -> None:
        """A healthy different profile is not evidence about the active source."""
        if self.page is None:
            return
        try:
            parsed = urlparse(str(self.page.url))
            path = parsed.path.strip("/").casefold()
        except Exception as exc:
            self._raise_page_failure(exc, operation="verify Instagram relationship source")
            path = ""
            parsed = None
        if (
            parsed is not None
            and parsed.hostname in {"instagram.com", "www.instagram.com"}
            and path in {username_norm.casefold(), f"{username_norm.casefold()}/{relation}"}
        ):
            return
        raise WorkerExecutionError(
            "Instagram relationship source changed during collection; progress was preserved",
            reason=f"instagram_{relation}_list_incomplete",
            pause_required=True,
            status_code=503,
        )

    async def _relation_list_end_is_current(self, dialog: Any) -> bool:
        """Revalidate the measured bottom after the last row snapshot.

        The snapshot itself can await a DOM repaint. A loader or taller viewport
        appearing then invalidates earlier geometry even if its names are unchanged.
        This last measurement has no settling sleep after it.
        """
        expected = self._relation_end_measurement
        if expected is None:
            # Small/custom adapters may override the older boolean confirmation.
            return await self._confirm_relation_list_end(dialog)
        try:
            current = await self._await_page_stage(
                dialog.evaluate(relation_scroll_script("measure")),
                timeout=self.collection_dom_operation_timeout_seconds,
            )
        except WorkerExecutionError as exc:
            if exc.code != "browser_window_surface_unstable":
                raise
            return False
        except asyncio.TimeoutError:
            return False
        except Exception as exc:
            self._raise_page_failure(exc, operation="recheck Instagram relationship list end")
            return False
        if (
            not isinstance(current, dict)
            or current.get("valid") is False
            or current.get("bottom") is not True
            or any(current.get(key) != expected.get(key) for key in (
                "height", "client", "top", "visible_height", "viewport_top", "viewport_bottom",
            ))
        ):
            return False
        if await self._has_visible_relation_loading_indicator(dialog):
            return False
        return await self._relation_surface_failure(dialog) is None

    async def _confirm_relation_list_end(self, dialog: Any) -> bool:
        """Confirm a stable physical list bottom without trusting the header count.

        Instagram's displayed relationship count can be a little higher than the
        rows it actually exposes. Conversely a transient network/render pause must
        never be mistaken for the end. Require two bottom measurements, no spinner,
        and a healthy page surface before accepting a short final total.
        """

        self._relation_end_measurement = None
        script = relation_scroll_script("measure")
        measurements: list[dict[str, Any]] = []
        for _ in range(2):
            try:
                value = await self._await_page_stage(
                    dialog.evaluate(script),
                    timeout=self.collection_dom_operation_timeout_seconds,
                )
            except WorkerExecutionError as exc:
                if exc.code != "browser_window_surface_unstable":
                    raise
                return False
            except asyncio.TimeoutError:
                return False
            except Exception as exc:
                self._raise_page_failure(exc, operation="confirm Instagram relationship list end")
                return False
            if (not isinstance(value, dict) or value.get("valid") is False
                    or value.get("bottom") is not True):
                return False
            measurements.append(value)
            if await self._has_visible_relation_loading_indicator(dialog):
                return False
            surface_reason = await self._relation_surface_failure(dialog)
            if surface_reason:
                return False
            await asyncio.sleep(max(0.2, self.collection_poll_interval_seconds))
            await self._guard()
        stable = all(measurements[0].get(key) == measurements[1].get(key) for key in (
            "height", "client", "top", "visible_height", "viewport_top", "viewport_bottom",
        ))
        if stable:
            self._relation_end_measurement = dict(measurements[-1])
        return stable

    async def _raise_incomplete_relation_list(
        self,
        surface_kind: Literal["followers", "following", "post_likers"],
        *,
        observed: int,
        expected_minimum: int,
        surface: Any | None = None,
    ) -> None:
        """Preserve rows when physical/semantic list-end evidence is missing."""

        surface_reason = (
            await self._relation_surface_failure(surface)
            if surface is not None and surface_kind in {"followers", "following"}
            else await self._page_surface_failure()
        )
        if surface_reason:
            self._raise_surface_reason(
                surface_reason,
                "Instagram relationship list stopped before its visible count was reached",
            )
        label = "粉丝" if surface_kind == "followers" else "关注"
        raise WorkerExecutionError(
            f"Instagram {label}列表已保存 {observed} 个，但尚未确认稳定末尾；"
            "已保留结果，将从已保存候选继续",
            reason=f"instagram_{surface_kind}_list_incomplete",
            pause_required=True,
            status_code=503,
        )

    async def _mark_post_likers_trigger_candidates(self) -> None:
        """Mark current Instagram click experiments using only the rendered post DOM."""
        try:
            await self.page.locator("body").evaluate(
                r"""body => {
                  body.querySelectorAll('[data-juxin-likers-trigger]').forEach(node =>
                    node.removeAttribute('data-juxin-likers-trigger'));
                  const visible = node => {
                    const style = getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' &&
                      rect.width > 0 && rect.height > 0;
                  };
                  const roots = [...body.querySelectorAll('article')].filter(visible);
                  if (!roots.length) {
                    const main = body.querySelector('main');
                    if (main && visible(main)) roots.push(main);
                  }
                  const actionOnly = /^(like|unlike|赞|讚|点赞|點讚|取消赞|取消讚|取消点赞|取消點讚)$/i;
                  const listText = /(liked\s+by|and\s+(?:[\d,.]+\s+)?others?|[\d,.]+\s+others?|[\d,.]+\s+(?:people|users?)\s+(?:like|liked)\s+this|(?:people|users?)\s+(?:who\s+)?liked\s+this|view\s+(?:all\s+)?[\d,.]+\s+likes?|[\d,.]+\s+likes?|[\d,.]+\s*(?:个|個|次)?(?:点赞|點讚|赞|讚)|(?:查看|檢視)(?:全部|所有).{0,20}(?:点赞|點讚|赞|讚)|(?:另有|其他|其余|其餘)\s*[\d,.]*\s*(?:人|位|个|個|用户|用戶)?)/i;
                  let serial = 0;
                  for (const root of roots) {
                    const nodes = root.querySelectorAll('a,button,[role="button"],span,div');
                    for (const node of nodes) {
                      if (!visible(node)) continue;
                      const href = node.getAttribute('href') || '';
                      const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
                      const aria = node.getAttribute('aria-label') || '';
                      const title = node.getAttribute('title') || '';
                      const joined = `${text} ${aria} ${title}`.replace(/\s+/g, ' ').trim();
                      if (!href.includes('/liked_by') && (!joined || joined.length > 240 || actionOnly.test(joined) || !listText.test(joined))) continue;
                      let clickable = node;
                      let cursor = node;
                      for (let depth = 0; depth < 5 && cursor && cursor !== root; depth += 1) {
                        const role = cursor.getAttribute && cursor.getAttribute('role');
                        const tag = cursor.tagName;
                        if (tag === 'A' || tag === 'BUTTON' || role === 'button' ||
                            cursor.hasAttribute('tabindex') || getComputedStyle(cursor).cursor === 'pointer') {
                          clickable = cursor;
                          break;
                        }
                        cursor = cursor.parentElement;
                      }
                      if (!clickable.hasAttribute('data-juxin-likers-trigger')) {
                        clickable.setAttribute('data-juxin-likers-trigger', String(++serial));
                      }
                    }
                  }
                }"""
            )
        except Exception as exc:
            self._raise_page_failure(
                exc, operation="inspect Instagram post like controls"
            )
            pass

    async def _find_post_likers_trigger(self) -> Any | None:
        """Find a likes-count control without ever selecting the Like action itself."""
        await self._mark_post_likers_trigger_candidates()
        locator_groups = (
            (self.page.locator('a[href*="/liked_by"]'), 80),
            (self.page.locator('[data-juxin-likers-trigger]'), 120),
            (self.page.locator(self.selectors.likes_trigger), 120),
            (
                self.page.locator(
                    'article a, article button, article [role="button"], '
                    'main a, main button, main [role="button"]'
                ),
                260,
            ),
            # Current Instagram experiments sometimes put the readable “and others”
            # label in a nested span while the parent owns the click handler.
            (self.page.locator("article span, main span"), 420),
        )
        visited: set[str] = set()
        for candidates, maximum in locator_groups:
            try:
                count = min(await candidates.count(), maximum)
            except Exception as exc:
                self._raise_page_failure(
                    exc, operation="inspect Instagram post like controls"
                )
                continue
            for index in range(count):
                candidate = candidates.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                    href = (await candidate.get_attribute("href")) or ""
                    text = await candidate.inner_text(timeout=2_000)
                    aria = (await candidate.get_attribute("aria-label")) or ""
                    title = (await candidate.get_attribute("title")) or ""
                except Exception as exc:
                    self._raise_page_failure(exc, operation="read Instagram post like control")
                    continue
                signature = "\u001f".join((href, text, aria, title)).casefold()
                if signature in visited:
                    continue
                visited.add(signature)
                if is_post_likers_trigger_candidate(
                    text=text,
                    aria_label=aria,
                    title=title,
                    href=href,
                ):
                    return candidate
        return None

    async def _click_post_likers_trigger(self, trigger: Any) -> bool:
        """Click the verified count control, including nested-text experiments."""
        try:
            await trigger.scroll_into_view_if_needed(timeout=5_000)
        except Exception as exc:
            self._raise_page_failure(exc, operation="scroll Instagram post like control")
            pass
        try:
            await trigger.click(timeout=10_000)
            return True
        except Exception as exc:
            self._raise_page_failure(exc, operation="click Instagram post like control")
            try:
                await trigger.evaluate(
                    """node => {
                      const clickable = node.closest('a,button,[role="button"],[tabindex]') || node;
                      clickable.scrollIntoView({block: 'center', inline: 'center'});
                      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup']) {
                        clickable.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                      }
                      clickable.click();
                    }"""
                )
                return True
            except Exception as fallback_exc:
                self._raise_page_failure(
                    fallback_exc, operation="click Instagram post like control"
                )
                return False

    async def _visible_profile_link_count(self, surface: Any) -> int:
        usernames: set[str] = set()
        try:
            links = surface.locator(self.selectors.account_links)
            count = min(await links.count(), 500)
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect Instagram liker rows")
            return 0
        for index in range(count):
            link = links.nth(index)
            try:
                if not await link.is_visible():
                    continue
                href = await link.get_attribute("href")
            except Exception as exc:
                self._raise_page_failure(exc, operation="read Instagram liker row")
                continue
            if href:
                username = extract_instagram_profile_username(href)
                if username:
                    usernames.add(username)
        return len(usernames)

    async def _mark_post_likers_surfaces(self) -> None:
        try:
            await self.page.locator("body").evaluate(
                r"""body => {
                  body.querySelectorAll('[data-juxin-likers-surface]').forEach(node =>
                    node.removeAttribute('data-juxin-likers-surface'));
                  const visible = node => {
                    const style = getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' &&
                      rect.width > 0 && rect.height > 0;
                  };
                  const heading = /^(likes?|liked by|点赞|點讚|赞|讚|讚好|喜欢|喜歡)$/i;
                  let serial = 0;
                  for (const node of body.querySelectorAll('h1,h2,h3,span,div')) {
                    if (!visible(node)) continue;
                    const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
                    if (!heading.test(text)) continue;
                    let candidate = node;
                    for (let depth = 0; depth < 9 && candidate && candidate !== body; depth += 1) {
                      const links = candidate.querySelectorAll('a[href^="/"],a[href^="https://www.instagram.com/"]');
                      const rect = candidate.getBoundingClientRect();
                      if (links.length && rect.width > 120 && rect.height > 80 &&
                          rect.width < window.innerWidth * 0.98 && rect.height < window.innerHeight * 0.98) {
                        candidate.setAttribute('data-juxin-likers-surface', String(++serial));
                        break;
                      }
                      candidate = candidate.parentElement;
                    }
                  }
                }"""
            )
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect Instagram liker surface")
            pass

    async def _current_post_likers_surface(self) -> tuple[Any | None, bool]:
        """Return only a verified liker surface, never an unrelated dialog/main."""
        dialogs = self.page.locator(self.selectors.dialog)
        try:
            dialog_count = min(await dialogs.count(), 20)
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect Instagram liker dialog")
            dialog_count = 0
        for index in range(dialog_count - 1, -1, -1):
            dialog = dialogs.nth(index)
            try:
                if not await dialog.is_visible():
                    continue
                text = await dialog.inner_text(timeout=3_000)
            except Exception as exc:
                self._raise_page_failure(exc, operation="read Instagram liker dialog")
                continue
            if await self._visible_profile_link_count(dialog):
                return dialog, False
            if has_visible_zero_likes(text):
                return None, True

        await self._mark_post_likers_surfaces()
        marked_surfaces = self.page.locator('[data-juxin-likers-surface]')
        try:
            marked_count = min(await marked_surfaces.count(), 20)
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect Instagram liker surface")
            marked_count = 0
        for index in range(marked_count - 1, -1, -1):
            surface = marked_surfaces.nth(index)
            try:
                if await surface.is_visible() and await self._visible_profile_link_count(surface):
                    return surface, False
            except Exception as exc:
                self._raise_page_failure(exc, operation="read Instagram liker surface")
                continue

        if "/liked_by" in str(self.page.url).casefold():
            main = self.page.locator("main").first
            try:
                if await main.count() and await main.is_visible():
                    text = await main.inner_text(timeout=3_000)
                    if await self._visible_profile_link_count(main):
                        return main, False
                    if has_visible_zero_likes(text):
                        return None, True
            except Exception as exc:
                self._raise_page_failure(exc, operation="read Instagram liker route")
                pass
        return None, False

    async def _wait_for_post_likers_surface(
        self,
        *,
        attempts: int = 24,
        interval: float = 0.5,
    ) -> tuple[Any | None, bool]:
        for _ in range(max(1, attempts)):
            surface, known_empty = await self._current_post_likers_surface()
            if surface is not None or known_empty:
                return surface, known_empty
            await asyncio.sleep(max(0, interval))
        return None, False

    async def _open_post_likers_surface(self, post_url: str) -> tuple[Any | None, bool]:
        """Open and verify the visible liker list; return ``(surface, known_empty)``."""
        try:
            article = self.page.locator("article").first
            if await article.count() and await article.is_visible():
                post_text = (await article.inner_text(timeout=5_000))[:100_000]
            else:
                post_text = (await self.page.locator("main").inner_text(timeout=5_000))[:100_000]
        except Exception as exc:
            self._raise_page_failure(exc, operation="read Instagram post")
            post_text = ""
        if has_visible_zero_likes(post_text):
            return None, True

        # Prefer the visible count control. This supports both dialog and client-side
        # route experiments without assuming that /liked_by/ remains navigable.
        for _ in range(2):
            trigger = await self._find_post_likers_trigger()
            if trigger is None:
                break
            if await self._click_post_likers_trigger(trigger):
                surface, known_empty = await self._wait_for_post_likers_surface()
                if surface is not None or known_empty:
                    return surface, known_empty
            await asyncio.sleep(0.75)

        liked_by_url = build_instagram_post_likers_url(post_url)
        if liked_by_url is None:
            raise WorkerExecutionError(
                "帖子已打开，但无法确认点赞列表地址；任务已保留，可刷新后继续",
                reason="instagram_post_likers_list_not_rendered",
                pause_required=False,
            )
        try:
            await self.page.goto(
                liked_by_url,
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            await self._wait_for_profile_surface()
            await self._guard()
            surface, known_empty = await self._wait_for_post_likers_surface(attempts=16)
            if surface is not None or known_empty:
                return surface, known_empty

            # Some builds rewrite /liked_by/ back to the post. Reacquire and click
            # the freshly rendered count control instead of treating navigation as a
            # successful collection surface.
            trigger = await self._find_post_likers_trigger()
            if trigger is not None and await self._click_post_likers_trigger(trigger):
                surface, known_empty = await self._wait_for_post_likers_surface()
                if surface is not None or known_empty:
                    return surface, known_empty
        except WorkerExecutionError:
            raise
        except Exception as exc:
            self._raise_page_failure(
                exc,
                operation="open Instagram liker route",
                navigation_timeout=True,
            )
            surface_reason = await self._page_surface_failure()
            if surface_reason:
                self._raise_surface_reason(
                    surface_reason,
                    "Instagram liker route is incomplete or unavailable",
                )
        raise WorkerExecutionError(
            "帖子已打开，但点赞用户列表没有显示；任务已保留，可刷新后继续",
            reason="instagram_post_likers_list_not_rendered",
            pause_required=False,
        )

    async def collect_post_likers(
        self, target: str, *, max_posts: int, per_post_limit: int,
        candidate_sink: CandidateBatchSink | None = None,
        initial_candidate_count: int = 0,
        progress_sink: CollectionProgressSink | None = None,
    ) -> CollectionOutcome:
        count = max(0, int(initial_candidate_count))
        replacing = False
        made_progress = False

        async def tracked_progress(payload: dict[str, Any]) -> None:
            nonlocal count, made_progress
            observed = payload.get("discovered_count")
            advanced = isinstance(observed, int) and not isinstance(observed, bool) and observed > count
            if progress_sink is not None:
                await progress_sink(payload)
            if isinstance(observed, int) and not isinstance(observed, bool):
                count = max(count, observed)
            if replacing and advanced:
                made_progress = True
                await self._finish_page_recovery(progressed=True)

        async def attempt() -> CollectionOutcome:
            return await self._collect_post_likers_once(
                target, max_posts=max_posts, per_post_limit=per_post_limit,
                candidate_sink=candidate_sink, initial_candidate_count=count,
                progress_sink=tracked_progress,
                capture_source_profile=progress_sink is not None,
            )

        username_norm, _ = normalize_instagram_username(target)
        fresh_reason = self._fresh_page_retry_targets.pop(username_norm, None)
        recoverable = _PROFILE_TAB_RECOVERY_REASONS | {
            "instagram_post_grid_not_rendered", "instagram_post_likers_list_not_rendered",
        }
        if fresh_reason:
            failure = WorkerExecutionError("尝试新的同账号采集页", reason=fresh_reason)
        else:
            try:
                return await attempt()
            except WorkerExecutionError as exc:
                if exc.code not in recoverable:
                    raise
                failure = exc
        await self._replace_stuck_page_once(username_norm, failure)
        self._forget_profile_attempt(username_norm)
        replacing = True
        try:
            result = await attempt()
            made_progress = True
            return result
        except WorkerExecutionError as exc:
            if exc.code in _PROFILE_RECOVERY_AUTHORITATIVE_REASONS:
                raise
            raise self._page_recovery_exhausted(exc, username_norm) from exc
        finally:
            await self._finish_page_recovery(progressed=made_progress)

    async def _collect_post_likers_once(
        self,
        target: str,
        *,
        max_posts: int,
        per_post_limit: int,
        candidate_sink: CandidateBatchSink | None = None,
        initial_candidate_count: int = 0,
        progress_sink: CollectionProgressSink | None = None,
        capture_source_profile: bool = True,
    ) -> CollectionOutcome:
        if not 1 <= max_posts <= 20 or per_post_limit < 1:
            raise ValidationError("max_posts must be 1-20 and per_post_limit must be positive")
        initial_candidate_count = max(0, int(initial_candidate_count))
        candidate_total_limit = max_posts * per_post_limit
        if progress_sink is not None:
            await progress_sink(
                {
                    "mode": "post_likers",
                    "source_total": None,
                    "discovered_count": initial_candidate_count,
                }
            )
        if candidate_sink is not None and initial_candidate_count >= candidate_total_limit:
            return CollectionOutcome(
                mode="post_likers",
                usernames=[],
                candidate_count=initial_candidate_count,
                source_total=None,
            )
        observed_total = initial_candidate_count

        async def tracking_sink(batch: list[str]) -> int | dict[str, Any]:
            nonlocal observed_total
            assert candidate_sink is not None
            acknowledgement = await candidate_sink(batch)
            candidate_total = (
                acknowledgement.get("total")
                if isinstance(acknowledgement, dict)
                else acknowledgement
            )
            if isinstance(candidate_total, bool) or not isinstance(candidate_total, int):
                raise RuntimeError("Candidate batch sink did not return an integer total")
            observed_total = max(observed_total, candidate_total)
            if progress_sink is not None:
                await progress_sink(
                    {
                        "mode": "post_likers",
                        "source_total": None,
                        "discovered_count": observed_total,
                    }
                )
            return acknowledgement

        def empty_profile_outcome() -> CollectionOutcome:
            return CollectionOutcome(
                mode="post_likers",
                usernames=[],
                candidate_count=(
                    observed_total if candidate_sink is not None else None
                ),
                source_total=None,
            )

        navigated_username = await self._navigate_profile(target)
        username_norm, _ = normalize_instagram_username(navigated_username)
        if progress_sink is not None and capture_source_profile:
            source_profile = await self._source_profile_metrics(username_norm)
            if source_profile:
                await progress_sink({
                    "mode": "post_likers", "source_total": None,
                    "discovered_count": observed_total,
                    "source_profile": source_profile,
                })
        # A confirmed public zero-post grid is already a complete result. Inspect
        # the rendered body before the normal 8-second post-grid wait so these
        # targets finish immediately instead of appearing to hang on their profile.
        try:
            initial_body_text = (
                await self.page.locator("body").inner_text(timeout=5_000)
            )[:100_000]
        except Exception as exc:
            self._raise_page_failure(exc, operation="read Instagram profile posts")
            initial_body_text = ""
        if (
            self._is_current_profile(username_norm)
            and extract_public_empty_profile_metrics(initial_body_text) is not None
        ):
            return empty_profile_outcome()
        try:
            await self.page.locator(self.selectors.post_links).first.wait_for(state="visible", timeout=8_000)
        except Exception as exc:
            self._raise_page_failure(exc, operation="wait for visible Instagram posts")
            pass
        urls = await self._visible_post_urls(maximum=max_posts)
        if not urls:
            try:
                body_text = (await self.page.locator("body").inner_text(timeout=5_000))[:100_000]
            except Exception as exc:
                self._raise_page_failure(exc, operation="read Instagram profile posts")
                body_text = ""
            if (
                self._is_current_profile(username_norm)
                and extract_public_empty_profile_metrics(body_text) is not None
            ):
                return empty_profile_outcome()
            surface_reason = await self._page_surface_failure(body_text=body_text)
            if surface_reason:
                self._raise_surface_reason(
                    surface_reason,
                    "Instagram post grid is incomplete or unavailable",
                )
            raise WorkerExecutionError(
                "Instagram 已打开目标主页，但没有识别到可见帖子列表；任务已保留，等待新页恢复",
                reason="instagram_post_grid_not_rendered",
                pause_required=False,
            )
        collected: dict[str, None] = {}
        skipped: list[str] = []
        verified_surfaces = 0
        last_unrecognized: WorkerExecutionError | None = None
        for post_url in urls[:max_posts]:
            try:
                await self.page.goto(post_url, wait_until="domcontentloaded", timeout=45_000)
            except Exception as exc:
                try:
                    await self._guard()
                except WorkerExecutionError:
                    raise
                self._raise_page_failure(
                    exc,
                    operation="open Instagram post",
                    navigation_timeout=True,
                )
                raise WorkerExecutionError(
                    "Instagram post navigation completed with an unrecognized page state",
                    reason="instagram_profile_dom_unrecognized",
                    pause_required=True,
                ) from exc
            try:
                await self.page.locator("article:visible, main:visible").first.wait_for(
                    state="visible", timeout=15_000
                )
            except Exception as exc:
                self._raise_page_failure(exc, operation="wait for Instagram post")
                pass
            await asyncio.sleep(0.75)
            await self._guard()
            try:
                surface, known_empty = await self._open_post_likers_surface(post_url)
                if known_empty:
                    verified_surfaces += 1
                    continue
                if surface is None:
                    skipped.append(post_url)
                    continue
                usernames = await self._read_visible_account_dialog(
                    surface,
                    per_post_limit,
                    surface_kind="post_likers",
                    candidate_sink=(
                        tracking_sink if candidate_sink is not None else None
                    ),
                    initial_candidate_count=observed_total,
                    candidate_total_limit=candidate_total_limit,
                    surface_unique_limit=per_post_limit,
                )
                verified_surfaces += 1
                for username in usernames:
                    collected.setdefault(username, None)
            except WorkerExecutionError as exc:
                if exc.details.get("pause_required", True):
                    raise
                last_unrecognized = exc
                skipped.append(post_url)
            except Exception as exc:
                self._raise_page_failure(exc, operation="collect Instagram post likers")
                skipped.append(post_url)
            finally:
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
        if collected or verified_surfaces:
            if candidate_sink is None and progress_sink is not None:
                await progress_sink(
                    {
                        "mode": "post_likers",
                        "source_total": None,
                        "discovered_count": len(collected),
                    }
                )
            return CollectionOutcome(
                mode="post_likers",
                usernames=list(collected),
                skipped_posts=skipped,
                candidate_count=(
                    observed_total if candidate_sink is not None else None
                ),
                source_total=None,
            )
        if last_unrecognized is not None:
            raise last_unrecognized
        raise WorkerExecutionError(
            "帖子已打开，但所有点赞用户入口均未识别；任务已保留，等待新页恢复",
            reason="instagram_post_likers_list_not_rendered",
            pause_required=False,
        )

    async def execute_action(
        self,
        operation: Literal["follow", "greet"],
        target: str,
        *,
        message: str | None = None,
    ) -> ActionOutcome:
        if operation == "follow":
            return await self.follow(target)
        if operation == "greet":
            if not message or not message.strip():
                raise ValidationError("A greeting message is required")
            return await self.greet(target, message.strip())
        raise ValidationError("Unsupported action")

    async def follow(self, target: str) -> ActionOutcome:
        username = await self._navigate_profile(target)
        # Restrict controls to the target profile header. Scanning every button can
        # otherwise click Follow on a suggested account shown lower on the same page.
        buttons = self.page.locator(
            'main header button, main header [role="button"], header button, header [role="button"]'
        )
        follow_button = None
        try:
            button_count = min(await buttons.count(), 60)
        except Exception:
            button_count = 0
        for index in range(button_count):
            button = buttons.nth(index)
            try:
                text = " ".join((await button.inner_text(timeout=2_000)).casefold().split())
            except Exception:
                continue
            if text in _FOLLOW_CONFIRMED_MARKERS:
                return ActionOutcome("follow", username, "already_done", text)
            if text in _PROFILE_FOLLOW_CONTROL_MARKERS:
                follow_button = button
                break
        if follow_button is None:
            raise WorkerExecutionError(
                "目标账号页没有可确认的关注按钮；未执行任何关注操作",
                reason="instagram_content_not_visible",
                pause_required=False,
            )
        async with self._destructive_action_lease():
            return await self._click_and_confirm_follow(
                username, follow_button
            )

    async def _click_and_confirm_follow(
        self, username: str, follow_button: Any
    ) -> ActionOutcome:
        try:
            await follow_button.click()
            # Private accounts often change to “已发送” rather than “已请求”. Poll
            # the target header for a bounded period and record success only after
            # that state is visible. This also covers slower render cycles.
            for _ in range(16):
                await asyncio.sleep(0.5)
                await self._guard()
                refreshed = self.page.locator(
                    'main header button, main header [role="button"], header button, header [role="button"]'
                )
                try:
                    refreshed_count = min(await refreshed.count(), 60)
                except Exception:
                    refreshed_count = 0
                for index in range(refreshed_count):
                    try:
                        text = " ".join(
                            (await refreshed.nth(index).inner_text(timeout=2_000)).casefold().split()
                        )
                    except Exception:
                        continue
                    if text in _FOLLOW_CONFIRMED_MARKERS:
                        return ActionOutcome("follow", username, "confirmed", text)
            raise WorkerExecutionError(
                "已点击目标账号的关注按钮，但页面未出现“已关注/已发送”；结果待人工确认，不会自动重试",
                reason="instagram_action_outcome_unknown",
                pause_required=True,
            )
        except asyncio.CancelledError as exc:
            # The click call is the first operation inside this try block, so a
            # cancellation here cannot prove Instagram did not receive it.
            setattr(exc, "instagram_action_outcome_unknown", True)
            setattr(
                exc,
                "action_outcome_message",
                "关注动作被中断，可能已经执行；结果待人工确认且不会自动重试",
            )
            raise
        except WorkerExecutionError as exc:
            if exc.code == "instagram_action_outcome_unknown":
                raise
            # Once the click call begins, a later network/challenge/read error can
            # no longer prove that Instagram did not receive it.  Preserve the
            # no-automatic-retry safety boundary instead of treating it like a
            # pre-navigation failure.
            raise WorkerExecutionError(
                "关注按钮可能已经点击，但确认页面发生异常；结果待人工确认，不会自动重试",
                reason="instagram_action_outcome_unknown",
                pause_required=True,
            ) from exc
        except Exception as exc:
            raise WorkerExecutionError(
                "关注点击发生异常，结果待人工确认；不会自动跳到下一个账号",
                reason="instagram_action_outcome_unknown",
                pause_required=True,
            ) from exc

    async def greet(self, target: str, message: str) -> ActionOutcome:
        if not message or not message.strip():
            raise ValidationError("A greeting message is required")
        if len(message) > 200:
            raise ValidationError("Greeting message may not exceed 200 characters")
        username_norm, username = normalize_instagram_username(target)
        await self._ensure_window_surface_stable()
        try:
            await self.page.goto(
                "https://www.instagram.com/direct/inbox/",
                wait_until="domcontentloaded",
                timeout=45_000,
            )
        except Exception as exc:
            try:
                await self._guard()
            except WorkerExecutionError:
                raise
            self._raise_page_failure(
                exc,
                operation="open Instagram message inbox",
                navigation_timeout=True,
            )
            raise WorkerExecutionError(
                "Instagram 消息页未能稳定打开；未发送任何消息",
                reason="instagram_direct_inbox_not_rendered",
                pause_required=False,
            ) from exc
        await self._wait_for_profile_surface()
        await self._guard()
        composer = await self._open_direct_thread_from_inbox_search(
            username_norm,
            username,
        )
        confirmation = await self._send_and_confirm_greeting(
            composer,
            message,
            username_norm=username_norm,
        )
        return ActionOutcome("greet", username, "confirmed", confirmation)

    async def _visible_outside_dialog(self, locator: Any, *, maximum: int) -> Any | None:
        """Return a visible locator that belongs to the inbox, never a modal."""

        try:
            count = min(await locator.count(), maximum)
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect Instagram message inbox")
            return None
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                inside_dialog = await candidate.evaluate(
                    "element => Boolean(element.closest('[role=dialog]'))"
                )
            except Exception as exc:
                self._raise_page_failure(exc, operation="inspect Instagram inbox control")
                continue
            if not inside_dialog:
                return candidate
        return None

    def _direct_inbox_search_inputs(self) -> Any:
        return self.page.locator(
            ", ".join(
                (
                    'main input[placeholder*="search" i]',
                    'main input[aria-label*="search" i]',
                    'main input[placeholder*="搜索"]',
                    'main input[aria-label*="搜索"]',
                    'main input[placeholder*="搜尋"]',
                    'main input[aria-label*="搜尋"]',
                    'main input[placeholder*="buscar" i]',
                )
            )
        )

    def _direct_inbox_result_candidates(self) -> Any:
        """Return clickable inbox controls in DOM order.

        Geometry and account-row structure are applied separately so this also
        works with Instagram variants that expose only ``tabindex`` rather than
        an ARIA option/listitem role.
        """

        return self.page.locator(
            'main a, main button, main [role="button"], '
            'main [role="option"], main [role="listitem"], main [tabindex="0"]'
        )

    async def _direct_inbox_row_profile_hrefs(
        self,
        row: Any,
        *,
        maximum: int = 20,
    ) -> tuple[str, ...]:
        """Read profile evidence from a result row without clicking its links."""

        hrefs: list[str] = []
        try:
            row_href = await row.get_attribute("href")
        except Exception:
            row_href = None
        if row_href:
            hrefs.append(str(row_href))
        try:
            links = row.locator("a[href]")
            link_count = min(await links.count(), maximum)
        except Exception:
            link_count = 0
        for index in range(link_count):
            try:
                href = await links.nth(index).get_attribute("href")
            except Exception:
                continue
            if href:
                hrefs.append(str(href))
        return tuple(dict.fromkeys(hrefs))

    async def _first_direct_inbox_recipient_row(
        self,
        search_input: Any,
        *,
        maximum: int = 240,
    ) -> Any | None:
        """Return only the first visible account row below the inbox search.

        Instagram nests profile anchors inside the clickable result container.
        Candidates at the same vertical position are collapsed to the largest
        outer control, preventing a profile link from becoming the click target.
        Missing layout evidence fails closed instead of scanning unrelated main
        controls by DOM order.
        """

        try:
            search_box = await search_input.bounding_box()
        except Exception:
            search_box = None
        if not search_box:
            return None
        candidates = self._direct_inbox_result_candidates()
        try:
            candidate_count = min(await candidates.count(), maximum)
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect Instagram inbox search results")
            return None

        rows: list[tuple[int, Any, dict[str, float] | None, float]] = []
        for index in range(candidate_count):
            candidate = candidates.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                if await candidate.evaluate(
                    "element => Boolean(element.closest('[role=dialog]'))"
                ):
                    continue
                row_text = await candidate.inner_text(timeout=2_000)
                row_href = await candidate.get_attribute("href")
                try:
                    box = await candidate.bounding_box()
                except Exception:
                    box = None
            except Exception as exc:
                self._raise_page_failure(exc, operation="inspect Instagram inbox recipient")
                continue

            lines = [line.strip() for line in row_text.splitlines() if line.strip()]
            if len(lines) < 2 and extract_instagram_profile_username(row_href or "") is None:
                continue
            if not box:
                continue
            search_left = float(search_box["x"])
            search_right = search_left + float(search_box["width"])
            search_bottom = float(search_box["y"]) + float(search_box["height"])
            row_left = float(box["x"])
            row_right = row_left + float(box["width"])
            horizontal_overlap = min(search_right, row_right) - max(
                search_left,
                row_left,
            )
            if float(box["y"]) + 2 < search_bottom or horizontal_overlap <= 0:
                continue
            area = float(box["width"]) * float(box["height"])
            rows.append((index, candidate, box, area))

        if not rows:
            return None
        first_top = min(float(record[2]["y"]) for record in rows if record[2])
        first_band = [
            record
            for record in rows
            if record[2] and float(record[2]["y"]) <= first_top + 12
        ]
        # Prefer the largest outer row when its nested profile link shares the
        # same top edge. DOM order resolves equal-sized candidates.
        return max(first_band, key=lambda record: (record[3], -record[0]))[1]

    async def _direct_inbox_result_click_target(self, row: Any) -> Any | None:
        """Return a conversation-opening row, never an inner profile anchor."""

        try:
            href = await row.get_attribute("href")
        except Exception:
            href = None
        if extract_instagram_profile_username(href or "") is None:
            return row
        try:
            ancestors = row.locator(
                "xpath=ancestor::*[self::button or @role='button' or "
                "@role='option' or @role='listitem' or @tabindex='0'][1]"
            )
            if not await ancestors.count():
                return None
            ancestor = ancestors.first
            if not await ancestor.is_visible():
                return None
            if await ancestor.evaluate(
                "element => Boolean(element.closest('[role=dialog]'))"
            ):
                return None
            return ancestor
        except Exception:
            return None

    async def _visible_direct_composer(
        self,
        *,
        reference: Any | None = None,
        maximum: int = 80,
    ) -> Any | None:
        """Return the visible editable control in the right Direct column.

        Instagram can retain hidden editor clones while switching threads.  Taking
        ``locator.last`` can therefore select a stale node even though the current
        composer is already visible.  This resolver rejects disabled, read-only and
        dialog-owned nodes, excludes the left inbox search column when geometry is
        available, then prefers the candidate nearest an existing composer or the
        bottom-right editable surface.
        """

        reference_box: dict[str, float] | None = None
        if reference is not None:
            try:
                reference_box = await reference.bounding_box()
            except Exception:
                reference_box = None

        search_box: dict[str, float] | None = None
        try:
            search_input = await self._visible_outside_dialog(
                self._direct_inbox_search_inputs(),
                maximum=30,
            )
            if search_input is not None:
                search_box = await search_input.bounding_box()
        except Exception:
            search_box = None

        candidates = self.page.locator(self.selectors.message_composer)
        try:
            candidate_count = min(await candidates.count(), maximum)
        except Exception as exc:
            self._raise_page_failure(exc, operation="inspect Instagram direct composer")
            return None

        usable: list[tuple[tuple[float, ...], Any]] = []
        for index in range(candidate_count):
            candidate = candidates.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                evaluate = getattr(candidate, "evaluate", None)
                if callable(evaluate) and await evaluate(
                    "element => Boolean(element.closest('[role=dialog]'))"
                ):
                    continue
                get_attribute = getattr(candidate, "get_attribute", None)
                if callable(get_attribute):
                    disabled = await get_attribute("disabled")
                    aria_disabled = str(
                        await get_attribute("aria-disabled") or ""
                    ).casefold()
                    readonly = await get_attribute("readonly")
                    if (
                        disabled is not None
                        or aria_disabled == "true"
                        or readonly is not None
                    ):
                        continue
                is_editable = getattr(candidate, "is_editable", None)
                if callable(is_editable) and not await is_editable():
                    continue
                box = await candidate.bounding_box()
            except Exception as exc:
                self._raise_page_failure(exc, operation="inspect Instagram direct composer")
                continue
            if not box:
                continue
            width = float(box["width"])
            height = float(box["height"])
            if width < 40 or height < 10:
                continue
            center_x = float(box["x"]) + width / 2
            center_y = float(box["y"]) + height / 2
            if search_box:
                search_right = float(search_box["x"]) + float(search_box["width"])
                search_bottom = float(search_box["y"]) + float(search_box["height"])
                # The Direct search lives in the left pane.  A real conversation
                # composer must be to its right and below its top search row.
                if center_x <= search_right or center_y <= search_bottom:
                    continue
            if reference_box:
                reference_center_x = float(reference_box["x"]) + float(
                    reference_box["width"]
                ) / 2
                reference_center_y = float(reference_box["y"]) + float(
                    reference_box["height"]
                ) / 2
                distance = abs(center_x - reference_center_x) + abs(
                    center_y - reference_center_y
                )
                score = (-distance, center_y, center_x, width, -float(index))
            else:
                score = (center_y, center_x, width, height, -float(index))
            usable.append((score, candidate))

        if not usable:
            return None
        return max(usable, key=lambda item: item[0])[1]

    async def _open_direct_thread_from_inbox_search(
        self,
        username_norm: str,
        username_display: str,
        *,
        recipient_attempts: int = 24,
        thread_attempts: int = 30,
        poll_interval: float = 0.4,
    ) -> Any:
        """Use the inbox left search field and open the exact visible account.

        This intentionally does not click Instagram's New Message button and does
        not navigate to the modal recipient picker. The stable inbox search is the
        only supported entry point.
        """

        search_inputs = self._direct_inbox_search_inputs()
        try:
            await search_inputs.first.wait_for(state="visible", timeout=12_000)
        except Exception:
            pass
        search_ready = False
        for _ in range(3):
            search_input = await self._visible_outside_dialog(search_inputs, maximum=30)
            if search_input is None:
                break
            try:
                await search_input.click(timeout=5_000)
                await search_input.fill("")
                await search_input.fill(username_norm)
                await asyncio.sleep(0.6)
                current_search = " ".join(
                    (await search_input.input_value()).strip().lstrip("@").casefold().split()
                )
                if current_search == username_norm:
                    search_ready = True
                    break
            except Exception as exc:
                self._raise_page_failure(exc, operation="use Instagram inbox search")
                await asyncio.sleep(0.4)
        if not search_ready:
            raise WorkerExecutionError(
                "消息页已打开，但左侧会话搜索框未能稳定写入目标账号；未发送任何消息",
                reason="instagram_direct_inbox_search_not_ready",
                pause_required=False,
            )
        await self._guard()

        recipient_row = None
        first_result_seen = False
        for _ in range(recipient_attempts):
            await self._guard()
            row = await self._first_direct_inbox_recipient_row(search_input)
            if row is not None:
                first_result_seen = True
                try:
                    row_text = await row.inner_text(timeout=2_000)
                except Exception as exc:
                    self._raise_page_failure(exc, operation="inspect Instagram inbox recipient")
                    row_text = ""
                profile_hrefs = await self._direct_inbox_row_profile_hrefs(row)
                if direct_inbox_result_text_matches(
                    row_text,
                    username_norm,
                    profile_hrefs=profile_hrefs,
                ):
                    recipient_row = row
            if recipient_row is not None:
                break
            await asyncio.sleep(poll_interval)
        if recipient_row is None:
            detail = "第一条结果不是该精确账号" if first_result_seen else "没有出现账号结果"
            raise WorkerExecutionError(
                f"左侧消息搜索{detail} {username_display}；未点击任何账号",
                reason="instagram_direct_inbox_recipient_not_found",
                pause_required=False,
            )

        click_target = await self._direct_inbox_result_click_target(recipient_row)
        if click_target is None:
            raise WorkerExecutionError(
                f"已找到 {username_display}，但首条账号整行无法安全点击；未发送任何消息",
                reason="instagram_direct_chat_not_rendered",
                pause_required=False,
            )
        try:
            await click_target.click(timeout=10_000)
        except Exception as exc:
            self._raise_page_failure(exc, operation="open Instagram inbox conversation")
            raise WorkerExecutionError(
                f"已找到 {username_display}，但未能打开右侧会话；未发送任何消息",
                reason="instagram_direct_chat_not_rendered",
                pause_required=False,
            ) from exc

        conversation_rendered = False
        for _ in range(thread_attempts):
            await self._guard()
            composer = await self._visible_direct_composer()
            if composer is not None:
                conversation_rendered = True
                if await self._direct_thread_matches_recipient(
                    username_norm,
                    composer,
                ):
                    return composer
            await asyncio.sleep(poll_interval)
        if conversation_rendered:
            raise WorkerExecutionError(
                f"右侧会话未能再次确认精确账号 {username_display}；未发送任何消息",
                reason="instagram_direct_recipient_mismatch",
                pause_required=False,
            )
        raise WorkerExecutionError(
            f"已选择 {username_display}，但右侧会话输入框没有显示；未发送任何消息",
            reason="instagram_direct_chat_not_rendered",
            pause_required=False,
        )

    async def _direct_thread_matches_recipient(
        self,
        username_norm: str,
        composer: Any,
    ) -> bool:
        """Verify the exact recipient in the right conversation before sending.

        The left search result stays visible after a thread opens, so evidence is
        accepted only in the same horizontal conversation column as the composer.
        A profile link is preferred. A uniquely positioned exact bare/structured
        handle in the right top bar is the only class-only fallback. Missing or
        ambiguous layout evidence fails closed.
        """

        target = username_norm.strip().lstrip("@").casefold()
        if not target:
            return False
        try:
            composer_box = await composer.bounding_box()
        except Exception as exc:
            self._raise_page_failure(exc, operation="verify Instagram direct recipient")
            return False
        if not composer_box:
            return False

        composer_left = float(composer_box["x"])
        composer_right = composer_left + float(composer_box["width"])
        composer_top = float(composer_box["y"])

        def belongs_to_conversation_column(box: dict[str, float] | None) -> bool:
            if not box:
                return False
            candidate_center = float(box["x"]) + float(box["width"]) / 2
            return (
                composer_left <= candidate_center <= composer_right
                and float(box["y"]) < composer_top
            )

        # The left search row normally sits just below the right thread header.
        # Its top edge is a useful visible-DOM boundary which excludes transcript
        # profile shares without relying on Instagram class names.  If layout
        # evidence is unavailable, use a deliberately narrow fraction of the area
        # above the bottom composer.
        header_center_limit = min(160.0, max(88.0, composer_top * 0.16))
        try:
            search_input = await self._visible_outside_dialog(
                self._direct_inbox_search_inputs(),
                maximum=30,
            )
            search_box = (
                await search_input.bounding_box()
                if search_input is not None
                else None
            )
        except Exception:
            search_box = None
        if search_box:
            search_center_x = float(search_box["x"]) + float(
                search_box["width"]
            ) / 2
            if search_center_x < composer_left:
                header_center_limit = float(search_box["y"]) + max(
                    8.0,
                    float(search_box["height"]) * 0.35,
                )

        def belongs_to_header_band(box: dict[str, float] | None) -> bool:
            if not belongs_to_conversation_column(box) or not box:
                return False
            center_y = float(box["y"]) + float(box["height"]) / 2
            return center_y <= header_center_limit

        # Transcript profile shares are not identity evidence. Restrict profile links
        # to the conversation header/banner and exclude every dialog surface.
        links = self.page.locator(
            "main header a[href], main [role='banner'] a[href]"
        )
        try:
            link_count = min(await links.count(), 240)
        except Exception as exc:
            self._raise_page_failure(exc, operation="verify Instagram direct recipient")
            link_count = 0
        for index in range(link_count):
            link = links.nth(index)
            try:
                if not await link.is_visible():
                    continue
                if await link.evaluate(
                    "element => Boolean(element.closest('[role=dialog]'))"
                ):
                    continue
                href = await link.get_attribute("href")
                if extract_instagram_profile_username(href or "") != target:
                    continue
                if belongs_to_header_band(await link.bounding_box()):
                    return True
            except Exception as exc:
                self._raise_page_failure(exc, operation="verify Instagram direct recipient")

        # Current Direct builds often render the top bar as ordinary nested divs
        # rather than a semantic header/banner.  Accept an exact profile href only
        # inside the strict top band of the same horizontal column as the composer.
        # The band deliberately excludes profile cards shared in the transcript.
        all_links = self.page.locator(
            ", ".join(
                (
                    f'main a[href="/{target}/"]',
                    f'main a[href="/{target}"]',
                    f'main a[href="https://www.instagram.com/{target}/"]',
                    f'main a[href="https://www.instagram.com/{target}"]',
                )
            )
        )
        try:
            all_link_count = min(await all_links.count(), 320)
        except Exception as exc:
            self._raise_page_failure(exc, operation="verify Instagram direct recipient")
            all_link_count = 0
        for index in range(all_link_count):
            link = all_links.nth(index)
            try:
                if not await link.is_visible():
                    continue
                if await link.evaluate(
                    "element => Boolean(element.closest('[role=dialog]'))"
                ):
                    continue
                href = await link.get_attribute("href")
                if extract_instagram_profile_username(href or "") != target:
                    continue
                if belongs_to_header_band(await link.bounding_box()):
                    return True
            except Exception as exc:
                self._raise_page_failure(exc, operation="verify Instagram direct recipient")

        headers = self.page.locator("main header, main [role='banner']")
        try:
            header_count = min(await headers.count(), 40)
        except Exception as exc:
            self._raise_page_failure(exc, operation="verify Instagram direct recipient")
            header_count = 0
        for index in range(header_count):
            header = headers.nth(index)
            try:
                if not await header.is_visible():
                    continue
                if await header.evaluate(
                    "element => Boolean(element.closest('[role=dialog]'))"
                ):
                    continue
                if not belongs_to_header_band(await header.bounding_box()):
                    continue
                header_text = await header.inner_text(timeout=2_000)
                if direct_recipient_text_matches(header_text, target):
                    return True
            except Exception as exc:
                self._raise_page_failure(exc, operation="verify Instagram direct recipient")

        # Last, handle the class-only/ordinary-div header experiment.  Candidate
        # evidence is accepted only inside the strict top band and only when all
        # nested profile hrefs agree, an explicit @handle agrees, or the second
        # structured line is the exact handle. A single bare line is never enough:
        # it may be another account's display name. Multiple spatially distinct
        # matches are ambiguous and must fail closed.
        def header_evidence_matches(
            row_text: str,
            profile_hrefs: Iterable[str],
        ) -> bool:
            profile_usernames = {
                username
                for href in profile_hrefs
                if (username := extract_instagram_profile_username(href))
            }
            if profile_usernames and profile_usernames != {target}:
                return False
            lines = [
                " ".join(line.split())
                for line in row_text.splitlines()
                if line.strip()
            ]
            explicit_handles = {
                line.strip().lstrip("@").casefold()
                for line in lines
                if line.strip().startswith("@")
            }
            structured_second: str | None = None
            if len(lines) >= 2:
                try:
                    structured_second, _ = normalize_instagram_username(
                        lines[1].strip().lstrip("@")
                    )
                except ValidationError:
                    structured_second = None
            if structured_second is not None and structured_second != target:
                # A display name may equal the requested handle while the
                # structured account line belongs to a different user.
                return False
            return any(
                (
                    profile_usernames == {target},
                    explicit_handles == {target},
                    structured_second == target,
                )
            )

        matches: list[dict[str, float]] = []
        fast_scan_completed = False
        page_evaluate = getattr(self.page, "evaluate", None)
        if callable(page_evaluate):
            try:
                records = await page_evaluate(
                    """bounds => {
                        const visible = element => {
                            if (!(element instanceof HTMLElement)) return false;
                            const style = window.getComputedStyle(element);
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && element.getClientRects().length > 0;
                        };
                        const candidates = Array.from(document.querySelectorAll(
                            'main button, main [role="button"], main [tabindex="0"], '
                            + 'main div[aria-label], main div[title], main div'
                        ));
                        const output = [];
                        for (const element of candidates) {
                            if (!visible(element) || element.closest('[role=dialog]')) continue;
                            const rect = element.getBoundingClientRect();
                            const centerX = rect.left + rect.width / 2;
                            const centerY = rect.top + rect.height / 2;
                            if (centerX < bounds.left || centerX > bounds.right) continue;
                            if (centerY > bounds.headerBottom) continue;
                            if (rect.top >= bounds.composerTop) continue;
                            if (rect.height > 144 || rect.width > bounds.maxWidth) continue;
                            const text = element.innerText || '';
                            const hrefs = [];
                            if (element instanceof HTMLAnchorElement && element.href) {
                                hrefs.push(element.getAttribute('href') || element.href);
                            }
                            for (const link of Array.from(element.querySelectorAll('a[href]')).slice(0, 12)) {
                                hrefs.push(link.getAttribute('href') || link.href);
                            }
                            output.push({
                                text,
                                hrefs: Array.from(new Set(hrefs)),
                                box: {
                                    x: rect.left,
                                    y: rect.top,
                                    width: rect.width,
                                    height: rect.height
                                }
                            });
                            if (output.length >= 160) break;
                        }
                        return output;
                    }""",
                    {
                        "left": composer_left,
                        "right": composer_right,
                        "composerTop": composer_top,
                        "headerBottom": header_center_limit,
                        "maxWidth": float(composer_box["width"]) + 32,
                    },
                )
                if isinstance(records, list):
                    fast_scan_completed = True
                    for record in records:
                        if not isinstance(record, dict):
                            continue
                        box = record.get("box")
                        row_text = record.get("text")
                        profile_hrefs = record.get("hrefs")
                        if (
                            isinstance(box, dict)
                            and isinstance(row_text, str)
                            and isinstance(profile_hrefs, list)
                            and header_evidence_matches(row_text, profile_hrefs)
                        ):
                            matches.append(box)
            except Exception:
                # Unit/dummy pages and older CDP surfaces may not support the
                # batched DOM evaluation. The bounded locator fallback below keeps
                # the same fail-closed evidence rules.
                fast_scan_completed = False

        if not fast_scan_completed:
            header_nodes = self.page.locator(
                "main button, main [role='button'], main [tabindex='0'], "
                "main div[aria-label], main div[title], main div"
            )
            try:
                node_count = min(await header_nodes.count(), 400)
            except Exception as exc:
                self._raise_page_failure(exc, operation="verify Instagram direct recipient")
                node_count = 0

            for index in range(node_count):
                candidate = header_nodes.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                    if await candidate.evaluate(
                        "element => Boolean(element.closest('[role=dialog]'))"
                    ):
                        continue
                    box = await candidate.bounding_box()
                    if not belongs_to_header_band(box) or not box:
                        continue
                    if float(box["height"]) > 144 or float(box["width"]) > float(
                        composer_box["width"]
                    ) + 32:
                        continue
                    row_text = await candidate.inner_text(timeout=1_500)
                    profile_hrefs = await self._direct_inbox_row_profile_hrefs(
                        candidate,
                        maximum=12,
                    )
                    if not header_evidence_matches(row_text, profile_hrefs):
                        continue
                    matches.append(box)
                except Exception as exc:
                    self._raise_page_failure(
                        exc,
                        operation="verify Instagram direct recipient",
                    )

        if not matches:
            return False

        # Nested div/button wrappers from one header overlap heavily and constitute
        # one logical candidate.  Two separate positions mean that identity is
        # ambiguous (for example a header plus a transcript card) and are rejected.
        clusters: list[list[dict[str, float]]] = []
        for box in matches:
            center_x = float(box["x"]) + float(box["width"]) / 2
            center_y = float(box["y"]) + float(box["height"]) / 2
            for cluster in clusters:
                anchor = cluster[0]
                anchor_center_x = float(anchor["x"]) + float(anchor["width"]) / 2
                anchor_center_y = float(anchor["y"]) + float(anchor["height"]) / 2
                horizontal_overlap = min(
                    float(box["x"]) + float(box["width"]),
                    float(anchor["x"]) + float(anchor["width"]),
                ) - max(float(box["x"]), float(anchor["x"]))
                if (
                    abs(center_y - anchor_center_y) <= 18
                    and (
                        abs(center_x - anchor_center_x) <= 36
                        or horizontal_overlap > 0
                    )
                ):
                    cluster.append(box)
                    break
            else:
                clusters.append([box])
        return len(clusters) == 1

    async def _composer_text(self, composer: Any) -> str:
        value = await composer.evaluate(
            """element => {
                if (typeof element.value === 'string') return element.value;
                // Instagram's empty Direct editor is commonly rendered as
                // <p><br></p>, which reports one or more newlines through
                // innerText.  Some experiments also insert a real placeholder
                // descendant into the contenteditable tree.  Read a detached
                // clone with those decorations removed so neither shape is
                // mistaken for an operator's unsent draft.
                const clone = element.cloneNode(true);
                for (const placeholder of clone.querySelectorAll(
                    '[data-placeholder], [data-lexical-placeholder="true"]'
                )) {
                    placeholder.remove();
                }
                return clone.innerText || clone.textContent || '';
            }"""
        )
        normalized = normalize_direct_message_text(value)
        # A visually empty contenteditable may expose only line breaks, NBSPs,
        # zero-width spaces or a BOM.  Replacing those layout artifacts cannot
        # hide meaningful content from the subsequent replacement verification;
        # any visible/non-whitespace text is returned byte-for-byte.
        return "" if not normalized.strip() else normalized

    async def _current_direct_composer(self, reference: Any | None = None) -> Any | None:
        """Resolve the live composer, tolerating React replacing its DOM node."""

        # Small unit/dummy workers may provide only a composer object and no page.
        # A real browser must always pass the full current safety resolver. Falling
        # back to a merely visible old node would bypass its dialog, readonly,
        # disabled and conversation-column checks after a React/thread transition.
        if self.page is None:
            return reference
        return await self._visible_direct_composer(reference=reference)

    async def _stable_direct_composer_with_text(
        self,
        reference: Any,
        expected: str,
        *,
        attempts: int = 10,
        interval: float = 0.15,
    ) -> tuple[Any | None, bool]:
        """Return a live composer after two consecutive exact text reads.

        The boolean reports whether any non-empty, non-matching value appeared.
        Such text is never cleared automatically because it may be an operator's
        draft or a concurrent edit.
        """

        exact_streak = 0
        saw_nonmatching_text = False
        current_reference = reference
        for _ in range(max(2, attempts)):
            current = await self._current_direct_composer(current_reference)
            if current is None:
                exact_streak = 0
            else:
                current_reference = current
                try:
                    value = await self._composer_text(current)
                except Exception as exc:
                    # Unknown editor state is not an empty-editor signal. In
                    # particular, never fall through to keyboard insertion after a
                    # failed read because that could append to an unseen draft.
                    raise WorkerExecutionError(
                        "无法确认招呼输入框中的实际内容；已保留现场且未执行发送",
                        reason="instagram_direct_composer_not_ready",
                        pause_required=True,
                    ) from exc
                if value == expected:
                    exact_streak += 1
                    if exact_streak >= 2:
                        return current, saw_nonmatching_text
                else:
                    exact_streak = 0
                    if value:
                        saw_nonmatching_text = True
            if interval:
                await asyncio.sleep(interval)
        return None, saw_nonmatching_text

    async def _write_greeting_to_composer(
        self,
        composer: Any,
        message: str,
    ) -> Any:
        """Replace the current Direct composer with one verified greeting.

        The operator explicitly allows a pre-existing draft to be overwritten.
        Playwright ``fill`` is preferred because it replaces the editor through a
        single input event without clipboard access or destructive keyboard chords.
        A focused ``keyboard.insert_text`` fallback is used only when fill leaves the
        verified-empty editor completely empty.  Partial or foreign text is retained
        and causes a fail-closed error rather than appending or sending mixed text.
        """

        expected = normalize_direct_message_text(message)
        active = await self._current_direct_composer(composer)
        if active is None:
            raise WorkerExecutionError(
                "右侧会话输入框已消失；未写入或发送任何话术",
                reason="instagram_direct_composer_not_ready",
                pause_required=False,
            )
        fill_error: Exception | None = None
        try:
            click = getattr(active, "click", None)
            if callable(click):
                await click(timeout=5_000)
            await active.fill(message)
        except Exception as exc:
            fill_error = exc

        written, saw_nonmatching_text = await self._stable_direct_composer_with_text(
            active,
            expected,
        )
        if written is not None:
            return written
        if saw_nonmatching_text:
            raise WorkerExecutionError(
                "招呼输入框出现了非目标话术或只写入部分内容；已保留现场且未发送",
                reason="instagram_direct_composer_not_ready",
                pause_required=True,
            ) from fill_error

        # React/Lexical occasionally ignores fill while keeping the editor empty.
        # Re-resolve the live node, focus it and insert Unicode text through the
        # keyboard API.  Clipboard permissions are intentionally not required.
        # A no-op fill is eligible for keyboard insertion only after the current
        # safe editor has produced two consecutive, reliable empty reads. A missing
        # or unreadable node is unknown state, never evidence of an empty draft.
        empty_composer, saw_fallback_draft = (
            await self._stable_direct_composer_with_text(
                active,
                "",
                attempts=6,
            )
        )
        if empty_composer is None:
            detail = (
                "当前会话输入框出现了未发送内容"
                if saw_fallback_draft
                else "无法连续确认当前会话输入框为空"
            )
            raise WorkerExecutionError(
                f"{detail}；已保留现场且未执行发送",
                reason="instagram_direct_composer_not_ready",
                pause_required=True,
            ) from fill_error
        active = empty_composer
        try:
            if await self._composer_text(active):
                raise WorkerExecutionError(
                    "当前会话输入框在写入期间出现了其他内容；已停止本次打招呼",
                    reason="instagram_direct_composer_not_ready",
                    pause_required=True,
                )
            click = getattr(active, "click", None)
            if callable(click):
                await click(timeout=5_000)
            await self.page.keyboard.insert_text(message)
        except WorkerExecutionError:
            raise
        except Exception as exc:
            raise WorkerExecutionError(
                "会话已打开，但招呼话术未能写入输入框；未执行发送",
                reason="instagram_direct_composer_not_ready",
                pause_required=True,
            ) from exc

        written, saw_nonmatching_text = await self._stable_direct_composer_with_text(
            active,
            expected,
        )
        if written is not None:
            return written
        if saw_nonmatching_text:
            raise WorkerExecutionError(
                "键盘写入的话术不完整；已保留输入框内容且未执行发送",
                reason="instagram_direct_composer_not_ready",
                pause_required=True,
            )
        raise WorkerExecutionError(
            "招呼话术没有稳定出现在消息框；未执行发送",
            reason="instagram_direct_composer_not_ready",
            pause_required=False,
        ) from fill_error

    async def _visible_transcript_message_count(self, message: str) -> int:
        """Count exact visible transcript bubbles while excluding the composer.

        `get_by_text(message)` also matches text currently sitting in a composer and
        identical previews in the left inbox.  Only leaf text above the visible
        composer and inside its right conversation column counts here.
        """
        count = await self.page.evaluate(
            """expected => {
                const normalize = value => String(value || '')
                    .replace(/\\r\\n?/g, '\\n')
                    .replace(/\\u00a0/g, ' ')
                    .replace(/[\\u200b\\ufeff]/g, '');
                const wanted = normalize(expected);
                const visible = element => {
                    if (!(element instanceof HTMLElement)) return false;
                    const style = window.getComputedStyle(element);
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && element.getClientRects().length > 0;
                };
                const editorSelector = [
                    'textarea',
                    '[role="textbox"]',
                    '[contenteditable="true"]',
                    '[contenteditable="plaintext-only"]',
                    '[contenteditable=""]',
                    '[data-lexical-editor="true"]',
                    '[data-slate-editor="true"]'
                ].join(',');
                const editors = Array.from(document.querySelectorAll(`main ${editorSelector.split(',').join(',main ')}`))
                    .filter(element => visible(element))
                    .filter(element => !element.closest('[role=dialog]'))
                    .filter(element => !element.hasAttribute('disabled'))
                    .filter(element => element.getAttribute('aria-disabled') !== 'true')
                    .filter(element => !element.hasAttribute('readonly'))
                    .map(element => ({element, rect: element.getBoundingClientRect()}))
                    .filter(item => item.rect.width >= 40 && item.rect.height >= 10)
                    .sort((left, right) =>
                        (left.rect.bottom - right.rect.bottom)
                        || (left.rect.right - right.rect.right)
                    );
                if (!editors.length) return 0;
                const composerRect = editors[editors.length - 1].rect;
                const roots = Array.from(document.querySelectorAll('main *'));
                return roots.filter(element => {
                    if (!visible(element)) return false;
                    if (element.closest('[role=dialog]')) return false;
                    if (element.matches(`input,${editorSelector}`)) return false;
                    if (element.closest(`input,${editorSelector}`)) return false;
                    const rect = element.getBoundingClientRect();
                    const centerX = rect.left + rect.width / 2;
                    if (centerX < composerRect.left || centerX > composerRect.right) return false;
                    if (rect.bottom > composerRect.top + 2) return false;
                    if (normalize(element.innerText || element.textContent) !== wanted) return false;
                    return !Array.from(element.children).some(
                        child => normalize(child.innerText || child.textContent) === wanted
                    );
                }).length;
            }""",
            message,
        )
        return int(count or 0)

    async def _direct_send_control(self, composer: Any | None = None) -> Any | None:
        # Production callers pass the exact already-verified composer.  The optional
        # resolver keeps this inspector usable on its own without allowing the send
        # path to silently bind a different thread/editor.
        if composer is None:
            composer = await self._current_direct_composer()
        if composer is None:
            return None
        try:
            composer_box = await composer.bounding_box()
        except Exception:
            composer_box = None
        if not composer_box:
            return None
        composer_left = float(composer_box["x"])
        composer_right = composer_left + float(composer_box["width"])
        composer_top = float(composer_box["y"])
        composer_bottom = composer_top + float(composer_box["height"])
        controls = self.page.locator('main button, main [role="button"]')
        labels = {
            "send", "send message", "发送", "发送消息", "傳送", "傳送訊息",
            "enviar", "enviar mensaje", "envoyer", "envoyer un message",
            "ส่ง", "ส่งข้อความ", "senden", "nachricht senden", "送信",
            "メッセージを送信", "보내기", "메시지 보내기",
        }
        try:
            count = min(await controls.count(), 160)
        except Exception:
            count = 0
        for index in range(count):
            control = controls.nth(index)
            try:
                if not await control.is_visible():
                    continue
                if await control.evaluate(
                    "element => Boolean(element.closest('[role=dialog]'))"
                ):
                    continue
                if str(await control.get_attribute("aria-disabled") or "").casefold() == "true":
                    continue
                if await control.get_attribute("disabled") is not None:
                    continue
                is_enabled = getattr(control, "is_enabled", None)
                if callable(is_enabled) and not await is_enabled():
                    continue
                box = await control.bounding_box()
                if not box:
                    continue
                center_x = float(box["x"]) + float(box["width"]) / 2
                center_y = float(box["y"]) + float(box["height"]) / 2
                if not (
                    composer_left - 96 <= center_x <= composer_right + 180
                    and composer_top - 56 <= center_y <= composer_bottom + 56
                ):
                    continue
                text = " ".join((await control.inner_text(timeout=1_500)).casefold().split())
                aria = " ".join(str(await control.get_attribute("aria-label") or "").casefold().split())
            except Exception:
                continue
            if text in labels or aria in labels:
                return control
        return None

    async def _send_and_confirm_greeting(
        self,
        composer: Any,
        message: str,
        *,
        username_norm: str | None = None,
        poll_attempts: int = 30,
        poll_interval: float = 0.4,
    ) -> str:
        expected = normalize_direct_message_text(message)
        active = await self._current_direct_composer(composer)
        if active is None:
            raise WorkerExecutionError(
                "右侧会话输入框没有保持可用；未写入或发送任何话术",
                reason="instagram_direct_composer_not_ready",
                pause_required=False,
            )
        try:
            before_count = await self._visible_transcript_message_count(message)
        except Exception as exc:
            raise WorkerExecutionError(
                "发送前无法读取右侧会话记录；未写入或发送任何话术",
                reason="instagram_direct_chat_not_rendered",
                pause_required=False,
            ) from exc
        active = await self._write_greeting_to_composer(active, message)

        triggered = False
        try:
            async with self._destructive_action_lease():
                # Re-resolve both the editor and recipient immediately before the
                # irreversible click/key event. This prevents a manual thread switch
                # or React node replacement between typing and sending from causing a
                # message to go to the wrong account.
                active = await self._current_direct_composer(active)
                if active is None:
                    raise WorkerExecutionError(
                        "发送前消息框已消失；未执行发送",
                        reason="instagram_direct_composer_not_ready",
                        pause_required=True,
                    )
                if await self._composer_text(active) != expected:
                    raise WorkerExecutionError(
                        "发送前消息框内容发生变化；已停止发送并保留现场",
                        reason="instagram_direct_composer_not_ready",
                        pause_required=True,
                    )
                if username_norm is not None and not await self._direct_thread_matches_recipient(
                    username_norm,
                    active,
                ):
                    raise WorkerExecutionError(
                        "发送前右侧会话账号已变化；已保留现场并取消发送",
                        reason="instagram_direct_recipient_mismatch",
                        pause_required=True,
                    )
                send_control = await self._direct_send_control(active)

                # Locating the send control is asynchronous and the operator or
                # React may switch the thread in that gap. Resolve the safe current
                # editor again and repeat both irreversible-action invariants before
                # the click/key event.
                active = await self._current_direct_composer(active)
                if active is None:
                    raise WorkerExecutionError(
                        "发送触发前消息框已消失；已保留现场且未执行发送",
                        reason="instagram_direct_composer_not_ready",
                        pause_required=True,
                    )
                if await self._composer_text(active) != expected:
                    raise WorkerExecutionError(
                        "发送触发前消息框内容发生变化；已保留现场且未执行发送",
                        reason="instagram_direct_composer_not_ready",
                        pause_required=True,
                    )
                if username_norm is not None and not await self._direct_thread_matches_recipient(
                    username_norm,
                    active,
                ):
                    raise WorkerExecutionError(
                        "发送触发前右侧会话账号已变化；已保留现场且未执行发送",
                        reason="instagram_direct_recipient_mismatch",
                        pause_required=True,
                    )
                if send_control is not None:
                    # From this line onward the browser may have received the
                    # irreversible action even when Playwright reports an error.
                    triggered = True
                    await send_control.click(timeout=10_000)
                    trigger = "send_button"
                else:
                    triggered = True
                    await active.press("Enter")
                    trigger = "enter_key"
                for _ in range(max(1, poll_attempts)):
                    if poll_interval:
                        await asyncio.sleep(poll_interval)
                    await self._guard()
                    current_count = await self._visible_transcript_message_count(message)
                    current = await self._current_direct_composer(active)
                    if current is None:
                        # A detached old node is not treated as an empty composer;
                        # wait for the replacement node so confirmation cannot be a
                        # transient false positive.
                        continue
                    active = current
                    try:
                        current_composer = await self._composer_text(current)
                    except Exception:
                        continue
                    if current_count > before_count and not current_composer:
                        recipient_still_matches = (
                            username_norm is None
                            or await self._direct_thread_matches_recipient(
                                username_norm,
                                current,
                            )
                        )
                        if recipient_still_matches:
                            return f"message_visible_in_direct_thread:{trigger}"
                raise WorkerExecutionError(
                    "未同时确认消息框已清空且话术出现在会话记录中；任务已暂停，不会跳到下一个账号",
                    reason="instagram_action_outcome_unknown",
                    pause_required=True,
                )
        except asyncio.CancelledError as exc:
            if not triggered:
                raise
            # Task cancellation is a BaseException on supported Python versions,
            # so the ordinary exception fence below cannot see it. Once the
            # click/Enter call has begun, convert cancellation to the same UNKNOWN
            # result as any other lost confirmation; the coordinator will persist
            # it before honoring the interrupted request.
            setattr(exc, "instagram_action_outcome_unknown", True)
            setattr(
                exc,
                "action_outcome_message",
                "消息发送动作被中断，可能已经执行；结果待人工确认且不会自动重试",
            )
            raise
        except WorkerExecutionError as exc:
            if not triggered or exc.code == "instagram_action_outcome_unknown":
                raise
            raise WorkerExecutionError(
                "消息发送动作可能已经执行，但后续页面检查失败；结果待人工确认，不会自动重试",
                reason="instagram_action_outcome_unknown",
                pause_required=True,
            ) from exc
        except Exception as exc:
            if not triggered:
                self._raise_page_failure(
                    exc,
                    operation="prepare Instagram greeting send",
                )
                raise WorkerExecutionError(
                    "发送前会话状态发生异常；未执行发送",
                    reason="instagram_direct_composer_not_ready",
                    pause_required=False,
                ) from exc
            # A click/key event may have reached Instagram even when Playwright
            # raises. Treat every post-trigger failure as unknown and stop the queue
            # to avoid a duplicate greeting.
            raise WorkerExecutionError(
                "消息发送动作发生异常，结果待人工确认；不会自动跳到下一个账号",
                reason="instagram_action_outcome_unknown",
                pause_required=True,
            ) from exc
