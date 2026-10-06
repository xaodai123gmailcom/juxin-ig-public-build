"""Optional collector-parent Reels activity; never owns another page or job.

All effects are fenced by the collector's lifecycle and existing action lease.
A failed/ambiguous like is terminal for this visit: it is never toggled or retried.
"""
from __future__ import annotations

import asyncio
import random
import re
import time
import uuid
from urllib.parse import urlparse


class ReelsUnavailable(Exception):
    """Optional browsing cannot safely continue; collection remains authoritative."""


# Playback identity does not depend on a like control or a visible permalink.
# Unknown identities/states may browse, but never authorize a like.
REEL_PROBE = r'''() => {
 const visible=e=>{const r=e.getBoundingClientRect();return e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&r.width>0&&r.height>0&&r.top<innerHeight&&r.bottom>0&&r.left<innerWidth&&r.right>0};
 const label=e=>[e.getAttribute('aria-label'),e.getAttribute('title'),...Array.from(e.querySelectorAll('svg[aria-label]')).map(s=>s.getAttribute('aria-label'))].filter(Boolean).map(s=>s.trim());
 const on=/^(unlike|取消赞|取消讚|取消点赞)$/i,off=/^(like|赞|讚|点赞)$/i;
 const permalink=value=>{try{const u=new URL(value,location.href),m=u.pathname.match(/^\/reels?\/([A-Za-z0-9_-]+)\/?$/);return ['www.instagram.com','instagram.com'].includes(u.hostname)&&m?'/reel/'+m[1]:null}catch{return null}};
 const candidates=Array.from(document.querySelectorAll('video')).filter(v=>visible(v)&&!v.closest('[role="dialog"],[aria-modal="true"]')&&!v.paused&&v.readyState>=2&&v.videoWidth>0).sort((a,b)=>{const score=v=>{const r=v.getBoundingClientRect();return Math.min(r.bottom,innerHeight)-Math.max(r.top,0)};return score(b)-score(a)});
 const video=candidates[0];if(!video)return null;
 for(const e of document.querySelectorAll('[data-collector-reel],[data-collector-reel-heart],[data-collector-reel-video]')){e.removeAttribute('data-collector-reel');e.removeAttribute('data-collector-reel-heart');e.removeAttribute('data-collector-reel-video')}
 const source=video.currentSrc||video.src||'';
 let scope=null,button=null,key=null,scopeKey=null;
 for(let root=video.parentElement;root&&!['MAIN','BODY','HTML'].includes(root.tagName);root=root.parentElement){
  if(Array.from(root.querySelectorAll('video')).filter(visible).length!==1)break;
  const paths=Array.from(root.querySelectorAll('a[href]')).map(a=>permalink(a.href)).filter(Boolean);
  if(new Set(paths).size>1)break;
  const hearts=Array.from(root.querySelectorAll('button,[role="button"]')).filter(e=>visible(e)&&label(e).some(s=>on.test(s)||off.test(s)));
  if(new Set(paths).size===1)key=paths[0];
  if(hearts.length===1){scope=root;button=hearts[0];scopeKey=new Set(paths).size===1?paths[0]:null;if(scopeKey)break;}
 }
 key=key||permalink(location.href);
 if(!key&&!source)return null;
 const canLike=!!(scopeKey&&scopeKey===key&&scope&&button),values=button?label(button):[];
 const liked=!!button&&(button.getAttribute('aria-pressed')==='true'||values.some(s=>on.test(s)));
 const unliked=canLike&&!liked&&values.some(s=>off.test(s))&&!button.disabled&&button.getAttribute('aria-disabled')!=='true';
 key=key||'media:'+source;
 video.setAttribute('data-collector-reel-video',key);
 if(canLike){scope.setAttribute('data-collector-reel',key);button.setAttribute('data-collector-reel-heart',key)}
 return {key,liked,can_like:unliked,source,time:video.currentTime};
}'''

ADVANCE_GUARD = r'''() => {
 const visible=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const focus=document.activeElement;
 return !Array.from(document.querySelectorAll('[role="dialog"],[aria-modal="true"]')).some(visible)&&
  !(focus&&(focus.isContentEditable||['INPUT','TEXTAREA','SELECT'].includes(focus.tagName)));
}'''


def same_reel(first, current):
    return bool(first and current and first['key'] == current['key'] and first['source'] == current['source'])


async def run_parent_reels(adapter, checkpoint, *, decisions=None, claim=None, draw=None, dwell=None,
                           sleep=asyncio.sleep, clock=time.monotonic):
    """Optional owner-scoped playback; at most one 50% decision per identity."""
    decisions = decisions if decisions is not None else set()
    draw = draw or random.random
    dwell = dwell or (lambda: random.uniform(8, 20))
    excluded = 0.0
    async def active_checkpoint():
        nonlocal excluded
        before = clock()
        await checkpoint()
        excluded += max(0.0, clock() - before)
    try:
        await active_checkpoint()
        await adapter.open()
        while True:
            await active_checkpoint()
            first = await adapter.current()
            await sleep(.15)
            await active_checkpoint()
            current = await adapter.current()
            if not same_reel(first, current):
                raise ReelsUnavailable('No stable playing Reel')
            key = current['key']
            started, excluded_at_start = clock(), excluded
            if key not in decisions:
                decisions.add(key)  # Before the draw/click, including ambiguous failures.
                can_like = current.get('can_like', not current['liked'])
                selected = can_like and draw() < .5
                # Media-only identities prove playback, not a permalink for a like.
                claimed = await claim(key, selected) if claim is not None and key.startswith('/reel/') else claim is None
                await active_checkpoint()
                if claimed and selected:
                    await adapter.like(current, active_checkpoint)
            seconds = max(8.0, min(20.0, float(dwell())))
            watched = stalled = 0.0
            sampled, sampled_at, excluded_at_sample = current, started, excluded_at_start
            while True:
                await active_checkpoint()
                after = await adapter.current()
                if not same_reel(current, after):
                    raise ReelsUnavailable('Reel changed or stopped during dwell')
                now = clock()
                interval = max(0.0, now - sampled_at - (excluded - excluded_at_sample))
                # !paused alone does not prove playback: buffered/frozen media
                # must not earn watch time. A looping video's reset still moves.
                before_time, after_time = sampled.get('time'), after.get('time')
                moving = (before_time is None or after_time is None or
                          abs(float(after_time) - float(before_time)) > .001)
                if moving:
                    watched += interval
                else:
                    stalled += interval
                    if stalled >= 6.0:
                        raise ReelsUnavailable('Reel playback stalled; watch timer stopped')
                sampled, sampled_at, excluded_at_sample = after, now, excluded
                remaining = seconds - watched
                if remaining <= 0:
                    break
                await sleep(min(.25, remaining))
            await active_checkpoint()
            await adapter.advance(key, active_checkpoint)
            # An adapter acknowledgement is not proof: never dwell forever on
            # the same media after a no-op, delayed or misdirected next action.
            next_reel = await adapter.current()
            if not next_reel or next_reel['key'] == key:
                raise ReelsUnavailable('Next Reel identity not confirmed')
    finally:
        stop = getattr(adapter, 'stop', None)
        if stop is not None:
            await stop()


class ParentReelsAdapter:
    def __init__(self, worker):
        self.worker = worker
        self.page = worker.page
        self.owner_token = uuid.uuid4().hex
        self.dom_timeout = 3.0
        self.clock = time.monotonic

    async def _bounded(self, operation, *, timeout=None):
        # Keep the collector's existing tracking for a transport that ignores
        # cancellation. The manager joins healthy calls; real hangs retain the
        # old page's cleanup ownership rather than blocking handoff forever.
        limit = self.dom_timeout if timeout is None else timeout
        stage = getattr(self.worker, '_await_page_stage', None)
        if callable(stage):
            return await stage(operation, timeout=limit)
        return await asyncio.wait_for(operation, timeout=limit)

    async def _evaluate(self, expression, *args):
        return await self._bounded(self.page.evaluate(expression, *args))

    async def guard(self):
        if self.worker.page is not self.page:
            raise ReelsUnavailable('Parent page ownership changed')
        await self.worker._guard()
        url = urlparse(self.page.url)
        if url.hostname not in {'www.instagram.com', 'instagram.com'}:
            raise ReelsUnavailable('Parent left Instagram')

    async def open(self):
        await self.guard()
        await self._bounded(self.page.goto('https://www.instagram.com/reels/', wait_until='domcontentloaded', timeout=15000), timeout=16.0)
        await self.guard()
        await self._evaluate('(owner) => { document.documentElement.dataset.collectorReelsOwner = owner }', self.owner_token)
        # A bounded media-loading wait is cancellable by the manager monitor.
        deadline = self.clock() + 3.0
        for _ in range(30):
            if self.clock() >= deadline:
                break
            if await self.current():
                return
            await asyncio.sleep(.1)
        raise ReelsUnavailable('Reels did not expose playable media')

    async def current(self):
        await self.guard()
        if not re.fullmatch(r'/reels?(?:/[A-Za-z0-9_-]+)?/?', urlparse(self.page.url).path):
            raise ReelsUnavailable('Parent left Reels')
        return await self._evaluate(REEL_PROBE)

    async def stop(self):
        # A new navigation/task clears or replaces this document token. Cleanup
        # must never pause another owner or close the collector's page itself.
        if self.worker.page is not self.page or getattr(self.worker, '_page_stage_abandoned', False):
            return
        try:
            await self._evaluate(r'''owner => {
                if(document.documentElement.dataset.collectorReelsOwner !== owner ||
                   !/^\/reels?(?:\/[A-Za-z0-9_-]+)?\/?$/.test(location.pathname))return;
                for(const video of document.querySelectorAll('video'))video.pause();
                delete document.documentElement.dataset.collectorReelsOwner;
            }''', self.owner_token)
        except Exception:
            pass  # The owning collector still joins this task and closes its page.

    async def like(self, expected, checkpoint):
        key = expected['key']
        if not re.fullmatch(r'/reel/[A-Za-z0-9_-]+', key):
            raise ReelsUnavailable('Invalid Reel identity')
        async with self.worker._destructive_action_lease():
            await checkpoint()
            current = await self.current()
            if not current or current['key'] != key or current['source'] != expected['source'] or current['liked'] or current.get('can_like') is False:
                return
            # Re-resolve a currently UNLIKED heart. If the DOM changes to Unlike,
            # this locator no longer matches; it can never toggle an existing like.
            button = self.page.locator('[data-collector-reel-heart="'+key+'"]').filter(
                has=self.page.locator('svg[aria-label="Like"],svg[aria-label="赞"],svg[aria-label="讚"],svg[aria-label="点赞"]'))
            direct = self.page.locator('[data-collector-reel-heart="'+key+'"][aria-label="Like"],'
                '[data-collector-reel-heart="'+key+'"][aria-label="赞"],'
                '[data-collector-reel-heart="'+key+'"][aria-label="讚"],'
                '[data-collector-reel-heart="'+key+'"][aria-label="点赞"]')
            if await self._bounded(direct.count()) == 1:
                button = direct
            if await self._bounded(button.count()) != 1:
                raise ReelsUnavailable('No exact unliked heart')
            await checkpoint()
            final = await self.current()
            if not final or final['key'] != key or final['source'] != expected['source'] or final['liked'] or final.get('can_like') is False:
                return
            # The native locator must still be inside the exact active-video scope
            # and must exclude any Unlike state at action time (not only probe time).
            scope = self.page.locator('[data-collector-reel="'+key+'"]').filter(
                has=self.page.locator('video[data-collector-reel-video="'+key+'"]'))
            button = scope.locator('[data-collector-reel-heart="'+key+'"]:not([aria-pressed="true"]):not([aria-label="Unlike"]):not([aria-label="取消赞"]):not([aria-label="取消讚"]):not([aria-label="取消点赞"])').filter(
                has_not=self.page.locator('svg[aria-label="Unlike"],svg[aria-label="取消赞"],svg[aria-label="取消讚"],svg[aria-label="取消点赞"]')).and_(button)
            await self._bounded(button.click(timeout=1500))
            state = await self.current()
            if not state or state['key'] != key or not state['liked']:
                raise ReelsUnavailable('Like outcome ambiguous; no retry')

    async def advance(self, key, checkpoint):
        await checkpoint()
        expected = await self.current()
        if not expected or expected['key'] != key:
            raise ReelsUnavailable('Reel changed before next')
        # Instagram also exposes icon-only arrow controls. Prefer an exact
        # accessible label; when none exists use one focused native ArrowDown.
        # Never wheel-scroll, guess coordinates, or retry an ambiguous click.
        buttons = self.page.get_by_role('button', name=re.compile(
            r'^(Next|Next reel|Next video|Scroll down|Down chevron|Arrow down|下一个|下一個|下一条|下一則|向下滚动|向下捲動)$', re.I))
        visible = []
        for index in range(await self._bounded(buttons.count())):
            item = buttons.nth(index)
            if await self._bounded(item.is_visible()):
                visible.append(item)
        if len(visible) > 1:
            raise ReelsUnavailable('Ambiguous next-Reel controls')
        await checkpoint()
        final = await self.current()
        if not same_reel(expected, final) or not await self._evaluate(ADVANCE_GUARD):
            raise ReelsUnavailable('Reel or input focus changed before next')
        if visible:
            await self._bounded(visible[0].click(timeout=1500))
        else:
            video = self.page.locator('video[data-collector-reel-video]')
            if await self._bounded(video.count()) != 1:
                raise ReelsUnavailable('No unique active Reel for next key')
            await self._bounded(video.press('ArrowDown', timeout=1500))
        # Confirm two stable observations of a different actual video identity.
        # The single action has a bounded 6 second acknowledgement window; no
        # blind fallback after an issued click can skip a late-loading video.
        previous = None
        deadline = self.clock() + 6.0
        for _ in range(40):
            if self.clock() >= deadline:
                break
            await asyncio.sleep(min(.15, max(0.0, deadline - self.clock())))
            await checkpoint()
            current = await self.current()
            if self.clock() >= deadline:
                break
            if current and current['key'] != key and same_reel(previous, current):
                return
            previous = current
        raise ReelsUnavailable('Next Reel not confirmed; no repeat action')
