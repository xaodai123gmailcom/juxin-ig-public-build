import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";
import { rollbackAuthenticatedSession } from "../src/auth-session-rollback.ts";
import { collectionRecoveryDetails } from "../src/collection-task-rows.ts";
import {
  MAX_GREETING_MESSAGE_CHARACTERS,
  parseGreetingMessages,
  resolveSuccessfulGreetingMessage,
} from "../src/greeting-messages.ts";

function assertMarkersInOrder(source, markers) {
  let cursor = 0;
  for (const marker of markers) {
    const index = source.indexOf(marker, cursor);
    assert.notEqual(index, -1, `缺少或顺序错误：${marker}`);
    cursor = index + marker.length;
  }
}

test("greeting message parser rejects an empty library", () => {
  assert.deepEqual(parseGreetingMessages("  \r\n\t\n"), {
    ok: false,
    code: "empty",
    error: "请至少输入一条非空打招呼话术。",
  });
});

test("greeting message parser preserves every normalized non-empty line", () => {
  assert.deepEqual(parseGreetingMessages("  Hello  \r\n\r\n  你好 🌍  \nHello"), {
    ok: true,
    messages: ["Hello", "你好 🌍", "Hello"],
  });
});

test("greeting message parser uses Core-compatible Unicode length and blocks the whole library on an overlong line", () => {
  const accepted = "🙂".repeat(MAX_GREETING_MESSAGE_CHARACTERS);
  assert.deepEqual(parseGreetingMessages(accepted), { ok: true, messages: [accepted] });

  const rejected = parseGreetingMessages(`first\n\n${"🙂".repeat(MAX_GREETING_MESSAGE_CHARACTERS + 1)}\nlast`);
  assert.equal(rejected.ok, false);
  assert.equal(rejected.code, "too_long");
  assert.equal(rejected.lineNumber, 3);
  assert.equal(rejected.characterCount, MAX_GREETING_MESSAGE_CHARACTERS + 1);
  assert.match(rejected.error, /第 3 行话术共 201 个字符/);
});

test("automatic and manual greeting UI share fail-closed validation and campaigns retain the full message library", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  assert.match(source, /async function startCampaign\(\)[\s\S]*if \(isPublic && !greetingMessages\.ok\) return;[\s\S]*messages: lines/);
  assert.match(source, /async function manualAction\(candidate: CoreCandidate\)[\s\S]*if \(isPublic && !greetingMessages\.ok\) return;/);
  assert.match(source, /disabled=\{collectionPlatform\(candidate\) !== "instagram" \|\| !selectedWindows\.size \|\| \(isPublic && !greetingMessages\.ok\)/);
});

test("successful greeting history resolves the exact attempt message with safe legacy fallbacks", () => {
  assert.equal(resolveSuccessfulGreetingMessage(
    "greet",
    { message: "attempt field", details: { greeting_message: "immutable chosen text" } },
    "legacy campaign text",
  ), "immutable chosen text");
  assert.equal(resolveSuccessfulGreetingMessage(
    "greet",
    { message: "attempt field", details: {} },
    "legacy campaign text",
  ), "attempt field");
  assert.equal(resolveSuccessfulGreetingMessage(
    "greet",
    { details: {} },
    "legacy campaign text",
  ), "legacy campaign text");
  assert.equal(resolveSuccessfulGreetingMessage(
    "follow",
    { message: "must never appear", details: { greeting_message: "must never appear" } },
    "must never appear",
  ), null);
});

test("greeting history renders actual messages with validated selection payloads", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  assert.match(source, /message: resolveSuccessfulGreetingMessage\([\s\S]*successfulAttempt[\s\S]*campaign\.message/);
  assert.match(source, /message: resolveSuccessfulGreetingMessage\([\s\S]*attempt[\s\S]*campaign\.message/);
  assert.match(source, /operation === "greet" \? <th>实际话术<\/th> : null/);
  assert.match(source, /className="formal-history-message">\{row\.message \|\| "—"\}/);
  assert.match(source, /message: lines\[0\], messages: lines/);
});

test("failed secure persistence revokes Core session and encrypted token", async () => {
  const calls = [];
  await rollbackAuthenticatedSession({
    async logout() { calls.push("logout"); },
    async secureDelete(key) { calls.push(`delete:${key}`); return true; },
  });
  assert.deepEqual(calls, ["logout", "delete:session-token"]);
});

test("secure token deletion is still attempted when logout reports a failure", async () => {
  const calls = [];
  await assert.rejects(
    () => rollbackAuthenticatedSession({
      async logout() { calls.push("logout"); throw new Error("Core offline after local revocation"); },
      async secureDelete(key) { calls.push(`delete:${key}`); return true; },
    }),
    /Core offline/,
  );
  assert.deepEqual(calls, ["logout", "delete:session-token"]);
});

test("an unconfirmed secure deletion is a visible rollback failure", async () => {
  await assert.rejects(
    () => rollbackAuthenticatedSession({
      async logout() {},
      async secureDelete() { return false; },
    }),
    /未确认删除会话令牌/,
  );
});

test("AuthGate rolls back a Core session created before secure persistence", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/auth-gate.tsx"), "utf8");
  assert.match(source, /coreSessionCreated = true;[\s\S]*await rollbackAuthenticatedSession\(client\)/);
  assert.match(source, /setUser\(null\);[\s\S]*setState\("anonymous"\)/);
});

test("review navigation exposes public and private manual queues with operator-managed layers", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  assert.doesNotMatch(source, /公开二审/);
  assert.doesNotMatch(source, /publicSecondary/);
  assert.match(source, /type ReviewView = "public" \| "private";/);
  assert.match(source, />公开审核 <span className="formal-status">\{formatCount\(publicReviewCount\)\}<\/span>/);
  assert.match(source, />私密审核 <span className="formal-status">\{formatCount\(privateReviewCount\)\}<\/span>/);
  assert.doesNotMatch(source, /私密二审|privateSecondary|private-secondary/);
  assert.match(source, /visibility: reviewView, review_stage: reviewStage/);
  assert.match(source, /read: \(query\) => client\.reviewQueue\(query\)/);
  assert.match(source, /const candidates = page\?\.items \?\? \[\];/);
  assert.match(source, /moveReviewStage/);
});

test("every remaining review list supports isolated resilient batch selection", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  assert.match(source, /setSelectedCandidateIds\(new Set\(\)\);[\s\S]*setReviewView/);
  assert.match(source, /setSelectedCandidateIds\(\(current\) => retainLiveSelection\(current, liveIds\)\)/);
  assert.match(source, /const candidateIds = selectedReviewIds\(candidates, selectedCandidateIds\)/);
  assert.match(source, /failedIds\.delete\(target\.id\)/);
  assert.match(source, /candidateIdsRef\.current\.has\(id\)/);
  assert.match(source, /if \(reviewMutationRef\.current\) return;/);
  assert.match(source, /setReviewMutationBusy\(true\)[\s\S]*setReviewMutationBusy\(false\)/);
  assert.match(source, /catch \(reason\) \{[\s\S]*await refreshRef\.current\?\.\(\)/);
  assert.match(source, /失败账号已保留勾选/);
  assert.match(source, /quick-review-select[\s\S]*type="checkbox"/);
  assert.match(source, /批量不合格（\$\{selectedCount\}）/);
  assert.match(source, /批量合格（\$\{selectedCount\}）/);
});

test("private review rows render the collected post count", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  assert.match(
    source,
    /queue === "private"[\s\S]*itemFailures\.posts[\s\S]*firstNumber\(itemProfile, \["posts", "posts_count", "media_count"\]\)/,
  );
  assert.doesNotMatch(
    source,
    /itemSuccesses\.following[\s\S]{0,260}<strong>—<\/strong><span>\{formatTime\(item\.created_at\)\}<\/span>/,
  );
});

test("private review keeps collected facts before the target account and decisions", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  const css = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.css"), "utf8");
  const header = source.match(
    /queue === "private"\s*\?\s*<div className="quick-review-table-head">([\s\S]*?)<\/div>\s*:\s*<div className="quick-review-table-head">/,
  );
  assert.ok(header, "未找到私密审核表头");
  const labels = [...header[1].matchAll(/<span(?:\s+className="[^"]+")?>([^<]+)<\/span>/g)].map((match) => match[1]);
  assert.deepEqual(labels, ["粉丝", "关注", "帖子", "采集时间", "头像与账号", "选择", "是否合格"]);

  const rows = source.match(
    /\{queue === "private"\s*\?\s*<>([\s\S]*?)<\/>\s*:\s*<>([\s\S]*?)<\/>\}\s*(<label className="formal-check quick-review-select"[\s\S]*?<\/label>)\s*(<div className="formal-row-actions quick-review-actions">)/,
  );
  assert.ok(rows, "未找到私密审核行");
  assertMarkersInOrder(rows[1], [
    'firstNumber(itemProfile, ["followers", "followers_count", "follower_count"])',
    'firstNumber(itemProfile, ["following", "following_count"])',
    'firstNumber(itemProfile, ["posts", "posts_count", "media_count"])',
    "{formatTime(item.created_at)}",
    'className="quick-review-account formal-profile-identity-link"',
  ]);
  assert.match(rows[3], /quick-review-select[\s\S]*type="checkbox"/);
  assert.match(
    css,
    /\.private-review-list \.quick-review-table-head, \.private-review-list \.quick-review-row \{ grid-template-columns: repeat\(3, minmax\(90px, \.6fr\)\) minmax\(150px, \.85fr\) minmax\(310px, 1\.7fr\) 52px minmax\(210px, 1fr\); \}/,
  );
});

test("public review is an avatar-first pending list without post photos", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  const css = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.css"), "utf8");

  assert.doesNotMatch(source, /title="公开账号审核"/);
  assert.doesNotMatch(source, /candidateRecentPosts|最近 6 个帖子|formal-post-grid|preview_data_url/);
  assert.match(source, /public-review-list/);
  assert.match(source, /open=\{reviewListOpen\}/);
  assert.match(source, /setReviewView\(next\);\s*setReviewListOpen\(true\);/);

  const header = source.match(
    /queue === "private"[\s\S]*?:\s*<div className="quick-review-table-head">([\s\S]*?)<\/div>\}/,
  );
  assert.ok(header, "未找到公开审核表头");
  const labels = [...header[1].matchAll(/<span(?:\s+className="[^"]+")?>([^<]+)<\/span>/g)].map((match) => match[1]);
  assert.deepEqual(labels, ["采集时间", "所在地", "帖子数", "粉丝数", "关注数量", "活跃度", "头像", "账号", "多选", "功能按键"]);

  const rows = source.match(
    /\{queue === "private"\s*\?\s*<>([\s\S]*?)<\/>\s*:\s*<>([\s\S]*?)<\/>\}\s*(<label className="formal-check quick-review-select"[\s\S]*?<\/label>)\s*(<div className="formal-row-actions quick-review-actions">)/,
  );
  assert.ok(rows, "未找到公开审核行");
  assertMarkersInOrder(rows[2], [
    "{formatTime(item.created_at)}",
    '{itemLocation === "—" ? "未知" : itemLocation}',
    'firstNumber(itemProfile, ["posts", "posts_count", "media_count"])',
    'firstNumber(itemProfile, ["followers", "followers_count", "follower_count"])',
    'firstNumber(itemProfile, ["following", "following_count"])',
    "{reviewActivityLabel(itemProfile)}",
    'className="quick-review-avatar-link"',
    'className="quick-review-account formal-profile-identity-link"',
  ]);
  assert.match(rows[2], /itemLocation === "—" \|\| itemFailures\.location/);
  assert.match(rows[3], /quick-review-select[\s\S]*type="checkbox"/);
  assert.match(
    css,
    /\.private-review-list \.formal-avatar\.small, \.public-review-list \.formal-avatar\.small \{ width: 87px; height: 87px;/,
  );
});

test("review links retain Instagram preview and never offer another platform", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  const reviewStart = source.indexOf("function ReviewWorkspace");
  const reviewEnd = source.indexOf("function CandidateRow", reviewStart);
  assert.notEqual(reviewStart, -1, "未找到审核工作区");
  assert.notEqual(reviewEnd, -1, "未找到审核工作区结束位置");
  const reviewSource = source.slice(reviewStart, reviewEnd);
  assert.equal(reviewSource.match(/title=\{\("在软件内预览 Instagram 主页"\)\}/g)?.length, 3);
  assert.equal(reviewSource.match(/target="_blank" rel="noreferrer"/g)?.length, 3);
  assert.equal(reviewSource.match(/href=\{collectionProfileUrl\(item\)\}/g)?.length, 3);
});

test("collection progress does not repeat a long BitBrowser window name", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  const start = source.indexOf("function CollectionTaskRow");
  const end = source.indexOf("type WorkspaceProps =", start);
  assert.notEqual(start, -1, "未找到采集任务行");
  assert.notEqual(end, -1, "未找到采集任务行结束位置");
  const rowSource = source.slice(start, end);
  assert.equal(rowSource.match(/windowNames\.get\(profileId\) \|\| "未知窗口"/g)?.length, 1);
  assert.doesNotMatch(rowSource, /<span><Monitor size=\{13\} \/>/);
  assert.match(rowSource, /<CollectionProgressValues modes=\{modes\} progress=\{progress\} coverage=\{asRecord\(target\?\.mode_coverage\)\} \/>/);
  const progressStart = source.indexOf("function CollectionProgressValues");
  assert.notEqual(progressStart, -1, "未找到共享采集进度组件");
  assert.match(source.slice(progressStart, start), /collectionModeProgressText\(displayMode, asRecord\(progress\[mode\]\)\)/);
});

test("collection and history copy describe strict public filtering", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  assert.doesNotMatch(source, /三级顺序审核|美国未达标或地区未知进二审/);
  assert.doesNotMatch(source, /先分类，再分支筛选/);
  assert.match(source, /const itemLocation = reviewLocation\(itemProfile, itemScreening\)/);
  assert.match(source, /snapshot\.history\.collection_exclusions/);
  assert.match(source, /excludeVerified: latest \? settings\.exclude_verified === true : true/);
  assert.match(source, />排除蓝V<\/label>/);
  assert.match(source, /exclude_public_zero_posts: excludePublicZeroPosts/);
  assert.match(source, />排除0帖（公开\/私密）<\/label>/);
  assert.match(source, /exclude_male_avatar: false/);
  assert.match(source, /local_person_recognition: false/);
  assert.doesNotMatch(source, /checked=\{excludeMaleAvatar\}|checked=\{localPersonRecognition\}|排除男性≥55%/);
});

test("bounded reads stay unchanged while preview cleanup remains only in settings", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  assert.match(source, /const SNAPSHOT_PAGE_LIMIT = 2_000;/);
  assert.doesNotMatch(source, /部分列表显示最近|前往设置|truncatedScopes\.length/);
  assert.match(source, /limit: SNAPSHOT_PAGE_LIMIT,[\s\S]*historyLimit: SNAPSHOT_PAGE_LIMIT/);
  assert.match(source, /SQLite 业务记录不设应用额度/);
  assert.match(source, /待审核预览、账号、任务、检查点、审核历史、成功记录及全局去重均已保留/);
  assert.match(source, /typeof storage\.database_bytes === "number"[\s\S]*typeof storage\.wal_bytes === "number"[\s\S]*storage\.database_bytes[\s\S]*storage\.wal_bytes/);
  assert.match(source, /label="SQLite \+ WAL 占用（字节）" value=\{sqliteStorageBytes\}/);
  assert.match(source, /<span>WAL 日志<\/span><strong>\{formatBytes\(storage\.wal_bytes\)\}<\/strong>/);
  assert.match(source, /const retainedHistoryTotal = \["approved", "action_successes", "manual_rejections", "collection_exclusions"\]/);
  assert.doesNotMatch(source, /const retainedHistoryTotal = \["approved", "approved_dismissed"/);
  assert.doesNotMatch(source, /页面显示批次|可分页|下一批/);
  assert.doesNotMatch(source, /释放 \$\{formatBytes\(clearedBytes\)\}/);
  assert.match(source, /移除 \$\{formatBytes\(clearedBytes\)\} 预览数据/);
  assert.match(source, /data-nav="settings" href="#\/settings"/);

  assert.match(source, /function storageCleanupNotice\(result: unknown\): string/);
  const cleanupNoticeStart = source.indexOf("function storageCleanupNotice");
  const cleanupNoticeEnd = source.indexOf("\nfunction candidateName", cleanupNoticeStart);
  assert.notEqual(cleanupNoticeStart, -1, "缺少安全清理结果说明");
  assert.notEqual(cleanupNoticeEnd, -1, "无法限定安全清理结果说明");
  const cleanupNotice = source.slice(cleanupNoticeStart, cleanupNoticeEnd);
  for (const marker of [
    "待审核预览",
    "账号",
    "任务",
    "检查点",
    "审核历史",
    "成功记录",
    "全局去重",
    "Core 总数",
    "当前界面",
    "不设应用额度",
    "本机可用磁盘空间",
  ]) {
    assert.match(cleanupNotice, new RegExp(marker), `清理反馈必须明确保留：${marker}`);
  }

  assert.match(source, /const STORAGE_CACHE_CLEAR_BUSY_KEY = "storage-cache-clear";/);
  assert.ok(
    (source.match(/STORAGE_CACHE_CLEAR_BUSY_KEY/g) || []).length === 3,
    "设置页保留缓存清理 busy key",
  );
  assert.match(source, /busyKeysRef\.current\.has\(key\)[\s\S]*busyKeysRef\.current\.add\(key\)/);
  assert.match(source, /busyKeysRef\.current\.delete\(key\)/);
  assert.ok(
    (source.match(/\(client\) => client\.clearStorageCache\(\)/g) || []).length === 1,
    "设置页保留正式 Core 安全清理命令",
  );
  assert.ok(
    (source.match(/仅清理预览缓存/g) || []).length === 1,
    "设置页明确按钮只清理预览缓存",
  );
});

test("failure list offers one-click delete and return for ordinary failures only", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  assert.match(source, /const ordinaryFailures = failures\.filter\(\(failure\) => !failure\.isUnknown\);/);
  assert.match(source, /一键删除并退回/);
  assert.match(source, /结果未知项不会被处理/);
  assert.match(source, /ordinaryFailures\.map\(\(\{ campaign, targetId \}\) => \(\{ campaignId: campaign\.id, targetId \}\)\)/);
  assert.match(source, /for \(const item of items\)[\s\S]*client\.dismissActionFailure\(item\.campaignId, item\.targetId\)/);
  assert.match(source, /disabled=\{!ordinaryFailures\.length \|\| disabled\(bulkKey\)\}/);
});

test("collection and action workspaces use clean aligned columns with internal scrolling", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  const css = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.css"), "utf8");
  const actionStart = source.indexOf("function ActionWorkspace");
  const actionEnd = source.indexOf("\ntype SuccessfulActionRow", actionStart);
  assert.notEqual(actionStart, -1, "缺少公开/私密执行页面");
  assert.notEqual(actionEnd, -1, "无法限定执行页面范围");
  const actionSource = source.slice(actionStart, actionEnd);
  assertMarkersInOrder(actionSource, [
    'className="action-approved-panel"',
    'className="action-window-column"',
    'className="action-runtime-column"',
  ]);
  const runtimeStart = actionSource.indexOf('className="action-runtime-column"');
  const bottomListStart = actionSource.indexOf('className="action-bottom-list"', runtimeStart);
  assert.notEqual(bottomListStart, -1, "失败列表必须移动到主三栏下方");
  const runtimeSource = actionSource.slice(runtimeStart, bottomListStart);
  assertMarkersInOrder(runtimeSource, [
    'title="执行设置"',
    'title="正在执行列表"',
  ]);
  assert.doesNotMatch(runtimeSource, /<FailureList/);
  assert.match(actionSource.slice(bottomListStart), /<FailureList/);
  assert.doesNotMatch(actionSource, /style=\{\{ display: "grid", gap: 14, alignContent: "start" \}\}/);

  assert.match(css, /\.formal-action-grid \{ height: clamp\(680px, calc\(100vh - 240px\), 880px\);[^}]*align-items: stretch;/);
  assert.match(css, /\.action-window-column > \.formal-panel > \.formal-panel-body:last-child \{ flex: 1; min-height: 0; max-height: none; \}/);
  assert.match(css, /\.action-runtime-column \{ display: grid; grid-template-rows: auto minmax\(0, 1fr\); gap: 14px; \}/);
  assert.match(css, /\.action-runtime-section \.formal-scroll\.short \{ flex: 1; min-height: 0; max-height: none; \}/);
  assert.match(css, /\.action-bottom-list \.formal-scroll\.short \{ min-height: 260px; max-height: 440px; \}/);

  assert.match(css, /\.collection-workbench-grid \{ height: clamp\(610px, calc\(100vh - 250px\), 760px\); min-height: 0;[^}]*align-items: stretch;/);
  assert.match(css, /\.collection-column, \.collection-middle-column, \.collection-execution-column \{ min-width: 0; min-height: 0; overflow: hidden; \}/);
  assert.match(css, /\.collection-column-scroll \{ flex: 1 1 0; min-height: 0; max-height: none; overflow-y: auto;/);
  assert.match(css, /\.collection-middle-column > \.formal-panel:first-child > \.formal-panel-body:last-child, \.collection-execution-column > \.formal-panel > \.formal-panel-body:last-child \{ flex: 1 1 0; min-height: 0; max-height: none; overflow-y: auto; \}/);
  assert.match(css, /@media \(max-width: 1320px\) \{[\s\S]*\.collection-workbench-grid \{ height: auto; grid-template-columns: 1fr 1fr; \}/);
});

test("review rows reflow before actions can cover multi-select controls", () => {
  const css = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.css"), "utf8");
  assert.match(css, /\.quick-review-panel \{[^}]*container-name: quick-review; container-type: inline-size;/);
  assert.match(css, /\.quick-review-row > \* \{ min-width: 0; \}/);
  assert.match(css, /\.quick-review-actions \{ width: 100%; min-width: 0; flex-wrap: wrap; \}/);
  assert.doesNotMatch(css, /\.quick-review-actions \{[^}]*flex-wrap: nowrap;/);
  assert.match(css, /@container quick-review \(max-width: 1280px\)/);
  assert.match(css, /@container quick-review \(max-width: 900px\)[\s\S]*\.quick-review-table-head \{ display: none; \}/);
  assert.match(css, /\.public-review-list \.quick-review-table-head > :last-child,[\s\S]*\.private-review-list \.quick-review-actions \{[\s\S]*grid-column: 1 \/ -1;/);
});

test("collection failure cards use the shared authoritative per-mode progress formatter", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  const client = readFileSync(resolve(import.meta.dirname, "../src/core-client.ts"), "utf8");
  assert.match(source, /const sourcesById = new Map\(snapshot\.sources\.map\(\(source\) => \[source\.id, source\]\)\);/);
  assert.match(source, /sourcesById\.get\(String\(candidate\.source_target_id \|\| ""\)\)/);
  assert.match(source, /<CollectionProgressValues modes=\{progressModes\} progress=\{modeProgress\} coverage=\{asRecord\(progressSource\?\.mode_coverage\)\} unavailableText="原任务进度记录不可用" \/>/);
  assert.match(source, /collectionModeProgressText\(displayMode, asRecord\(progress\[mode\]\)\)/);
  assert.match(client, /source_target_id\?: string \| null;/);
  assert.doesNotMatch(source, /原任务进度记录不可用[\s\S]{0,120}已采集 0/);
});

test("split targets support durable compact multi-window assignment", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  const client = readFileSync(resolve(import.meta.dirname, "../src/core-client.ts"), "utf8");
  const css = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.css"), "utf8");
  assert.match(source, /指定窗口后加入/);
  assert.match(source, /collection-assignment-trigger/);
  assert.match(source, /client\.assignWaitingSplitWindows\(editor\.candidateId as string, windowIds\)/);
  assert.match(source, /allowedWindowIds\.length \? `指定 \$\{allowedWindowIds\.length\} 个窗口/);
  assert.doesNotMatch(source, /allowedWindowIds\.map\([^\n]*window\.name/);
  assert.match(client, /split_waiting_assign_windows/);
  assert.match(client, /allowed_window_ids: allowedWindowIds/);
  assert.match(css, /\.collection-assignment-trigger \{ max-width: 92px; \}/);
  assert.match(css, /\.collection-window-option-name \{ white-space: normal; overflow-wrap: anywhere;/);
});

test("history summary uses complete Core totals and labels the bounded visible rows", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  const start = source.indexOf("function HistoryWorkspace");
  const end = source.indexOf("\nfunction ImmutableOperationalHistory", start);
  assert.notEqual(start, -1, "缺少历史页面");
  assert.notEqual(end, -1, "无法限定历史页面");
  const historySource = source.slice(start, end);

  assert.match(historySource, /const rejectedTotal = snapshot\.counts\.rejected \?\? rejections\.length;/);
  assert.match(historySource, /const excludedTotal = snapshot\.counts\.collection_excluded \?\? exclusions\.length;/);
  assert.match(historySource, /const retainedHistory = retainedHistoryOf\(snapshot\.storage\);/);
  assert.match(historySource, /const approvedTotal = retainedHistory\.approved \?\? approvals\.length;/);
  assert.doesNotMatch(historySource, /const approvedTotal = snapshot\.counts\.approved/);
  assert.match(historySource, /<HistoryTotals \/>/);
  const reportSource = readFileSync(resolve(import.meta.dirname, "../src/reports-workspace.tsx"), "utf8");
  for (const label of ["总采集数", "总去重数", "今日采集数", "合格数"]) assert.ok(reportSource.includes(label));
  assert.match(reportSource, /workReport<Totals>\("history",b.start,b.end\)/);
  assert.doesNotMatch(historySource, /label="人工审核不合格" value=\{rejections\.length\}/);
  assert.doesNotMatch(historySource, /label="采集阶段已排除" value=\{exclusions\.length\}/);
  assert.doesNotMatch(historySource, /label="审核合格账号" value=\{approvals\.length\}/);

  for (const [visible, total] of [
    ["rejections", "rejectedTotal"],
    ["exclusions", "excludedTotal"],
    ["approvals", "approvedTotal"],
  ]) {
    assert.match(
      historySource,
      new RegExp(`显示 \\{formatCount\\(${visible}\\.length\\)\\} / 总数 \\{formatCount\\(${total}\\)\\}`),
      `${visible} 列表必须区分当前显示数量与 Core 完整总数`,
    );
  }
});

test("collection exclusion history only appends a real location", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  const start = source.indexOf("function HistoryWorkspace");
  const end = source.indexOf("\nfunction ImmutableOperationalHistory", start);
  assert.notEqual(start, -1, "缺少历史页面");
  assert.notEqual(end, -1, "无法限定历史页面");
  const historySource = source.slice(start, end);

  assert.match(source, /function collectionExclusionReason\(record: CoreCollectionExclusion\)/);
  assert.match(historySource, /\{collectionExclusionReason\(record\)\}/);
  assert.doesNotMatch(historySource, /所在地未记录/);
});

test("collection exclusion history translates precise rejection reasons", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  const start = source.indexOf("function collectionExclusionReason");
  const end = source.indexOf("\nfunction reviewLocation", start);
  assert.notEqual(start, -1, "缺少采集排除原因格式化函数");
  assert.notEqual(end, -1, "无法限定采集排除原因格式化函数");
  const formatter = source.slice(start, end);
  for (const [code, label] of [
    ["followers_above_max", "粉丝数过高"],
    ["followers_below_min", "粉丝数不足"],
    ["following_above_max", "关注数过高"],
    ["following_below_min", "关注数不足"],
    ["posts_above_max", "帖子数过高"],
    ["posts_below_min", "帖子数不足"],
    ["activity_days_above_max", "活跃度超过限制"],
    ["professional_account_excluded", "公开专业或商业账号"],
    ["external_bio_link_excluded", "简介包含可点击站外链接"],
    ["verified_account_excluded", "公开蓝 V 认证账号"],
  ]) {
    assert.match(formatter, new RegExp(`${code}[\\s\\S]*${label}`));
  }
  assert.match(formatter, /所在地不符合：\$\{locationCountry\}（要求美国）/);
  assert.match(formatter, /所在地未显示（旧规则曾排除）/);
  assert.match(formatter, /const fallback = fallbackByCode\[raw\] \|\| fallbackByCode\[record\.reason_code\]/);
  assert.match(formatter, /!raw \|\| raw === record\.reason_code \|\| fallbackByCode\[raw\]/);
  assert.doesNotMatch(formatter, /return raw;$/m);
});

test("review and action headline counts use complete Core totals instead of bounded arrays", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  assert.match(source, /const publicReviewCount = counts \? counts\.public\.stage1 \+ counts\.public\.stage2 : snapshot\?\.counts\.pending_public/);
  assert.match(source, /const privateReviewCount = counts \? counts\.private\.stage1 \+ counts\.private\.stage2 : snapshot\?\.counts\.pending_private/);
  assert.match(source, /const counts = reviewPage\?\.data\.counts;/);
  assert.match(source, /const pendingReviewTotal = publicReviewCount !== undefined && privateReviewCount !== undefined \? publicReviewCount \+ privateReviewCount : undefined;/);
  assert.match(source, /const currentReviewTotal = page\?\.total \?\? 0;/);
  assert.match(source, /label="待审核总量" value=\{pendingReviewTotal\}/);
  assert.match(source, /本页 \{formatCount\(quickCandidates\.length\)\} \/ 共 \{page \? formatCount\(currentReviewTotal\) : "—"\} 个/);
  assert.match(source, /const approvedTotal = snapshot\.counts\[isPublic \? "approved_public" : "approved_private"\] \?\? approved\.length;/);
  assert.match(source, /label=\{isPublic \? "公开合格账号" : "私密合格账号"\} value=\{approvedTotal\}/);
  assert.match(source, /显示 \{formatCount\(approved\.length\)\} \/ 总数 \{formatCount\(approvedTotal\)\}/);
  assert.doesNotMatch(source, /label=\{isPublic \? "公开合格账号" : "私密合格账号"\} value=\{approved\.length\}/);
});

test("unified discard limits can all be disabled without hidden qualification controls", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  const collection = source.slice(source.indexOf("function CollectionWorkspace("), source.indexOf("function CollectionTaskList("));
  assert.match(collection, /function setAllDiscardLimitsUnlimited\(\)/);
  assert.match(collection, /DISCARD_LIMIT_KEYS\.map\(key => \[key, "0"\]\)/);
  assert.match(collection, /onClick=\{setAllDiscardLimitsUnlimited\}/);
  assert.match(collection, /source_limits: \{\}/);
  assert.match(collection, /公开帖子活跃度上限（天）/);
  assert.doesNotMatch(collection, /setFollowersMax|setFollowingMax|setPostsMax|setActivityDays|mode_limits/);
});

test("collection offers 1-1, 1-2 and 1-3 screening choices", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  assert.match(source, /单窗口并行档位/);
  assert.match(source, /\(\[1, 2, 3\] as ParallelScreeningWorkers\[\]\)/);
  assert.match(source, /parallel_screening_workers: parallelScreeningWorkers/);
  assert.match(source, /单窗口并行档位：1-\{firstNumber/);
  assert.match(source, /R59 分批读取 · 子页并行采集/);
  assert.doesNotMatch(source, /待领取上限 1|待领取最多 1 个/);
});

test("snapshot truncation labels are page-aware and use authoritative has_more keys", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  const start = source.indexOf("function truncatedSnapshotScopes");
  const end = source.indexOf("\nfunction storageCleanupNotice", start);
  assert.notEqual(start, -1, "缺少按页面解析截断范围的函数");
  assert.notEqual(end, -1, "无法限定截断范围函数");
  const scopeSource = source.slice(start, end);

  assert.match(scopeSource, /if \(mode === "settings"\) return \[\];/);
  assert.match(scopeSource, /const hasMore = snapshot\.has_more;/);
  assert.match(scopeSource, /if \(!hasMore\)[\s\S]*legacyTruncated[\s\S]*\["当前页面"\]/);
  assert.match(scopeSource, /fields\[mode\][\s\S]*hasMore\[key\] === true/);
  const expectedByMode = {
    collection: [
      ["split_candidates", "分裂号队列"],
      ["tasks", "采集任务"],
      ["sources", "采集来源"],
    ],
    review: [
      ["pending_public_accounts", "公开待审核"],
      ["pending_private_accounts", "私密待审核"],
    ],
    public: [
      ["approved_public_accounts", "公开合格账号"],
      ["greet_campaigns", "自动打招呼任务"],
      ["greet_action_success_history", "自动打招呼成功历史"],
    ],
    private: [
      ["approved_private_accounts", "私密合格账号"],
      ["follow_campaigns", "自动点关注任务"],
      ["follow_action_success_history", "自动点关注成功历史"],
    ],
    history: [
      ["approval_history", "审核合格历史"],
      ["manual_rejection_history", "人工审核不合格历史"],
      ["collection_exclusion_history", "采集阶段排除历史"],
      ["action_success_history", "自动执行成功历史"],
      ["tasks", "任务历史"],
      ["actions", "自动执行记录"],
    ],
  };
  const modes = Object.keys(expectedByMode);
  modes.forEach((mode, index) => {
    const modeStart = scopeSource.indexOf(`${mode}: [`);
    const nextStart = index + 1 < modes.length
      ? scopeSource.indexOf(`${modes[index + 1]}: [`, modeStart)
      : scopeSource.indexOf("  };", modeStart);
    assert.notEqual(modeStart, -1, `缺少 ${mode} 页面范围映射`);
    assert.notEqual(nextStart, -1, `无法限定 ${mode} 页面范围映射`);
    const modeSource = scopeSource.slice(modeStart, nextStart);
    for (const [key, label] of expectedByMode[mode]) {
      assert.match(
        modeSource,
        new RegExp(`\\["${key}", "${label}"\\]`),
        `${mode} 页面必须把 ${key} 标为 ${label}`,
      );
    }
  });
});

test("snapshot failure UI is fail-closed while accurately describing automatic recovery", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  assert.match(source, /本次快照更新失败，正在自动重试/);
  assert.match(source, /总览数据暂未更新；独立读取正常的审核名单仍可操作，已有任务可暂停或停止/);
  assert.match(source, /<RefreshCw size=\{15\} \/>立即重试/);
  assert.doesNotMatch(source, /快照已停止更新/);
  assert.match(
    source,
    /refresh: useCallback\(\(\) => \{[\s\S]*if \(refreshInFlightRef\.current\) return refreshInFlightRef\.current;[\s\S]*const readSnapshot = refreshRef\.current;[\s\S]*Promise\.resolve\(\)\.then\(readSnapshot\)[\s\S]*\.catch\(\(\) => \{[\s\S]*\.finally\(\(\) => \{[\s\S]*refreshInFlightRef\.current = null;[\s\S]*\}, \[\]\)/,
  );
  assert.match(source, /disabled: useCallback\(\(key\?: string\) => Boolean\(error\)/);
});

test("collection window controls serialize stop and delete while keeping one-click delete available", () => {
  const source = readFileSync(resolve(import.meta.dirname, "../src/formal-workbench.tsx"), "utf8");
  const start = source.indexOf("function CollectionTaskRow");
  const end = source.indexOf("\ntype WorkspaceProps", start);
  assert.notEqual(start, -1, "缺少采集任务行组件");
  assert.notEqual(end, -1, "无法限定采集任务行组件");
  const row = source.slice(start, end);

  assert.match(
    row,
    /const rowActionKey = `collection-window-\$\{task\.id\}-\$\{profileId \|\| profileIdOverride \|\| targetId \|\| "empty"\}`;/,
    "同一窗口的全部操作必须共享稳定互斥键",
  );
  assert.match(row, /const controlWindow = \(action:[\s\S]*return run\(rowActionKey,/);
  assert.match(row, /return collectionControls\.run\(rowActionKey,/);
  assert.match(row, /await run\(rowActionKey, \(client\) => client\.controlCollectionWindow\(task\.id, profileId, action, targetId \|\| undefined\)/);
  assert.doesNotMatch(row, /run\(`\$\{key\}-\$\{suffix\}`/);
  assert.doesNotMatch(row, /disabled=\{disabled\(`\$\{key\}-/);

  const stopExpression = row.match(/const canStopWindow = ([^;]+);/)?.[1];
  const activeDefinition = source.match(/const ACTIVE_STATES = [^;]+;/)?.[0];
  const pausedDefinition = source.match(/const PAUSED_STATES = [^;]+;/)?.[0];
  assert.ok(stopExpression && activeDefinition && pausedDefinition, "缺少窗口停止条件");
  // Exercise the production decision instead of fixing its spelling to the old
  // network-only expression. Manual waiters still own a cancellable worker.
  const canStop = new Function("profileId", "state", "recovery",
    `${activeDefinition}\n${pausedDefinition}\nreturn ${stopExpression};`);
  for (const state of ["recoverable", "stopped", "completed", "failed", "idle"])
    assert.equal(canStop("w1", state, collectionRecoveryDetails(undefined, state)), false,
      `${state} 已结束的工作器不能继续显示停止按钮`);
  for (const state of ["running", "paused", "waiting_network", "manual_intervention", "auth_required", "blocked_by_auth"]) {
    const recovery = collectionRecoveryDetails({ state }, state);
    assert.equal(canStop("w1", state, recovery), true, `${state} 应保留停止入口`);
    assert.equal(canStop("", state, recovery), false, "没有窗口不能发送窗口停止命令");
  }
  assert.match(
    row,
    /\{profileId \? <button className="formal-button compact danger collection-return-button" disabled=\{disabled\(rowActionKey\)\}/,
    "有执行窗口时删除按钮不得依赖先暂停或先停止",
  );
});

function startControlledResumeEffect(client) {
  const source = readFileSync(resolve(import.meta.dirname, "../src/auth-gate.tsx"), "utf8");
  const begin = source.indexOf("useEffect(() => {");
  const end = source.indexOf("}, [retry]);", begin);
  assert.ok(begin >= 0 && end > begin);
  // Execute the production effect, erasing only its two local type annotations.
  const effectSource = source.slice(begin, end + "}, [retry]);".length)
    .replace("let client: CollectorCoreClient;", "let client;")
    .replace("let rollbackFailure: unknown;", "let rollbackFailure;");
  let cleanup;
  const execute = new Function("useEffect", "getCollectorCoreClient", "rollbackAuthenticatedSession", `
    const retry = 0, clientRef = { current: null };
    const setState = () => {}, setError = () => {}, setUser = () => {};
    const validateAuthResponse = value => value;
    ${effectSource}
  `);
  execute(effect => { cleanup = effect(); }, () => client, rollbackAuthenticatedSession);
  return () => cleanup();
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const nextEffectTurn = () => new Promise(resolve => setImmediate(resolve));

test("an obsolete resume failure cannot delete the newer stored session", async () => {
  const resume = deferred();
  const mutations = [];
  const stop = startControlledResumeEffect({
    secureGet: async () => "old-token",
    request: () => resume.promise,
    secureDelete: async key => { mutations.push(["delete", key]); return true; },
    logout: async () => { mutations.push(["logout"]); },
  });
  await nextEffectTurn();
  stop();
  resume.reject(new Error("expired old session"));
  await nextEffectTurn();
  assert.deepEqual(mutations, []);
});

test("an obsolete successful resume cannot overwrite a newer stored session", async () => {
  const resume = deferred();
  const mutations = [];
  const stop = startControlledResumeEffect({
    secureGet: async () => "old-token",
    request: () => resume.promise,
    secureSet: async (...args) => { mutations.push(["set", ...args]); return true; },
    secureDelete: async () => true,
    logout: async () => {},
  });
  await nextEffectTurn();
  stop();
  resume.resolve({ session_token: "old-response-token", user: { id: "old-owner" } });
  await nextEffectTurn();
  assert.deepEqual(mutations, []);
});

test("a disposed initial credential read cannot launch a stale resume request", async () => {
  const stored = deferred();
  let requests = 0;
  const stop = startControlledResumeEffect({
    secureGet: () => stored.promise,
    request: async () => { requests++; return { session_token: "old", user: {} }; },
    secureSet: async () => true,
  });
  stop();
  stored.resolve("old-token");
  await nextEffectTurn();
  assert.equal(requests, 0);
});

test("an active failed resume still removes the expired persisted token", async () => {
  const deleted = [];
  const stop = startControlledResumeEffect({
    secureGet: async () => "expired",
    request: async () => { throw new Error("expired"); },
    secureDelete: async key => { deleted.push(key); return true; },
  });
  await nextEffectTurn();
  stop();
  assert.deepEqual(deleted, ["session-token"]);
});
