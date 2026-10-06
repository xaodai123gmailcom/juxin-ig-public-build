"""Read-only DOM evidence shared by collection navigation and completion."""

import json
import math


def relation_neighbour_status(expected_names, username, positioned_rows):
    """Compare an item's local identity window, without requiring fixed pixels.

    Known/duplicate accounts remain anchors. Insertions and stable reorderings
    are reconcilable; a missing previously observed neighbour is uncertainty.
    """
    expected = list(dict.fromkeys(expected_names))
    current = list(dict.fromkeys(row.get("username") for row in positioned_rows
        if isinstance(row, dict) and row.get('recommended') is not True
        and isinstance(row.get("username"), str)))
    if username not in expected or username not in current:
        return "missing_anchor"
    index = expected.index(username)
    window = expected[max(0, index - 1):index + 2]
    if any(name not in current for name in window):
        return "missing_anchor"
    index = current.index(username)
    return ("confirmed" if current[max(0, index - 1):index + 2] == window
        else "neighbours_changed")


def relation_position_status(anchors, positioned_rows, shift):
    """Confirm the retained neighbours in the next atomic DOM snapshot."""
    if not isinstance(anchors, list) or not anchors:
        return "missing_anchor"
    if not isinstance(positioned_rows, list):
        return "missing_anchor"
    rows = {row.get("username"): row for row in positioned_rows if isinstance(row, dict)}
    positions = []
    for anchor in anchors:
        current = rows.get(anchor.get("username"))
        if current is None:
            return "missing_anchor"
        actual, expected = current.get("y"), anchor.get("expected_y")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in (actual, expected)):
            return "missing_anchor"
        positions.append((actual, expected))
    if any(right[0] <= left[0] for left, right in zip(positions, positions[1:])):
        return "anchor_order_changed"
    tolerance = max(12, abs(shift) * .15) if type(shift) in (int, float) and math.isfinite(shift) else 12
    if any(abs(actual - expected) > tolerance for actual, expected in positions):
        return "anchor_position_changed"
    return "confirmed"

# An anchor with display:contents has no box of its own, even though its text or
# avatar is rendered. Treat those descendants as evidence for the same link;
# invisible descendants and empty wrappers still provide no visible evidence.
_RELATION_VISIBLE = r"""
  const insideClip = (el, rect) => {
    // Layout boxes outside an overflow viewport still have width/height.
    // They must not be queued as visible rows: hovering such a link makes
    // Playwright scroll the dialog and can recycle later rows in this frame.
    const view = el.ownerDocument?.defaultView;
    if (view && Number.isFinite(view.innerHeight) && view.innerHeight > 0) {
      const centerY = (rect.top + rect.bottom) / 2;
      if (centerY <= 1 || centerY >= view.innerHeight - 1) return false;
    }
    for (let parent = el.parentElement; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (/^(auto|scroll|hidden|clip|overlay)$/.test(style.overflowY)) {
        const clip = parent.getBoundingClientRect();
        const centerY = (rect.top + rect.bottom) / 2;
        if (centerY <= clip.top + 1 || centerY >= clip.bottom - 1) return false;
      }
      if (/^(auto|scroll|hidden|clip|overlay)$/.test(style.overflowX)) {
        const clip = parent.getBoundingClientRect();
        const centerX = (rect.left + rect.right) / 2;
        if (centerX <= clip.left + 1 || centerX >= clip.right - 1) return false;
      }
    }
    return true;
  };
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse') return false;
    if (r.width > 0 && r.height > 0) return true;
    if (s.display !== 'contents') return false;
    for (const child of el.childNodes) {
      if (child.nodeType === 1 && visible(child)) return true;
      if (child.nodeType === 3 && child.textContent.trim()) {
        const range = document.createRange();
        range.selectNodeContents(child);
        if (Array.from(range.getClientRects()).some(rect => rect.width > 0 && rect.height > 0)) return true;
      }
    }
    return false;
  };
  const visibleRow = el => {
    if (!visible(el)) return false;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) return insideClip(el, r);
    if (getComputedStyle(el).display !== 'contents') return false;
    for (const child of el.childNodes) {
      if (child.nodeType === 1 && visibleRow(child)) return true;
      if (child.nodeType === 3 && child.textContent.trim()) {
        const range = document.createRange();
        range.selectNodeContents(child);
        if (Array.from(range.getClientRects()).some(rect =>
          rect.width > 0 && rect.height > 0 && insideClip(el, rect))) return true;
      }
    }
    return false;
  };
  const rowBox = el => {
    const rect = el.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) return rect;
    for (const child of el.childNodes || []) {
      if (child.nodeType === 1 && visibleRow(child)) return rowBox(child);
      if (child.nodeType === 3 && child.textContent.trim()) {
        const range = document.createRange(); range.selectNodeContents(child);
        const box = Array.from(range.getClientRects()).find(r =>
          r.width > 0 && r.height > 0 && insideClip(el, r));
        if (box) return box;
      }
    }
    return rect;
  };
  const relationContextLink = (link, root) => {
    const prefix = /^(?:followed by\s|seguido(?:s|a|as)? por\s|suivi(?:e|s|es)? par\s|abonniert von\s|关注者包括|關注者包括)/i;
    for (let parent = link.parentElement; parent && parent !== root; parent = parent.parentElement) {
      if (parent.querySelector('img,button,[role="button"],input')) break;
      const text = String(parent.innerText || parent.textContent || '').normalize('NFKC').trim();
      if (prefix.test(text)) return true;
    }
    return false;
  };
  const relationAccountRows = (links, root) => {
    // Avatar and username links belong to one row. At a clipping edge one can
    // disappear before the other; their different centres are not a row jump.
    const ancestors = new Map();
    const identity = link => new URL(link.getAttribute('href'), 'https://www.instagram.com')
      .pathname.replace(/^\//, '').replace(/\/$/, '').toLowerCase();
    for (const link of links) {
      const name = identity(link);
      for (let parent = link.parentElement; parent && parent !== root; parent = parent.parentElement) {
        if (!ancestors.has(parent)) ancestors.set(parent, new Set());
        ancestors.get(parent).add(name);
      }
    }
    return links.map(link => {
      let row = null, semanticRow = null, positionBoundary = false;
      for (let parent = link.parentElement; parent && parent !== root; parent = parent.parentElement) {
        if (ancestors.get(parent)?.size !== 1) break;
        // Recommendation labels belong to the whole single-account card, even
        // when that card clips its content. A positional viewport boundary must
        // not turn an inline label into a heading for every later account.
        semanticRow = parent;
        // A single account does not make its stationary viewport a moving row.
        const overflow = getComputedStyle(parent).overflowY;
        if (!positionBoundary) {
          if (parent.clientHeight > 0 && /^(auto|scroll|overlay)$/.test(overflow)) {
            positionBoundary = true;
          } else {
            // Clipping is also commonly a property of the moving account card
            // itself; use that card before stopping ascent beyond its boundary.
            row = parent;
            if (parent.clientHeight > 0 && /^(hidden|clip)$/.test(overflow)) positionBoundary = true;
          }
        }
      }
      return {link, row, semanticRow};
    });
  };
"""

# Read relationship rows and their recommendation labels in one DOM snapshot.
# A profile link alone does not prove that an account belongs to this list.
RELATION_ROWS_SCRIPT = r"""root => {""" + _RELATION_VISIBLE + r"""
  const name = link => {
    try {
      const url = new URL(link.getAttribute('href') || '', 'https://www.instagram.com/');
      if (!['www.instagram.com', 'instagram.com'].includes(url.hostname)) return null;
      const match = url.pathname.match(/^\/([a-zA-Z0-9._]{1,30})\/?$/);
      if (!match || /^(accounts|direct|explore|reels|stories|about|legal|privacy|terms)$/i.test(match[1])) return null;
      return match[1].toLowerCase();
    } catch { return null; }
  };
  const normalize = text => String(text || '').normalize('NFKC').trim().toLowerCase().replace(/\s+/g, '');
  const labels = new Set([
    '为你推荐','為你推薦','为您推荐','為您推薦','推荐用户','推薦用戶','推荐账户','推薦帳號',
    'suggested for you','suggestions for you','suggested accounts','recommended for you',
    'people you may know','แนะนำสำหรับคุณ','おすすめ','おすすめのアカウント','회원님을 위한 추천',
    'sugerencias para ti','sugestões para você','suggestions pour vous','vorschläge für dich'
  ].map(normalize));
  const footers = new Set([
    '查看所有推荐用户','查看全部推荐用户','查看所有推薦用戶','查看全部推薦用戶',
    '查看所有推荐','查看全部推荐','查看所有推薦','查看全部推薦',
    'see all suggestions','see all suggested accounts','view all suggestions'
  ].map(normalize));
  const elements = Array.from(root.querySelectorAll('*'));
  const order = new Map(elements.map((node, index) => [node, index]));
  const profileLinks = Array.from(root.querySelectorAll('a[href]')).filter(link => name(link));
  // Inline mutual-follow context may contain links to other accounts. Those
  // links describe the row; they are not additional members of this source list.
  // Match only the local, explicit context phrase. A display name elsewhere in
  // a card, or the same username in its own row, must not be filtered globally.
  const contextLink = link => relationContextLink(link, root);
  const contextLinks = new Set(profileLinks.filter(contextLink));
  const allLinks = profileLinks.filter(link => !contextLinks.has(link));
  const links = allLinks.filter(visibleRow);
  const visibleLinks = new Set(links);
  const rows = relationAccountRows(allLinks, root).filter(item => visibleLinks.has(item.link));
  const exactLabel = node => labels.has(normalize(node.textContent))
    && !node.closest('a[href],button,[role="button"]')
    && !node.querySelector('a[href],button,[role="button"]')
    && visible(node) && labels.has(normalize(node.innerText || node.textContent));
  const markers = elements.filter(exactLabel);
  const headings = markers.filter(node => !rows.some(item => item.semanticRow?.contains(node)));
  const boundary = headings.length ? Math.min(...headings.map(node => order.get(node))) : Infinity;
  for (const item of rows) {
    item.recommended = order.get(item.link) > boundary || Boolean(item.semanticRow &&
      markers.some(marker => item.semanticRow.contains(marker)));
  }
  // Do not let one interleaved recommendation end a list containing later real
  // rows. A footer/title or several consecutive recommendation cards provides
  // semantic tail evidence; the caller must still prove the physical bottom.
  const first = rows.findIndex(item => item.recommended);
  const tail = first < 0 ? [] : rows.slice(first);
  const onlyRecommendations = tail.length > 0 && tail.every(item => item.recommended);
  const recommendationNames = new Set(tail.filter(item => item.recommended).map(item => name(item.link)));
  const footer = elements.some(node => footers.has(normalize(node.textContent))
    && order.get(node) > (tail.length ? order.get(tail[0].link) : Infinity)
    && visible(node) && footers.has(normalize(node.innerText || node.textContent)));
  const positioned = new Map();
  for (const item of rows) {
    const username = name(item.link), rect = rowBox(item.row || item.link);
    if (!positioned.has(username)) positioned.set(username, {
      username, href: item.link.getAttribute('href') || '', y: (rect.top + rect.bottom) / 2,
      recommended: item.recommended
    });
  }
  const ordered = [...positioned.values()].sort((a, b) => a.y - b.y);
  return {
    hrefs: ordered.filter(item => !item.recommended).map(item => item.href),
    positioned_rows: ordered,
    recommendations_reached: onlyRecommendations &&
      (headings.length > 0 || footer || recommendationNames.size >= 2),
    diagnostics: {
      profile_links: profileLinks.length,
      visible_profile_links: profileLinks.filter(visibleRow).length,
      context_links: contextLinks.size,
      candidate_accounts: new Set(rows.filter(item => !item.recommended).map(item => name(item.link))).size,
      recommended_accounts: new Set(rows.filter(item => item.recommended).map(item => name(item.link))).size
    }
  };
}"""

GUARD_EVIDENCE_SCRIPT = r"""body => {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const result = [];
  for (const el of body.querySelectorAll('[role="alertdialog"], [role="alert"], [role="dialog"], form, h1, h2, [role="heading"]')) {
    if (!visible(el) || el.closest('article, [role="row"], [contenteditable="true"], main header')) continue;
    if (el.querySelector('article, [role="row"], [contenteditable="true"]')) continue;
    // A relationship/post dialog is user content, not a blocking system notice.
    if (el.querySelectorAll('a[href]').length > 1) continue;
    const text = (el.innerText || '').trim();
    if (text && text.length <= 2000) result.push(text);
  }
  return result.slice(0, 20);
}"""

_RELATION_SCROLLER = _RELATION_VISIBLE + r"""
  const rows = [...node.querySelectorAll('a[href]')].filter(a => {
    // Offscreen rows still identify their scroll container during delayed
    // repaint. Only RELATION_ROWS_SCRIPT may admit candidates, and it keeps
    // using visibleRow. Losing that container must not turn the header shell
    // into a false physical bottom between two rendered frames.
    if (!visible(a)) return false;
    try {
      const url = new URL(a.getAttribute('href'), 'https://www.instagram.com');
      return ['www.instagram.com', 'instagram.com'].includes(url.hostname) &&
        /^\/[\w.]{1,30}\/?$/.test(url.pathname) &&
        !/^\/(accounts|direct|explore|reels|stories|about|legal|privacy|terms)\/?$/i.test(url.pathname);
    } catch { return false; }
  });
  const visibleProfiles = new Set(rows.filter(visibleRow));
  const visibleProfileCount = visibleProfiles.size;
  // Walk account ancestors, not arbitrary divs: recent layouts also use section
  // and role=list containers. Keep the search inside this relationship surface.
  // Compute each ancestor's eligibility once, and accumulate unique account
  // names while walking up. Preloaded offscreen rows must not turn this into
  // an all-wrappers x all-rows containment scan.
  const ancestorEligibility = new Map();
  const candidateNames = new Map();
  const candidateVisibleNames = new Map();
  for (const row of rows) {
    const name = new URL(row.getAttribute('href'), 'https://www.instagram.com').pathname
      .replace(/\/$/, '').toLowerCase();
    for (let el = row.parentElement; el && node.contains(el); el = el.parentElement) {
      if (!ancestorEligibility.has(el)) ancestorEligibility.set(el,
        el.clientHeight > 0 && /^(auto|scroll|overlay)$/.test(getComputedStyle(el).overflowY) && visible(el));
      if (ancestorEligibility.get(el)) {
        if (!candidateNames.has(el)) {
          candidateNames.set(el, new Set());
          candidateVisibleNames.set(el, new Set());
        }
        candidateNames.get(el).add(name);
        if (visibleProfiles.has(row)) candidateVisibleNames.get(el).add(name);
      }
      if (el === node) break;
    }
  }
  let candidates = [...candidateNames.keys()];
  // overflow:auto alone is not proof of a scrollable viewport. An inner wrapper
  // that fits its content must not win over the actual outer scrolling list.
  const overflowing = candidates.filter(el => el.scrollHeight > el.clientHeight + 2);
  if (overflowing.length) candidates = overflowing;
  // Prefer the innermost real scrolling viewport containing the most account rows.
  candidates.sort((a, b) => {
    // Avatar/name anchors often point to the same profile. Counting those twice
    // lets a dialog header outweigh a virtualized viewport with one real row,
    // falsely proving the shell's bottom while the list itself is still at top.
    // Prefer current visible evidence. Offscreen rows are a fallback for a
    // repaint gap, not extra votes for unrelated preloaded header/footer rows.
    const count = el => candidateVisibleNames.get(el).size || candidateNames.get(el).size;
    // A dialog header/footer may add another profile link outside the list.
    // That single link must not make its outer shell win over the list viewport.
    if (a.contains(b) && count(b) >= count(a) / 2) return 1;
    if (b.contains(a) && count(a) >= count(b) / 2) return -1;
    const difference = Number(candidateVisibleNames.get(b).size > 0) -
      Number(candidateVisibleNames.get(a).size > 0) || count(b) - count(a);
    return difference || (a.contains(b) ? 1 : b.contains(a) ? -1 : 0);
  });
  let scrollable = candidates[0];
  if (!scrollable && rows.length && visible(node) && node.clientHeight > 0 &&
      node.scrollHeight <= node.clientHeight + 2 &&
      !['hidden', 'clip'].includes(getComputedStyle(node).overflowY)) scrollable = node;
  // A virtualized list may temporarily remove every row while keeping its
  // scrollable spacer. That unanchored viewport is uncertainty, never proof
  // that its surrounding header/footer is the whole list. Wait for repaint;
  // do not move an unrelated wrapper or latch this transient result in a cache.
  if (scrollable && [...scrollable.querySelectorAll('*')].some(el =>
      el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 2 && visible(el) &&
      /^(auto|scroll|overlay)$/.test(getComputedStyle(el).overflowY) &&
      !candidateNames.has(el))) scrollable = null;
  if (!scrollable) return {valid: false, bottom: false, moved: false,
    visible_profile_links: visibleProfileCount, scroll_candidates: candidates.length};
  const box = el => {
    const rect = el.getBoundingClientRect();
    const top = Number.isFinite(rect.top) ? rect.top : 0;
    return {top, bottom: Number.isFinite(rect.bottom) ? rect.bottom : top + rect.height,
      height: rect.height};
  };
  const scale = el => {
    const layout = el.offsetHeight || el.clientHeight;
    const value = box(el).height / layout;
    return Number.isFinite(value) && value > 0 ? value : 1;
  };
  const viewport = () => {
    const own = box(scrollable);
    let top = own.top, bottom = own.bottom;
    const parents = [];
    for (let el = scrollable.parentElement; el; el = el.parentElement) {
      const overflow = getComputedStyle(el).overflowY;
      if (/^(auto|scroll|overlay|hidden|clip)$/.test(overflow)) {
        const clip = box(el);
        top = Math.max(top, clip.top);
        bottom = Math.min(bottom, clip.bottom);
        parents.push({el, clip, canScroll: overflow !== 'clip'});
      } else if (el === scrollable.ownerDocument?.scrollingElement) {
        parents.push({el, clip: {top: 0, bottom: el.clientHeight}, canScroll: true});
      }
    }
    const view = scrollable.ownerDocument?.defaultView;
    if (view && Number.isFinite(view.innerHeight) && view.innerHeight > 0) {
      top = Math.max(top, 0); bottom = Math.min(bottom, view.innerHeight);
    }
    return {top, bottom, height: Math.max(0, bottom - top), own, parents};
  };
  const tailClipped = area => area.own.bottom > area.bottom + 2 && (
    scrollable.scrollHeight > scrollable.clientHeight + 2 ||
    [...scrollable.querySelectorAll('a[href]')].some(link => visible(link) && box(link).bottom > area.bottom + 2)
  );
  let continuity = {};
  const advance = () => {
    const area = viewport();
    if (area.height <= 2) return false;
    let target = scrollable;
    if (scrollable.scrollTop + scrollable.clientHeight >= scrollable.scrollHeight - 3 && tailClipped(area)) {
      // An inner scrollbar can be at bottom while its final rows remain clipped
      // by an outer viewport. Reveal that tail by moving only a forward-capable
      // clipping ancestor. Never rewind the inner list or reset the source.
      const parent = area.parents.find(item => item.canScroll &&
        item.clip.bottom < area.own.bottom - 2 &&
        item.el.scrollTop + item.el.clientHeight < item.el.scrollHeight - .5);
      if (!parent) return false;
      target = parent.el;
    }
    const before = target.scrollTop;
    const positioned = new Map();
    const accounts = relationAccountRows(rows.filter(link => !relationContextLink(link, node)), node);
    for (const item of accounts.filter(item => scrollable.contains(item.link) && visibleRow(item.link))) {
      const link = item.link;
      const username = new URL(link.getAttribute('href'), 'https://www.instagram.com').pathname
        .replace(/^\//, '').replace(/\/$/, '').toLowerCase();
      const rect = rowBox(item.row || link), linkRect = rowBox(link);
      if (!positioned.has(username)) positioned.set(username, {
        username, y: (rect.top + rect.bottom) / 2, visibleLinkYs: []
      });
      // Position continuity uses the stable account card, but that identity is
      // observable only while an actual avatar/name link survives clipping. A
      // short inline link can leave the viewport before its taller card centre.
      positioned.get(username).visibleLinkYs.push((linkRect.top + linkRect.bottom) / 2);
    }
    const ordered = [...positioned.values()].sort((a, b) => a.y - b.y);
    const acknowledged = typeof acknowledgedNames === 'undefined' ? null : acknowledgedNames;
    if (acknowledged && ordered.some(row => !acknowledged.has(row.username))) {
      // A repaint after the durable write must be consumed before this scroll.
      continuity = {continuity_pending: true};
      return false;
    }
    // clientHeight may be much larger than the area actually read by visibleRow.
    // Keep overlap within that same clipped area, in the target's layout units.
    let step = area.height * .65;
    if (acknowledged) {
      // Prefer two neighbours, but a clipped/narrow list can show only one.
      // Do not scroll its sole identity out of view and then demand an anchor
      // that our own movement removed. A genuinely insufficient overlap waits
      // in place under the reader's no-progress deadline instead of jumping.
      const retainable = ordered.filter(row => row.visibleLinkYs.some(y => y > area.top + 2 && y < area.bottom - 2));
      const survivingLinkY = row => Math.max(...row.visibleLinkYs.filter(y => y > area.top + 2 && y < area.bottom - 2));
      const last = retainable[retainable.length - 1];
      const safeStep = last ? survivingLinkY(last) - area.top - 3 : 0;
      if (safeStep <= scale(target) * .5) {
        continuity = {continuity_pending: true};
        return false;
      }
      const anchor = retainable[Math.max(0, retainable.length - 2)];
      const anchorStep = survivingLinkY(anchor) - area.top - 4;
      step = Math.min(step, anchorStep > scale(target) * .5 ? anchorStep : safeStep);
    }
    const maximum = target.scrollHeight - target.clientHeight;
    let next = Math.min(maximum,
      before + (acknowledged ? step / scale(target) : Math.max(1, step / scale(target))));
    // Do not stop a subpixel short of the final virtual-row boundary merely
    // because bottom measurement allows rounding tolerance. Reach the actual
    // clamp when it is nearby and at least one readable identity still overlaps.
    const endShift = (maximum - before) * scale(target);
    if (maximum > next && maximum - next <= 3 && ordered.some(row =>
      row.visibleLinkYs.some(y => y - endShift > area.top + 2 && y - endShift < area.bottom - 2))) next = maximum;
    target.scrollTop = next;
    const shift = (target.scrollTop - before) * scale(target);
    continuity = {anchor_rows: ordered.filter(row => row.visibleLinkYs.some(y =>
      y - shift > area.top + 2 && y - shift < area.bottom - 2)).map(row => ({
        username: row.username, y: row.y, expected_y: row.y - shift
      })), scroll_shift: shift};
    return target.scrollTop > before + .5;
  };
  const reset = () => {
    scrollable.scrollTop = 0;
    // Initial list positioning also reveals its first row if an enclosing
    // viewport retained a prior offset. This runs only for the explicit reset.
    for (const item of viewport().parents) {
      if (!item.canScroll || item.el.scrollTop <= 0) continue;
      const hiddenAbove = item.clip.top - box(scrollable).top;
      if (hiddenAbove > 1) item.el.scrollTop = Math.max(0,
        item.el.scrollTop - hiddenAbove / scale(item.el));
    }
  };
"""


def relation_scroll_script(action: str, acknowledged_names: list[str] | None = None) -> str:
    operations = {
        "reset": "reset();",
        "advance": "moved = advance();",
        "end": "scrollable.scrollTop = scrollable.scrollHeight;",
        "measure": "",
    }
    acknowledgement = ("const acknowledgedNames = new Set(" + json.dumps(acknowledged_names) + ");\n"
                       if acknowledged_names is not None else "")
    return "node => {\n// relation-action: " + action + "\n" + acknowledgement + _RELATION_SCROLLER + "\nconst before = scrollable.scrollTop;\nlet moved = false;\n" + operations[action] + r"""
      const area = viewport();
      return {valid: true, moved: moved || scrollable.scrollTop > before + 1, ...continuity,
        visible_profile_links: visibleProfileCount, scroll_candidates: candidates.length,
        visible_height: area.height, viewport_top: area.top, viewport_bottom: area.bottom,
        tail_clipped: tailClipped(area),
        top: scrollable.scrollTop, height: scrollable.scrollHeight, client: scrollable.clientHeight,
        bottom: area.height > 2 && !tailClipped(area) &&
          scrollable.scrollTop + scrollable.clientHeight >= scrollable.scrollHeight - 3};
    }"""
