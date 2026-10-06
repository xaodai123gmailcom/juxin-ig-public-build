# R6 standalone Instagram nurture

This module is separate from collector-parent Reels activity. Collector-parent policy uses 8–20 seconds / 50%; its independent ownership and action guards remain required.

## Public contract

- Existing `studio` start/save-template commands, `kind: nurture`
- Editable `minutes`: strict integer 1–120, default 5
- Editable `concurrency`: strict integer 0–1000, default 0. Zero retains the existing “all selected windows” meaning; each batch is explicitly limited to 1000 windows rather than silently truncated
- All legacy surface/dwell/probability/action/round/schedule fields are normalized for new tasks: Reels only, dwell target 8–20 seconds, 70% probability, one round, no delayed schedule, no save/follow/comment actions
- The probability has no hidden daily-100-like cap. Duration and durable one-decision-per-Reel identity bound it
- Pre-R6 stored jobs lack the policy version marker and are not automatically reinterpreted. Scheduler pauses them, resume/retry refuses with an explicit instruction to preserve history and create a new task. Stop/cancel and pending-result review remain available

## Identity, count provenance and history

The worker-owned tab must identify a unique semantic Profile sidebar link outside main/article/dialog content and a unique numeric signed-in `ds_user_id`. It then navigates to that profile and verifies the matching heading and own Edit profile control in the profile header/summary, rechecking sidebar identity and cookie identity. Resumption must match the persisted original actor. Mid-run guards recheck that actor before effects.

Counts come only from that verified profile's counter list or exact followers/following links. Exact title values are preferred. Abbreviations without an exact value and missing values remain null; verified zero stays zero. A bounded read window permits counters to finish loading. Identity uncertainty stops before Reels; missing counters with a verified identity are retained as partial.

Each job retains its first verified `result.account_snapshot`, all attempt snapshots in `account_snapshots`, execution start (`nurture_started_at`), Reels start (`nurture_reels_started_at`), active Reels duration (`nurture_actual_seconds`), end (`nurture_finished_at`), status/failure information and observed counts. Failed initial attempts cannot overwrite an earlier verified actor. A failed/unavailable preflight clears stale latest-window counts rather than presenting them as a fresh capture.

Successful completion stores immutable `confirmed_at` alongside `nurture_finished_at`; later cleanup/status-message timestamps cannot move the report's completion date.

## Duration and action safety

Configured duration is active Reels time after own-profile preparation and Reels readiness. Profile/login preparation, paused time and cleanup are excluded. Execution start/end are separate. No new dwell starts with less than eight seconds remaining; the final active duration can therefore be slightly shorter than the configured duration. Measured duration is reported, not the nominal configured number.

A decision requires a stable playing video's permalink and media source, a unique local heart and explicit like state. Already-liked and unknown states do not trigger clicks. Positive, negative and unknown decisions are persisted once per video identity before potential revisits; restart cannot draw again. Native clicks are scoped to the exact Reel and positively unliked control, with case-insensitive Like/Unlike exclusions. Both the studio window lease and the worker's endpoint-generation action lease fence the action. Pending effects are durably recorded before clicking. Ambiguous results remain `needs_review`; there is no blind retry or unlike operation.

No stable playing video, unverified next-video transition, login challenge, actor change or lost lease stops with a truthful failure/uncertain state. Confirmed browse receipts are saved before a potentially uncertain like.

## Window ownership and storage

Start admission checks existing leases and unfinished studio jobs transactionally; a mixed busy/free selection rolls back as a whole. Same request IDs remain idempotent. The scheduler preserves bounded concurrency and does not consume a runnable slot for a busy window. The same global lease covers connection, own profile, Reels and cleanup; renewal continues through owned cleanup.

Existing close policy is preserved: completed jobs close their owned window before release; failed/cancelled jobs release and leave the window open; paused/needs-review jobs retain their window/lease. No busy window is stolen.

The full decision/action ledger remains in SQLite for recovery. Inventory snapshots expose its count and pending/unresolved action receipts, avoiding retransmission of every completed technical entry. This is a projection only, not deletion or truncation of durable history.

## Verification

- `test_standalone_nurture_r6.py`: fixed defaults/legacy override enforcement, finite bounds, dwell partition, unknown/zero counts, 70% boundary, persisted decisions, no pending replay, cookie identity, transactional admission, legacy blocks, truncated-plan rejection, receipts/lease ownership, failed preflight, immutable report date, early-return recovery and compact public projection
- `test_standalone_nurture_browser_r6.py`: six real-Chrome offline routed cases covering own-profile metrics, partial metrics, foreign/fake profile scope, actor changes, probability/like state/media changes, native no-unlike race and ambiguous-action replay prevention
- `IGAC_REQUIRE_STANDALONE_NURTURE_BROWSER=1` makes absent/unusable Chrome a hard release-gate failure. A missing or skipped local browser run does not substitute for the mandatory Windows Chrome run
- `standalone_nurture_selftest.py` and the installed-core verification gate exercise packaged execution with synthetic data; no live Instagram account or interaction is used
