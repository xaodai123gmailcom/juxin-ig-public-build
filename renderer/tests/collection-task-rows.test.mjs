import assert from "node:assert/strict";
import test from "node:test";
import { collectionTaskRows, collectionFailurePresentation, collectionRowStatus, collectionWindowProgress, collectionRecoveryAction, collectionRecoveryDetails, collectionModeProgressText, collectionModeProgressItems, collectionModeUnseenCount } from "../src/collection-task-rows.ts";

const task = (targets, profiles = [{ profile_id: "w1", state: "idle" }]) => ({
  id: "task", status: "running", targets,
  windows: [{ profile_id: "w1" }, { profile_id: "w2" }],
  runtime: { profile_states: profiles },
});

test("legacy completed target with idle window is removed even before archive migration", () => {
  const input = task([{ id: "done", status: "completed", current_window_id: "w1",
    mode_progress: { followers: { source_total: 143, processed: 124 } } }]);
  assert.deepEqual(collectionTaskRows(input), []);
  assert.equal(input.targets[0].mode_progress.followers.processed, 124);
  assert.equal(input.targets.length, 1); // History remains intact.
});

test("auto-archived target leaves no orphan idle card while another window keeps working", () => {
  const input = task([
    { id: "done", status: "completed", current_stage: "completed_archived" },
    { id: "next", status: "running", current_window_id: "w2" },
  ], [{ profile_id: "w1", state: "idle" },
    { profile_id: "w2", state: "working", current_target_id: "next" }]);
  assert.deepEqual(collectionTaskRows(input).map((row) => row.target.id), ["next"]);
});

test("window reuse selects next live target despite stale runtime pointing to completed target", () => {
  const rows = collectionTaskRows(task([
    { id: "done", status: "completed", current_window_id: "w1" },
    { id: "next", status: "running", current_window_id: "w1" },
  ], [{ profile_id: "w1", state: "working", current_target_id: "done" }]));
  assert.equal(rows.length, 1);
  assert.equal(rows[0].target.id, "next");
});

test("numeric progress and idle state never hide unfinished targets", () => {
  const rows = collectionTaskRows(task([{ id: "pending", status: "recoverable",
    preferred_window_id: "w1", mode_progress: { followers: { source_total: 124, processed: 124 } } }]));
  assert.equal(rows.length, 1);
  assert.equal(collectionRowStatus("recoverable", "idle", "running"), "recoverable");
  assert.equal(collectionRowStatus("running", "idle", "running"), "running");
  assert.equal(collectionRowStatus("completed", "idle", "running"), "completed");
});

test("all-completed task stays hidden after restart with persisted window pool", () => {
  assert.deepEqual(collectionTaskRows({ ...task([]), status: "completed" }), []);
});

test("bounded target projection retains live worker and manually blocked target", () => {
  assert.equal(collectionTaskRows(task([], [{ profile_id: "w1", state: "working",
    current_target_id: "outside-page" }])).length, 1);
  assert.equal(collectionTaskRows(task([{ id: "manual", status: "stopped",
    current_stage: "interrupted_recoverable", manual_recovery_required: true,
    preferred_window_id: "w1" }])).length, 1);
});

test("a transferred target's counters appear only on its current window", () => {
  const rows = collectionTaskRows(task([{ id: "transferred", status: "running",
    current_window_id: "w2", preferred_window_id: "w1",
    mode_progress: { following: { source_total: 17, processed: 12 } } }], [
    { profile_id: "w1", state: "idle", current_target_id: "transferred" },
    { profile_id: "w2", state: "working", current_target_id: "transferred" },
  ]));
  assert.deepEqual(rows.map(row => [row.profileId, row.target?.id]), [["w2", "transferred"]]);
  assert.equal(rows[0].target.mode_progress.following.processed, 12);
});

test("a preferred window keeps an unclaimed recovery target visible", () => {
  const rows = collectionTaskRows(task([{ id: "recovery", status: "recoverable",
    current_window_id: null, preferred_window_id: "w1" }]));
  assert.deepEqual(rows.map(row => [row.profileId, row.target?.id]), [["w1", "recovery"]]);
});

test("a pending failure for the exact recoverable target shares its card and leaves checkpoint controls accessible", () => {
  const original = task([{ id: "target", status: "recoverable", manual_recovery_required: true,
    current_window_id: null, preferred_window_id: "w1", mode_progress: { followers: { processed: 18, saved: 15 } } }]);
  const failure = { id: "generation", kind: "failure", queue_state: "available",
    source_task_id: "task", source_target_id: "target", username: "mamas__mia" };
  const result = collectionFailurePresentation(collectionTaskRows(original), [failure]);
  assert.equal(result.rows.length, 1);
  assert.equal(result.rows[0].target.mode_progress.followers.processed, 18);
  assert.equal(result.rows[0].failure, failure);
  assert.deepEqual(result.orphanFailures, []);
});

test("a failure from another task or target never merges into the current window card", () => {
  const original = task([{ id: "target", status: "recoverable", manual_recovery_required: true,
    current_window_id: null, preferred_window_id: "w1" }]);
  const unrelated = [
    { id: "other-task", kind: "failure", queue_state: "available", source_task_id: "old", source_target_id: "target" },
    { id: "other-target", kind: "failure", queue_state: "available", source_task_id: "task", source_target_id: "other" },
  ];
  const result = collectionFailurePresentation(collectionTaskRows(original), unrelated);
  assert.equal(result.rows[0].failure, null);
  assert.deepEqual(result.orphanFailures, unrelated);
  const noPendingRecovery = collectionFailurePresentation(collectionTaskRows(task([{ id: "target", status: "recoverable",
    preferred_window_id: "w1" }])), [{ id: "old-failure", kind: "failure", queue_state: "available",
    source_task_id: "task", source_target_id: "target" }]);
  assert.equal(noPendingRecovery.rows[0].failure, null);
  assert.equal(noPendingRecovery.orphanFailures.length, 1);
});

test("one stalled window stays stalled while another window advances", () => {
  const now = Date.parse("2026-09-15T12:10:00Z");
  const target = { id: "target" };
  assert.equal(collectionWindowProgress({ current_target_id: "target", last_progress_at: "2026-09-15T12:00:00Z" }, target, "running", now).stalled, true);
  assert.equal(collectionWindowProgress({ current_target_id: "other", last_progress_at: "2026-09-15T12:09:59Z" }, { id: "other" }, "running", now).stalled, false);
});

test("idle, paused, network waits and unknown progress do not produce a stalled warning", () => {
  const now = Date.parse("2026-09-15T12:10:00Z");
  const old = { last_progress_at: "2026-09-15T12:00:00Z" };
  for (const state of ["paused", "waiting_network", "auth_required", "queued", "pending", "idle"])
    assert.equal(collectionWindowProgress(old, { id: "target" }, state, now).stalled, false);
  assert.deepEqual(collectionWindowProgress({}, { id: "target" }, "running", now), { lastProgressAt: null, stalled: false });
});

test("a new target cannot inherit the previous target's runtime progress", () => {
  const result = collectionWindowProgress({ current_target_id: "old", last_progress_at: "2026-09-15T12:00:00Z" },
    { id: "new" }, "running", Date.parse("2026-09-15T12:10:00Z"));
  assert.deepEqual(result, { lastProgressAt: null, stalled: false });
});

test("each recoverable state exposes one matching recovery action", () => {
  const normal = { stalled: false, authRequired: false, pageRecoveryExhausted: false };
  for (const [state, action, label] of [["waiting_network", "resume", "立即重试"],
    ["paused", "resume", "继续"], ["recoverable", "resume", "继续"], ["failed", "restart", "继续"], ["stopped", "restart", "继续"]]) {
    const result = collectionRecoveryAction(state, normal);
    assert.equal(result.action, action); assert.equal(result.label, label);
  }
  assert.equal(collectionRecoveryAction("completed", normal), null);
  assert.equal(collectionRecoveryAction("unknown", normal), null);
  for (const state of ["running", "working", "collecting", "processing", "active", "starting", "opening", "queued", "pending"])
    assert.equal(collectionRecoveryAction(state, normal), null, state);
  assert.equal(collectionRecoveryAction("running", { ...normal, stalled: true }).label, "检查点重启");
  assert.equal(collectionRecoveryAction("manual_intervention", { ...normal, authRequired: true, pageRecoveryExhausted: true }).label, "处理后继续");
});

test("healthy work does not invite an unnecessary resume or page restart", () => {
  assert.equal(collectionRecoveryAction("running", { stalled: false, authRequired: false, pageRecoveryExhausted: false }), null);
});

test("page recovery exhaustion with a live automatic waiter stays automatic and keeps the candidate visible", () => {
  const profile = { state: "waiting_network", recovery_kind: "instagram_page", reason: "instagram_page_recovery_exhausted",
    original_reason: "instagram_profile_not_ready", next_retry_at: "2026-09-15T12:15:00Z",
    candidate_username: "@candidate", source_discovery_complete: true, last_progress_at: "2026-09-15T12:00:00Z" };
  const state = collectionRowStatus("waiting_network", profile.state, "running");
  const info = collectionRecoveryDetails(profile, state);
  assert.equal(info.automatic, true);
  assert.equal(info.manualRequired, false);
  assert.equal(info.pageRecovery, true);
  assert.equal(info.nextRetryAt, profile.next_retry_at);
  assert.equal(info.reasonLabel, "账号主页尚未加载完成");
  assert.equal(info.candidateUsername, "candidate");
  assert.equal(info.sourceDiscoveryComplete, true);
  assert.equal(collectionWindowProgress(profile, { id: "target" }, state, Date.parse("2026-09-15T12:10:00Z")).stalled, false);
});

test("network and list cooldowns show their actual cause without requiring login", () => {
  for (const [kind, reason, expectedPage, label] of [
    ["network", "instagram_network_unavailable", false, "网络暂时不可用"],
    ["instagram_page", "instagram_relationship_list_no_progress", true, "名单暂时没有继续推进"],
    ["instagram_page", "instagram_surface_no_progress", true, "页面暂时没有继续推进"],
  ]) {
    const info = collectionRecoveryDetails({ state: "waiting_network", recovery_kind: kind, reason }, "waiting_network");
    assert.equal(info.automatic, true);
    assert.equal(info.manualRequired, false);
    assert.equal(info.pageRecovery, expectedPage);
    assert.equal(info.reasonLabel, label);
    assert.equal(info.manualMessage, "");
  }
});

test("manual page uncertainty does not falsely say BitBrowser login has expired", () => {
  const info = collectionRecoveryDetails({ state: "manual_intervention", current_stage: "manual_required",
    recovery_kind: "profile_intervention", reason: "instagram_page_recovery_exhausted", original_reason: "unknown_failure" }, "manual_intervention");
  assert.equal(info.manualRequired, true);
  assert.equal(info.automatic, false);
  assert.equal(info.nextRetryAt, null);
  assert.match(info.manualMessage, /检查该窗口页面/);
  assert.doesNotMatch(info.manualMessage, /登录|验证/);
  assert.equal(collectionRecoveryAction("manual_intervention", { stalled: false, authRequired: false, manualRequired: true, pageRecoveryExhausted: true }).action, "resume");
});

test("login, verification, restrictions and unfinished cleanup each preserve their manual gate", () => {
  for (const [reason, expected] of [["instagram_login_required", /完成 Instagram 登录/],
    ["instagram_challenge", /完成 Instagram 验证/], ["instagram_rate_limited", /操作限制/],
    ["instagram_action_blocked", /操作限制/], ["browser_operations_pending", /等待清理完成/]]) {
    const info = collectionRecoveryDetails({ state: "manual_intervention", recovery_kind: "profile_intervention",
      reason: "instagram_page_recovery_exhausted", original_reason: reason, next_retry_at: "2026-09-15T12:15:00Z" }, "manual_intervention");
    assert.equal(info.manualRequired, true);
    assert.equal(info.automatic, false);
    assert.equal(info.nextRetryAt, null);
    assert.match(info.manualMessage, expected);
  }
  const global = collectionRecoveryDetails({ state: "blocked_by_auth", recovery_kind: "global_auth" }, "blocked_by_auth");
  assert.match(global.manualMessage, /浏览器服务登录已失效/);
});

test("a stale timer cannot promise automatic work after pause, stop, failure, completion or a new healthy target", () => {
  const old = { state: "waiting_network", recovery_kind: "instagram_page", reason: "instagram_profile_not_ready",
    next_retry_at: "2026-09-15T12:15:00Z", candidate_username: "previous", source_discovery_complete: true };
  for (const state of ["paused", "stopped", "failed", "completed", "running"]) {
    const info = collectionRecoveryDetails(old, state);
    assert.equal(info.automatic, false, state);
    assert.equal(info.manualRequired, false, state);
    assert.equal(info.nextRetryAt, null, state);
    assert.equal(info.candidateUsername, "", state);
  }
  assert.equal(collectionRecoveryDetails(undefined, "waiting_network").automatic, false);
  assert.equal(collectionRecoveryDetails({ state: "idle" }, "waiting_network").automatic, false);
  assert.equal(collectionRecoveryDetails({ ...old, next_retry_at: "broken timestamp" }, "waiting_network").nextRetryAt, null);
});

test("a bounded snapshot retains a real manually blocked window until its target arrives", () => {
  for (const state of ["manual_intervention", "blocked_by_auth"])
    assert.deepEqual(collectionTaskRows(task([], [{ profile_id: "w1", state, current_target_id: "outside-page" }]))
      .map(row => row.profileId), ["w1"]);
});

test("an executing recovery attempt is distinct from a retry countdown and does not claim success", () => {
  const profile = { state: "working", current_stage: "recovering_page", recovery_kind: "instagram_page",
    recovery_in_progress: true, progress_confirmed: false, current_target_id: "target",
    last_progress_at: "2026-09-15T12:00:00Z", next_retry_at: "2026-09-15T12:05:00Z",
    reason: "instagram_page_recovery_exhausted", original_reason: "instagram_followers_list_incomplete", candidate_username: "pending" };
  const state = collectionRowStatus("running", profile.state, "running");
  const info = collectionRecoveryDetails(profile, state);
  assert.equal(info.automatic, false);
  assert.equal(info.verifyingRecovery, true);
  assert.equal(info.nextRetryAt, null);
  assert.equal(info.candidateUsername, "pending");
  assert.equal(info.reasonLabel, "粉丝名单尚未读取完成");
  assert.equal(info.manualRequired, false);
  const progress = collectionWindowProgress(profile, { id: "target" }, state, Date.parse("2026-09-15T12:10:00Z"));
  assert.equal(progress.stalled, true);
  assert.equal(progress.lastProgressAt, profile.last_progress_at);
  assert.equal(collectionRecoveryAction(state, { stalled: progress.stalled, authRequired: false, pageRecoveryExhausted: true }).label, "检查点重启");
  for (const stopped of ["paused", "stopped", "failed", "completed"])
    assert.equal(collectionRecoveryDetails(profile, stopped).verifyingRecovery, false, stopped);
});

test("recovery stall detection starts only after three minutes without confirmed progress", () => {
  const profile = { state: "working", current_target_id: "target", recovery_in_progress: true,
    progress_confirmed: false, last_progress_at: "2026-09-15T12:00:00Z" };
  const started = Date.parse(profile.last_progress_at);
  for (const elapsed of [0, 30_000, 179_999, 180_000]) {
    const progress = collectionWindowProgress(profile, { id: "target" }, "running", started + elapsed);
    assert.equal(progress.stalled, false, String(elapsed));
    assert.equal(collectionRecoveryAction("running", { stalled: progress.stalled, authRequired: false,
      pageRecoveryExhausted: false }), null);
  }
  assert.equal(collectionWindowProgress(profile, { id: "target" }, "running", started + 180_001).stalled, true);
});

test("new recovery attempts and task updates cannot hide a stalled target or borrow sibling progress", () => {
  const now = Date.parse("2026-09-15T12:17:00Z");
  const profile = { state: "working", current_target_id: "target", recovery_in_progress: true,
    progress_confirmed: false, last_progress_at: "2026-09-15T12:00:00Z",
    updated_at: "2026-09-15T12:16:59Z", retry_count: 8 };
  assert.equal(collectionWindowProgress(profile, { id: "target" }, "running", now).stalled, true);
  assert.equal(collectionWindowProgress({ ...profile, current_target_id: "sibling",
    last_progress_at: "2026-09-15T12:16:59Z" }, { id: "target", last_success_at: "2026-09-15T12:00:00Z" }, "running", now).stalled, true);
  assert.deepEqual(collectionWindowProgress(profile, { id: "new-target" }, "running", now),
    { lastProgressAt: null, stalled: false });
  for (const state of ["paused", "stopped", "completed", "waiting_network", "manual_required"])
    assert.equal(collectionWindowProgress(profile, { id: "target" }, state, now).stalled, false, state);
});

test("verifying recovery exposes its actual cause without promising a full collection", () => {
  const recovering = { state: "working", recovery_in_progress: true, progress_confirmed: false };
  const cases = [
    [{ reason: "instagram_page_recovery_exhausted", original_reason: "instagram_relationship_list_no_progress" }, "名单暂时没有继续推进"],
    [{ reason: "instagram_profile_not_ready" }, "账号主页尚未加载完成"],
    [{ reason: "other", message: "读取候选详情超时" }, "读取候选详情超时"],
  ];
  for (const [failure, expected] of cases) {
    const info = collectionRecoveryDetails({ ...recovering, ...failure }, "running");
    assert.equal(info.reasonLabel, expected);
    assert.equal(info.automatic, false);
    assert.equal(info.verifyingRecovery, true);
    assert.equal(info.nextRetryAt, null);
    assert.equal(collectionRecoveryDetails({ ...recovering, ...failure }, "paused").reasonLabel, "");
  }
});

test("confirmed new progress removes recovery presentation even while internal retry bookkeeping remains", () => {
  const profile = { state: "working", recovery_kind: "instagram_page", recovery_in_progress: true,
    progress_confirmed: true, current_target_id: "target", last_progress_at: "2026-09-15T12:09:50Z",
    reason: null, message: null, retry_count: 4, candidate_username: "old", next_retry_at: null };
  const info = collectionRecoveryDetails(profile, "running");
  assert.equal(info.automatic, false);
  assert.equal(info.verifyingRecovery, false);
  assert.equal(info.reasonLabel, "");
  assert.equal(info.candidateUsername, "");
  assert.equal(info.nextRetryAt, null);
  assert.equal(collectionWindowProgress(profile, { id: "target" }, "running", Date.parse("2026-09-15T12:10:00Z")).stalled, false);
  assert.equal(profile.retry_count, 4);
});

test("checked candidates and newly saved records remain distinct, including authoritative zero counts", () => {
  assert.equal(collectionModeProgressText("followers", { source_total: 914, discovered: 700, processed: 460, saved: 36, skipped_global_duplicates: 424, qualified_for_review: 9 }),
    "粉丝总数 914 · 已识别 700 · 未识别 214 · 去重 424 · 队列 240 · 丢弃 — · 合格 9");
  assert.equal(collectionModeProgressText("following", { source_total: 0, discovered: 50, processed: 0, saved: 0, skipped_global_duplicates: 0, qualified_for_review: 0 }),
    "关注总数 0 · 已识别 50 · 未识别 0 · 去重 0 · 队列 50 · 丢弃 — · 合格 0 · 数量口径不一致，需核实");
});

test("discovery never substitutes for checked work and missing counts never invent saved results", () => {
  assert.equal(collectionModeProgressText("followers", { source_total: 100, discovered: 20 }), "粉丝总数 100 · 已识别 20 · 未识别 80 · 去重 — · 队列 — · 丢弃 — · 合格 —");
  assert.equal(collectionModeProgressText("post_likers", { processed: 3 }), "post_likers总数 读取中 · 已识别 — · 未识别 — · 去重 — · 队列 — · 丢弃 — · 合格 —");
  assert.equal(collectionModeProgressText("following", {}), "关注总数 读取中 · 已识别 — · 未识别 — · 去重 — · 队列 — · 丢弃 — · 合格 —");
  assert.equal(collectionModeProgressText("following", { processed: null, discovered: 0, saved: null }), "关注总数 读取中 · 已识别 0 · 未识别 — · 去重 — · 队列 — · 丢弃 — · 合格 —");
});


test("dedupe uses the authoritative mode count, never a source or partial-counter difference", () => {
  for (const value of [undefined, null, -1, 1.5, "12", NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.match(collectionModeProgressText("followers", {
      source_total: 241, processed: 200, saved: 10, skipped_global_duplicates: value,
  }), /去重 —/);
  }
  assert.match(collectionModeProgressText("following", {
    source_total: 241, processed: 23, saved: 23, skipped_global_duplicates: 0,
  }), /已识别 — · 未识别 — · 去重 0 · 队列 — · 丢弃 — · 合格 —/);
});

test("qualified count comes only from confirmed manual-review admissions", () => {
  const progress = { source_total: 86, processed: 45, saved: 42, skipped_global_duplicates: 3, qualified_for_review: 12 };
  assert.equal(collectionModeProgressText("followers", progress),
    "粉丝总数 86 · 已识别 — · 未识别 — · 去重 3 · 队列 — · 丢弃 — · 合格 12");
  for (const invalid of [undefined, null, -1, 1.2, "42", Infinity]) {
    assert.match(collectionModeProgressText("followers", { ...progress, qualified_for_review: invalid }), /合格 —$/);
  }
});


test("r94 screenshot gap stays separate from durable duplicates and discarded results", () => {
  const text = collectionModeProgressText("followers", { source_total: 432, discovered: 326,
    processed: 326, saved: 324, skipped_global_duplicates: 2, discarded: 21, hover_discarded: 7 });
  for (const part of ["已识别 326", "去重 2", "丢弃 21", "未识别 106", "队列 0"])
    assert.ok(text.includes(part), part);
});


test("recovery shows the underlying hover or transport cause instead of the generic wrapper", () => {
  for (const [original_reason, expected] of [
    ["instagram_hover_card_unavailable", /悬浮卡/],
    ["worker_not_connected", /读取连接/],
    ["browser_context_missing", /页面会话/],
  ]) {
    const details = collectionRecoveryDetails({state: "working", recovery_in_progress: true,
      reason: "instagram_page_recovery_exhausted", original_reason}, "running");
    assert.equal(details.verifyingRecovery, true);
    assert.match(details.reasonLabel, expected);
    assert.equal(details.automatic, false);
  }
});


test("R6.1 requested screenshot order separates unseen from the pending queue", () => {
  const progress = Object.freeze({ source_total: 456, discovered: 419, processed: 342,
    skipped_global_duplicates: 19, discarded: 249, hover_discarded: 7, qualified_for_review: 74 });
  const items = collectionModeProgressItems("followers", progress);
  assert.deepEqual(items.map(item => item.text), ["粉丝总数 456", "已识别 419", "未识别 37", "去重 19", "队列 77", "丢弃 249", "合格 74"]);
  assert.deepEqual(items.map(item => item.label), ["粉丝总数", "已识别", "未识别", "去重", "队列", "丢弃", "合格"]);
  assert.equal(items.find(item => item.key === "unobserved").value, 37);
  assert.equal(progress.source_total - progress.processed, 114, "old remaining count is not silently relabeled");
  assert.doesNotMatch(collectionModeProgressText("followers", progress), /丢弃 256|剩余待采集/);
});

test("unknown, compact, fractional and unsafe source totals never become a fake zero", () => {
  for (const source_total of [undefined, null, "1.2K", "1.2万", "1,200", -1, 1.2, NaN, Infinity, true, Number.MAX_SAFE_INTEGER + 1]) {
    const items = collectionModeProgressItems("following", {source_total, discovered: 100, processed: 70});
    assert.equal(items[0].value, "读取中");
    assert.equal(items[2].value, "—");
    assert.equal(items[4].value, 30, "known pending queue does not require a source total");
  }
  const zero = collectionModeProgressItems("following", {source_total: 0, discovered: 0, processed: 0});
  assert.deepEqual(zero.slice(0, 3).map(item => item.value), [0, 0, 0]);
});

test("rounded or changing headers preserve identity counts and make no exact-population promise", () => {
  const actual = {discovered: 1196, processed: 1170, skipped_global_duplicates: 23, discarded: 201, qualified_for_review: 945};
  for (const [source_total, unseen, warn] of [[1200, 4, false], [1300, 104, false], [1100, 0, true]]) {
    const items = collectionModeProgressItems("followers", {...actual, source_total});
    assert.deepEqual(items.slice(1, 7).map(item => item.value), [1196, unseen, 23, 26, 201, 945]);
    assert.equal(items.some(item => item.key === "count-warning"), warn);
    assert.match(items[0].title, /平台取整/);
    assert.match(items[2].title, /仅表示显示差额/);
  }
});

test("repeated snapshots and independent outcomes never inflate identified users or force a partition", () => {
  const progress = Object.freeze({source_total: 418, discovered: 395, processed: 365,
    skipped_global_duplicates: 33, discarded: 201, hover_discarded: 7, qualified_for_review: 130});
  const expected = collectionModeProgressText("followers", progress);
  for (let repeat = 0; repeat < 5; repeat++) assert.equal(collectionModeProgressText("followers", progress), expected);
  assert.equal(365 - (33 + 201 + 130), 1, "unclassified record remains unclassified");
  const inconsistent = collectionModeProgressItems("followers", {...progress, processed: 400});
  assert.equal(inconsistent[1].value, 395);
  assert.equal(inconsistent[2].value, 23);
  assert.equal(inconsistent[4].value, "—");
  assert.equal(inconsistent.at(-1).key, "count-warning");
});


test("manual recheck gap uses exactly the same projection as the unseen counter", () => {
  for (const progress of [{source_total:456,discovered:419,processed:342}, {source_total:0,discovered:2},
    {source_total:null,discovered:0}, {source_total:1200,discovered:1196}, {source_total:10,discovered:null}]) {
    assert.equal(collectionModeProgressItems("followers", progress)[2].value, collectionModeUnseenCount(progress) ?? "—");
  }
});

test("R6.2 keeps child capacity and factory timeout distinct from parent-page instability", () => {
  for (const [original_reason, expected] of [
    ["screening_slots_busy", "筛选子页仍在收尾，等待页槽释放"],
    ["parallel_screening_page_create_timeout", "筛选子页创建超时，等待自动重试"],
  ]) {
    const profile = {state: "waiting_network", recovery_kind: "instagram_page",
      reason: "instagram_page_recovery_exhausted", original_reason};
    const details = collectionRecoveryDetails(profile, "waiting_network");
    assert.equal(details.reasonLabel, expected);
    assert.equal(details.automatic, true);
    assert.equal(collectionRecoveryDetails({...profile, state: "paused"}, "paused").automatic, false);
  }
});
