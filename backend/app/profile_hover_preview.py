"""Conservative evidence from a relationship-list hover card.

Only an exact account card with three unambiguous counts is usable. Abbreviated
counts retain a conservative lower bound, never an invented exact count.
Unknown privacy stays conservative; a rounded count can exclude only when its
lower bound exceeds the relevant saved limits.
"""
from __future__ import annotations


HOVER_PREVIEW_SCRIPT = r"""username => {
  const wanted = String(username || '').toLowerCase();
  const mayContainUsername = raw => {
    // Hidden inline decoration can split a username in textContent while
    // innerText still renders it contiguously. A subsequence is a conservative
    // rejection filter; exact visible identity is checked separately below.
    let position = 0;
    for (const letter of wanted) {
      position = raw.indexOf(letter, position);
      if (position < 0) return false;
      position++;
    }
    return true;
  };
  // One synchronous DOM observation only. Never retain card evidence between
  // polls: Instagram can replace the portal while keeping the same element.
  const rectangles = new WeakMap(), texts = new WeakMap(), visibility = new WeakMap();
  const rectOf = el => {
    if (!rectangles.has(el)) rectangles.set(el, el.getBoundingClientRect());
    return rectangles.get(el);
  };
  const textOf = el => {
    if (!texts.has(el)) texts.set(el, String(el.innerText || '').trim());
    return texts.get(el);
  };
  const visible = el => {
    if (!visibility.has(el)) {
      const r = rectOf(el), s = getComputedStyle(el);
      visibility.set(el, r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden');
    }
    return visibility.get(el);
  };
  const labels = {
    posts: '(?:posts?|帖子|贴文|貼文|게시물|投稿|โพสต์|bài viết|publicaciones|publicações|publicacoes|postingan|kiriman|publications|beiträge)',
    followers: '(?:followers?|粉丝|粉絲|팔로워|フォロワー|ผู้ติดตาม|người theo dõi|seguidores|seguidor|pengikut|abonnés)',
    following: '(?:following|已关注|关注中|关注|關注|追蹤中|追踪中|팔로잉|フォロー中|กำลังติดตาม|đang theo dõi|siguiendo|seguidos|seguindo|mengikuti|abonnements)'
  };
  const anyLabel = new RegExp(Object.values(labels).join('|'), 'gi');
  const exact = token => /^\d{1,3}(?:,\d{3})*$|^\d+$/.test(token) ? Number(token.replace(/,/g, '')) : null;
  const number = '([0-9][0-9,.]*\\s*(?:mil|rb|jt|k|m|b|千|万|萬|亿|億|만)?)';
  const parse = raw => {
    const token = String(raw || '').trim().replace(/\s+/g, '');
    const integer = exact(token);
    if (Number.isSafeInteger(integer)) return {minimum: integer};
    const abbreviated = /^(\d+(?:[.,]\d+)?)\s*(mil|rb|jt|k|m|b|千|万|萬|亿|億|만)$/i.exec(token);
    if (!abbreviated) return null;
    const scale = {k: 1000, m: 1000000, b: 1000000000, mil: 1000, rb: 1000, jt: 1000000,
      千: 1000, 万: 10000, 萬: 10000, 亿: 100000000, 億: 100000000, 만: 10000}[abbreviated[2].toLowerCase()];
    const decimals = (abbreviated[1].split(/[.,]/)[1] || '').length;
    // A one-decimal display may have been rounded or truncated. Subtract a
    // whole display unit to stay strictly below either plausible source count.
    const minimum = Math.max(0, Math.floor(Number(abbreviated[1].replace(',', '.')) * scale - scale / 10 ** decimals));
    return Number.isSafeInteger(minimum) ? {minimum, display: token} : null;
  };
  const read = (values, label, triplet) => {
    const pattern = new RegExp('(?:^|\\s)' + number + '\\s*' + labels[label] + '(?=\\s|$)', 'i');
    const reversed = new RegExp('(?:^|\\s)' + labels[label] + '\\s*' + number + '(?=\\s|$)', 'i');
    const direct = new Map();
    for (const value of values) {
      const found = pattern.exec(value) || reversed.exec(value);
      if (found) {
        const metric = parse(found[1]);
        if (metric) direct.set(JSON.stringify(metric), metric);
      }
    }
    // A short parent node can straddle two adjacent metrics: a node containing
    // "5000 已关注" may actually be the follower count followed by the next
    // label, whose own count is in a separate node. The complete rendered
    // triplet is the authority; ambiguous/conflicting local nodes reject it.
    if (direct.size > 1) return null;
    if (direct.size === 1 && JSON.stringify([...direct.values()][0]) !== JSON.stringify(triplet[label])) return null;
    return triplet[label];
  };
  const completeTriplet = text => {
    const value = text.replace(/\s+/g, ' ');
    // The three metrics must form one complete, consistently ordered group.
    // A partial group cannot certify where one value ends and the next begins.
    const kinds = ['posts', 'followers', 'following'];
    const patterns = [
      new RegExp('(?:^|\\s)' + kinds.map(kind => number + '\\s*' + labels[kind]).join('\\s*') + '(?=\\s|$)', 'gi'),
      new RegExp('(?:^|\\s)' + kinds.map(kind => labels[kind] + '\\s*' + number).join('\\s*') + '(?=\\s|$)', 'gi')
    ];
    const groups = new Map();
    for (const pattern of patterns) {
      for (const found of value.matchAll(pattern)) {
        const metrics = kinds.map((kind, index) => [kind, parse(found[index + 1])]);
        if (metrics.some(([, metric]) => metric === null)) continue;
        const group = Object.fromEntries(metrics);
        groups.set(JSON.stringify(group), group);
      }
    }
    return groups.size === 1 ? [...groups.values()][0] : null;
  };
  // Instagram can render the hover popup in a separate portal, with its name
  // as plain text rather than an anchor. Find the smallest visible compact
  // region containing this exact username and all three count labels.
  let best = null, bestArea = Infinity;
  for (const card of document.querySelectorAll('div,section,article,[role="tooltip"]')) {
    // Raw text is only a cheap rejection filter, never identity evidence.
    // Unlike innerText/geometry this does not ask the renderer for layout of
    // thousands of unrelated list/profile containers on every card poll.
    const rawText = card.textContent;
    if (typeof rawText === 'string' && !mayContainUsername(rawText.toLowerCase())) continue;
    const r = rectOf(card);
    if (r.width < 180 || r.width > 560 || r.height < 90 || r.height > 680 || !visible(card)) continue;
    const value = textOf(card);
    if (value.length > 1000 || !value.toLowerCase().split(/[^a-z0-9._]+/).includes(wanted)) continue;
    const triplet = completeTriplet(value);
    if (!triplet) continue;
    const metricTexts = [];
    for (const el of card.querySelectorAll('span,li,div')) {
      if (!visible(el)) continue;
      const text = textOf(el).replace(/\s+/g, ' ');
      if (text.length <= 44 && (text.match(anyLabel) || []).length === 1) metricTexts.push(text);
    }
    const posts = read(metricTexts, 'posts', triplet), followers = read(metricTexts, 'followers', triplet), following = read(metricTexts, 'following', triplet);
    if ([posts, followers, following].some(count => count === null)) continue;
    const area = r.width * r.height;
    if (area >= bestArea) continue;
    const privateLabel = /(?:this account is private|private account|私密帐号|私密账户|私人帳號|这是私密账户|這是私人帳號|비공개 계정)/i.test(value);
    // A public account with zero posts has no post links to prove visibility.
    // Instagram's own empty-post message is explicit public-page evidence.
    const publicEmptyPosts = posts.minimum === 0 && !posts.display &&
      /(?:还没有帖子|尚无帖子|尚無貼文|no posts yet|아직 게시물이 없습니다)/i.test(value);
    const visiblePost = [...card.querySelectorAll('a[href]')].some(a => {
      try { return /^\/(p|reel)\//.test(new URL(a.getAttribute('href'), location.href).pathname) && visible(a); }
      catch { return false; }
    });
    const bounded_counts = Object.fromEntries(Object.entries({posts, followers, following})
      .filter(([, metric]) => metric.display)
      .map(([field, metric]) => [field, {display: metric.display, minimum: metric.minimum}]));
    best = {username: wanted, posts: posts.minimum, followers: followers.minimum,
      following: following.minimum,
      visibility: privateLabel ? 'private' : (visiblePost || publicEmptyPosts) ? 'public' : 'unknown',
      evidence: 'relationship_hover_card'};
    if (Object.keys(bounded_counts).length) best.bounded_counts = bounded_counts;
    bestArea = area;
  }
  return best;
}"""


def hover_count_exclusion(preview: dict | None, settings: dict) -> tuple[str, dict] | None:
    """Return an exclusion only if visible counts prove the saved rule was crossed."""
    if not isinstance(preview, dict):
        return None
    counts = {key: preview.get(key) for key in ('followers', 'following', 'posts')}
    if any(type(value) is not int or value < 0 for value in counts.values()):
        return None
    bounded = preview.get('bounded_counts', {})
    if not isinstance(bounded, dict) or any(
        key not in counts or not isinstance(value, dict)
        or type(value.get('minimum')) is not int
        or value['minimum'] != counts[key]
        or not isinstance(value.get('display'), str)
        for key, value in bounded.items()
    ):
        return None
    visibility = preview.get('visibility')
    if visibility not in ('public', 'private'):
        visibility = 'unknown'
    # Keep the existing persisted option name for saved tasks/API compatibility.
    # It now covers both public and private accounts. A complete, account-bound
    # card can prove exact zero before its privacy label/images finish loading;
    # this rule does not require guessing privacy. A rounded floor is not zero.
    if settings.get('exclude_public_zero_posts', True) is True and counts['posts'] == 0 and 'posts' not in bounded:
        return f'{visibility}_zero_posts_excluded', {'posts': {'actual': 0, 'maximum': 0}}
    if settings.get('discard_count_limits_enabled', True) is False:
        return None
    exceeded = {}
    for field, value in counts.items():
        limits = [settings.get(f'{kind}_discard_{field}_max', 4000)
                  for kind in ((visibility,) if visibility != 'unknown' else ('public', 'private'))]
        # Unknown privacy may be rejected only when BOTH public and private
        # limits prove the same account must be excluded.
        if all(type(limit) is int and limit > 0 and value > limit for limit in limits):
            exceeded[field] = {'actual': value, 'maximum': max(limits)}
    return ('account_count_ceiling_exceeded', exceeded) if exceeded else None
