import {StorageStatus} from './storage-status';
import { collectionCoverageDisplay, type CollectionCoverage } from "./split-review-report";
import {ReviewAccountSettings} from './review-account-settings';
import {ChatGPTButton} from './chatgpt-button';
import {GoogleTranslatorButton} from './google-translator-button';
import {StorageManagementSettings} from './storage-management-settings';
import {MessageNotificationSettings} from './message-notification-settings';
import {AccountUnreadProvider,UnreadBadge} from './account-unread';
import {
  Home,
  ClipboardList,
  Leaf,
  BarChart3,
  AlertTriangle,
  Archive,
  ArrowDown,
  ArrowUp,
  Camera,
  Check,
  ChevronDown,
  CirclePause,
  CirclePlay,
  Clock3,
  Database,
  Download,
  ExternalLink,
  Eye,
  FileClock,
  History,
  Inbox,
  ListFilter,
  LoaderCircle,
  LockKeyhole,
  LockKeyholeOpen,
  MessageCircle,
  Monitor,
  Pause,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Send,
  Settings,
  ShieldCheck,
  Square,
  Trash2,
  UserCheck,
  UserPlus,
  UserRoundCheck,
  Users,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import {
  type CoreActionCampaign,
  type CoreActionOperation,
  type CoreApprovalHistoryCandidate,
  type CoreBitBrowserWindow,
  type CoreCandidate,
  type CoreReviewQueuePage,
  type CoreReviewStage,
  type CoreCollectionExclusion,
  type CoreCollectionTask,
  type CoreFollowMonitorSnapshot,
  type CoreHistoryRecord,
  type CoreTaskTarget,
  type CoreSplitCandidate,
  type CoreWorkbenchSnapshot,
  type CoreWorkbenchLiveStatus,
  type CollectorCoreClient,
  getCollectorCoreClient,
  startWorkbenchSnapshotPolling,
} from "./core-client";
import { useAuth } from "./auth-gate";
import { collectionPlatform, collectionPlatformWindows, collectionProfileLabel, collectionProfileUrl, parseCollectionSeedDraft, collectionSeedIdentity } from "./collection-platform";
import "./collection-platform.css";
import { WorkbenchPlatformProvider, useWorkbenchPlatform, usePlatformCore, useCollectionDraft } from "./workbench-platform";
import { collectionAssignmentTasks, assignmentSourceTask, assignmentWindowReason, assignmentHasJoinedWindow, planAssignmentJoins } from "./collection-window-assignment";
import { CollectionFixedFooter } from "./collection-fixed-footer";
import { downloadAccountExport } from "./account-export";
import "./account-export.css";
import { DISCARD_LIMIT_KEYS, readDiscardCountLimits, discardCountLimitsError, discardCountLimitsPayload, parseDiscardLimit, type DiscardLimitKey } from "./collection-discard-limits";
import { collectionFailurePresentation, collectionRowStatus, collectionTaskRows, collectionWindowProgress, collectionRecoveryAction, collectionRecoveryDetails, collectionModeProgressText, collectionModeProgressItems, collectionModeUnseenCount, collectionSourceRecheckModes, collectionRecheckNeedsPause, collectionRecheckCompleted, collectionCompletionCleanupProfile, collectionCompletionCleanupRows } from "./collection-task-rows";
import { applyLiveStatusOverlay } from "./workbench-live-status";
import { formatCount, formatTime } from "./workbench-format";
import { countWindowGroups, createViewSnapshotReader, reconcileWindowOrder, retainEqualOrder, retainLiveSelection, sameComponentProps, sameAccountWorkspaceInputs, sameReviewWorkspaceInputs } from "./workbench-render-state";
import { performActionSafetyControl, type ActionSafetyRequest, performCollectionSafetyControls, runWithSnapshotRefresh, workbenchCommandFeedback, type CollectionSafetyRequest } from "./workbench-command-state";
import {
  MAX_GREETING_MESSAGE_CHARACTERS,
  parseGreetingMessages,
  resolveSuccessfulGreetingMessage,
} from "./greeting-messages";
import { REVIEW_PAGE_SIZE, createReviewQueueReader, reviewQueueKey, selectedReviewIds, type ReviewQueueQuery } from "./review-queue-state";
import "./formal-workbench.css";
import "./review-stages-r54.css";
import "./workbench-polish-r41.css";
import { HomeWorkspace } from "./home-workspace";
import { ReportsWorkspace, HistoryTotals } from "./reports-workspace";
import { PostingWorkspace } from "./posting-workspace";
import { StudioWorkspace } from "./studio-workspace";
import { AccountWorkspace } from "./account-workspace";
import { shareUnchangedJson } from "./snapshot-sharing";
import { historyPage, HISTORY_RENDER_PAGE_SIZE } from "./history-pagination";
import { collectionOperationalHistoryRows, operationalHistoryGroups, operationalHistoryTypeLabel, type OperationalHistoryRow } from "./operational-history";
import { memo, useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction, type FormEvent, type ReactNode } from "react";

export type FormalWorkbenchMode = "home" | "reports" | "accounts" | "collection" | "review" | "public" | "private" | "follow-monitor" | "history" | "nurture" | "posting" | "settings";

export type FormalWorkbenchProps = {
  mode: FormalWorkbenchMode;
};

type Notice = { kind: "success" | "error"; text: string } | null;
type RunSuccessMessage = string | ((result: unknown) => string);
type RunCoreAction = (
  key: string,
  action: (client: CollectorCoreClient) => Promise<unknown>,
  success?: RunSuccessMessage,
) => Promise<boolean>;
type ParallelScreeningWorkers = 1 | 2 | 3;

const SNAPSHOT_PAGE_LIMIT = 2_000;
const STORAGE_CACHE_CLEAR_BUSY_KEY = "storage-cache-clear";

const PAGE_COPY: Record<FormalWorkbenchMode, { title: string; subtitle: string }> = {
  home: { title: "首页", subtitle: "" },
  reports: { title: "报表", subtitle: "" },
  accounts: { title: "账号", subtitle: "独立浏览器窗口 · 账号、窗口绑定与任务状态" },
  nurture: { title: "养号", subtitle: "模板、每日计划与多窗口执行" },
  posting: { title: "发帖", subtitle: "素材、文案与多窗口发帖计划" },
  collection: { title: "采集任务", subtitle: "目标账号、窗口与采集顺序全部由本机 Core 统一调度" },
  review: { title: "人工审核", subtitle: "公开与私密队列分别审核，通过后自动进入对应结果页面" },
  public: { title: "公开页面 · 自动打招呼", subtitle: "仅处理审核通过的公开账号，多窗口自动分组执行" },
  private: { title: "私密页面 · 自动点关注", subtitle: "仅处理审核通过的私密账号，多窗口自动分组执行" },
  "follow-monitor": { title: "检查", subtitle: "自定义窗口，一轮检查关注变化与私信待回复" },
  history: { title: "历史与去重记录", subtitle: "不可删除的审核、排除、任务与执行记录" },
  settings: { title: "系统设置", subtitle: "BitBrowser V2、存储占用与本机 Core 状态" },
};

const ACTIVE_STATES = new Set(["running", "queued", "pending", "starting", "active", "processing"]);
const PAUSED_STATES = new Set(["paused", "suspended"]);
const RESUMABLE_STATES = new Set(["paused", "suspended", "recoverable"]);
const FAILED_STATES = new Set(["failed", "error", "cancelled", "canceled", "stopped", "aborted", "interrupted"]);
const FAILURE_TARGET_STATES = new Set([...FAILED_STATES, "recoverable", "unknown"]);
const SUCCESS_STATES = new Set(["completed", "complete", "success", "succeeded", "done", "confirmed", "already_done"]);
// Automatic actions enter immutable success history only after Core has
// confirmed the external state (or proved it had already been completed).
const ACTION_SUCCESS_STATES = new Set(["confirmed", "already_done"]);

function sortBitBrowserWindows(windows: CoreBitBrowserWindow[]): CoreBitBrowserWindow[] {
  return [...windows].sort((a, b) =>
    (a.provider_order ?? a.serial_number ?? Number.MAX_SAFE_INTEGER)
      - (b.provider_order ?? b.serial_number ?? Number.MAX_SAFE_INTEGER)
    || a.name.localeCompare(b.name, "zh-CN")
    || a.id.localeCompare(b.id));
}

function windowSequenceLabel(window: CoreBitBrowserWindow): string {
  const value = window.serial_number ?? window.provider_order;
  return typeof value === "number" && Number.isFinite(value) ? `序号 ${value}` : "";
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function firstText(record: Record<string, unknown>, keys: string[], fallback = "—"): string {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return fallback;
}

function firstNumber(record: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  }
  return null;
}

function formatBytes(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

function profileOf(candidate: CoreCandidate): Record<string, unknown> {
  return asRecord(candidate.profile);
}

function collectionExclusionReason(record: CoreCollectionExclusion): string {
  const raw = typeof record.reason === "string" ? record.reason.trim() : "";
  const profile = asRecord(record.profile);
  const legacyRules: Array<[string, string, string[]]> = [
    ["followers_above_max", "粉丝数过高", ["followers", "followers_count", "follower_count"]],
    ["followers_below_min", "粉丝数不足", ["followers", "followers_count", "follower_count"]],
    ["followers_unknown", "粉丝数无法确认", []],
    ["following_above_max", "关注数过高", ["following", "following_count"]],
    ["following_below_min", "关注数不足", ["following", "following_count"]],
    ["following_unknown", "关注数无法确认", []],
    ["posts_above_max", "帖子数过高", ["posts", "posts_count", "media_count"]],
    ["posts_below_min", "帖子数不足", ["posts", "posts_count", "media_count"]],
    ["posts_unknown", "帖子数无法确认", []],
    ["activity_days_above_max", "活跃度超过限制", ["activity_days"]],
    ["timestamp_unavailable", "帖子发布时间无法读取", []],
    ["post_grid_unavailable", "帖子列表无法读取", []],
    ["page_read_incomplete", "主页资料读取不完整", []],
  ];
  const legacyDetails = legacyRules.flatMap(([code, label, valueKeys]) => {
    if (!raw.includes(code)) return [];
    const value = valueKeys.length ? firstNumber(profile, valueKeys) : null;
    return [`${label}${value === null ? "" : `（实际 ${formatCount(value)}${code === "activity_days_above_max" ? " 天" : ""}）`}`];
  });
  if (legacyDetails.length) return [...new Set(legacyDetails)].join("；");

  const locationCountry = typeof record.location_country === "string" ? record.location_country.trim() : "";
  if (record.reason_code === "non_us_location") {
    return locationCountry ? `所在地不符合：${locationCountry}（要求美国）` : "所在地明确不符合筛选条件";
  }
  if (record.reason_code === "location_unknown") return "所在地未显示（旧规则曾排除）";
  const fallbackByCode: Record<string, string> = {
    public_basic_conditions_failed: "基础数量不符合筛选条件",
    public_basic_conditions_unknown: "基础数量无法确认",
    public_activity_failed: "活跃度不符合筛选条件",
    public_activity_unknown: "活跃度无法确认",
    professional_account_excluded: "公开专业或商业账号",
    external_bio_link_excluded: "简介包含可点击站外链接",
    verified_account_excluded: "公开蓝 V 认证账号",
    public_zero_posts_excluded: "公开账号帖子数为 0（已按开关规避）",
    private_zero_posts_excluded: "私密账号帖子数为 0（已按开关规避）",
    unknown_zero_posts_excluded: "悬浮卡已确认帖子数为 0（已按开关排除）",
    male_avatar_probability_excluded: "头像识别男性概率达到 55%（已按开关排除）",
    duplicate_account: "重复账号，已跳过采集",
  };
  const fallback = fallbackByCode[raw] || fallbackByCode[record.reason_code];
  // Some immutable legacy rows persisted the internal code in both `reason`
  // and `reason_code`.  Treat that as a code, not as operator-facing copy.
  const readable = !raw || raw === record.reason_code || fallbackByCode[raw]
    ? fallback || "未通过采集筛选"
    : raw;
  if (locationCountry && !readable.includes(locationCountry)) return `${readable} · ${locationCountry}`;
  return readable;
}

function reviewLocation(profile: Record<string, unknown>, screening: Record<string, unknown>): string {
  const direct = firstText(profile, ["location_zh", "location", "location_country", "country", "country_name"], "");
  if (direct) return direct;
  const locationGate = asRecord(screening.location);
  return firstText(locationGate, ["value", "country", "location"], firstText(screening, ["location_country", "country"], "—"));
}

function reviewedPostActivity(profile: Record<string, unknown>): { checked: boolean; days: number | null; noPosts: boolean } {
  const status = firstText(profile, ["post_activity_status"], "");
  const checked = Boolean(status && status !== "not_checked");
  const raw = firstNumber(profile, ["post_activity_days"]);
  const days = ["identified", "timestamp_found", "timestamp_read"].includes(status) && raw !== null && Number.isFinite(raw) && raw >= 0 ? raw : null;
  return { checked, days, noPosts: status === "no_posts" };
}

function reviewActivityLabel(profile: Record<string, unknown>, isPrivate = false): string {
  if (isPrivate) return "不可读取";
  const post = reviewedPostActivity(profile);
  if (post.checked) return post.noPosts ? "0 帖（待人工）" : post.days === null ? "发帖时间未知" : post.days === 0 ? "当天发帖" : `${post.days} 天前发帖`;
  if (firstText(profile, ["activity_status"]) === "no_posts") return "0 帖（待人工）";
  if (firstText(profile, ["activity_status"]) === "story_today") return "当天活跃（快拍）";
  const days = firstNumber(profile, ["activity_days"]);
  if (days !== null) return days === 0 ? "当天活跃" : `${days} 天前`;
  return firstText(profile, ["activity_label", "activity_status", "recent_activity"], "未知");
}

function reviewFailureFlags(screening: Record<string, unknown>, profile: Record<string, unknown> = {}) {
  const reasonCodes = asRecord(screening.basic).reason_codes;
  const reasons = new Set(Array.isArray(reasonCodes) ? reasonCodes.filter((item): item is string => typeof item === "string") : []);
  const activityGate = asRecord(screening.activity);
  const post = reviewedPostActivity(profile);
  const hasKnownActivity = post.checked ? post.days !== null : firstNumber(profile, ["activity_days"]) !== null || firstText(profile, ["activity_status"]) === "story_today";
  return {
    followers: [...reasons].some((reason) => reason.startsWith("followers_") && (reason.endsWith("_above_max") || reason.endsWith("_below_min") || reason.endsWith("_unknown"))),
    following: [...reasons].some((reason) => reason.startsWith("following_") && (reason.endsWith("_above_max") || reason.endsWith("_below_min") || reason.endsWith("_unknown"))),
    posts: [...reasons].some((reason) => reason.startsWith("posts_") && (reason.endsWith("_above_max") || reason.endsWith("_below_min") || reason.endsWith("_unknown"))),
    activity: activityGate.passed === false || (activityGate.enabled === true && activityGate.passed !== true && !hasKnownActivity && activityGate.reason !== "activity_no_posts"),
    location: asRecord(screening.location).passed === false,
  };
}

function reviewSuccessFlags(screening: Record<string, unknown>, profile: Record<string, unknown> = {}) {
  const failures = reviewFailureFlags(screening, profile);
  const post = reviewedPostActivity(profile);
  const activityKnown = post.checked ? post.days !== null : firstNumber(profile, ["activity_days"]) !== null || firstText(profile, ["activity_status"]) === "story_today";
  return {
    followers: firstNumber(profile, ["followers", "followers_count", "follower_count"]) !== null && !failures.followers,
    following: firstNumber(profile, ["following", "following_count"]) !== null && !failures.following,
    posts: firstNumber(profile, ["posts", "posts_count", "media_count"]) !== null && !failures.posts,
    activity: activityKnown && !failures.activity,
    location: asRecord(screening.location).passed === true,
  };
}

function approvalHistoryOf(snapshot: CoreWorkbenchSnapshot): CoreApprovalHistoryCandidate[] {
  const raw = snapshot.history.approvals;
  if (!Array.isArray(raw)) return [];
  return raw.filter((item): item is CoreApprovalHistoryCandidate => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return false;
    const record = item as Record<string, unknown>;
    return typeof record.id === "string" && typeof record.username === "string";
  });
}

function mergedCampaignHistory(snapshot: CoreWorkbenchSnapshot): CoreActionCampaign[] {
  const history = Array.isArray(snapshot.history.actions) ? snapshot.history.actions : [];
  const byId = new Map<string, CoreActionCampaign>();
  for (const campaign of history) byId.set(campaign.id, campaign);
  for (const campaign of snapshot.campaigns) byId.set(campaign.id, campaign);
  return [...byId.values()];
}

function retainedHistoryOf(storage: CoreWorkbenchSnapshot["storage"]): Record<string, number> {
  if (!storage) return {};
  const raw = storage.retained_history;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  return Object.fromEntries(Object.entries(raw).filter((entry): entry is [string, number] => typeof entry[1] === "number" && Number.isFinite(entry[1]) && entry[1] >= 0));
}

function truncatedSnapshotScopes(mode: FormalWorkbenchMode, snapshot: CoreWorkbenchSnapshot): string[] {
  if (mode === "settings") return [];
  if (mode === "home") return [];
  const hasMore = snapshot.has_more;
  if (!hasMore) {
    const legacyTruncated = snapshot.truncated === true
      || (snapshot.truncated !== false && Object.values(snapshot.truncated).some((value) => value === true));
    return legacyTruncated ? ["当前页面"] : [];
  }
  const fields: Record<Exclude<FormalWorkbenchMode, "settings">, Array<[string, string]>> = {
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
    "follow-monitor": [],
    accounts: [],
    home: [],
    reports: [],
    nurture: [],
    posting: [],
    history: [
      ["approval_history", "审核合格历史"],
      ["manual_rejection_history", "人工审核不合格历史"],
      ["collection_exclusion_history", "采集阶段排除历史"],
      ["action_success_history", "自动执行成功历史"],
      ["tasks", "任务历史"],
      ["actions", "自动执行记录"],
    ],
  };
  return [...new Set(fields[mode]
    .filter(([key]) => hasMore[key] === true)
    .map(([, label]) => label))];
}

function storageCleanupNotice(result: unknown): string {
  const payload = asRecord(result);
  const clearedEntries = firstNumber(payload, ["cleared_entries"]) ?? 0;
  const clearedBytes = firstNumber(payload, ["cleared_bytes"]) ?? 0;
  if (clearedEntries === 0) {
    return `没有可清理的已完成审核预览；这不是清理失败。当前界面仅显示优先或最近的最多 ${formatCount(SNAPSHOT_PAGE_LIMIT)} 条，不是存储额度；完整业务记录和 Core 总数仍会保留，SQLite 业务记录仅受本机可用磁盘空间限制。`;
  }
  return `已安全清理 ${formatCount(clearedEntries)} 条已完成审核预览，移除 ${formatBytes(clearedBytes)} 预览数据；待审核预览、账号、任务、检查点、审核历史、成功记录及全局去重均已保留。SQLite 业务记录不设应用额度，清理预览不会减少 Core 总数；当前界面仍仅显示优先或最近的最多 ${formatCount(SNAPSHOT_PAGE_LIMIT)} 条。`;
}

function candidateName(candidate: CoreCandidate): string {
  return firstText(profileOf(candidate), ["full_name", "name", "display_name"], collectionProfileLabel(candidate));
}

function candidateAvatar(candidate: CoreCandidate): string | null {
  const cached = firstText(asRecord(candidate.review_cache), ["avatar_preview"], "");
  if (cached) return cached;
  const value = firstText(profileOf(candidate), ["avatar_url", "profile_pic_url", "profile_pic_url_hd", "avatar"], "");
  return value || null;
}

function accountInitial(username: string, name?: string): string {
  const source = (name || username).replace(/^@/, "").trim();
  return (source.slice(0, 2) || "IG").toUpperCase();
}

function normalizeStatus(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "unknown";
}

function statusClass(value: unknown): string {
  const state = normalizeStatus(value);
  if (SUCCESS_STATES.has(state)) return "success";
  if (FAILED_STATES.has(state)) return "failed";
  if (ACTIVE_STATES.has(state) || state === "recovering_page") return "running";
  if (RESUMABLE_STATES.has(state)) return "paused";
  if (state === "partial") return "warning";
  return "";
}

function StatusBadge({ status }: { status: unknown }) {
  const text = typeof status === "string" && status ? status : "unknown";
  const labels:Record<string,string>={partial:"部分失败",manual_intervention:"需要处理",manual_required:"需要处理",auth_required:"需要登录",blocked_by_auth:"等待登录恢复",waiting_network:"等待自动恢复",recovering_page:"正在验证恢复",running:"运行中",working:"采集中",recoverable:"可继续",paused:"已暂停",completed:"已完成",ready:"空闲"};
  return <span className={`formal-status ${statusClass(status)}`}>{labels[text]||text}</span>;
}

function EmptyState({ icon, title }: { icon?: ReactNode; title: string }) {
  return (
    <div className="formal-empty">
      <div>
        {icon ?? <Inbox size={28} />}
        <strong>{title}</strong>
      </div>
    </div>
  );
}

function Avatar({ candidate, small = false }: { candidate: CoreCandidate; small?: boolean }) {
  const avatar = candidateAvatar(candidate);
  const name = candidateName(candidate);
  const [imageFailed, setImageFailed] = useState(false);
  useEffect(() => setImageFailed(false), [avatar]);
  return (
    <div className={`formal-avatar ${small ? "small" : ""}`} aria-label={`${name} 头像`}>
      {avatar && !imageFailed ? <img src={avatar} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setImageFailed(true)} /> : accountInitial(candidate.username, name)}
    </div>
  );
}

function Panel({ icon, title, actions, children, className = "" }: {
  icon: ReactNode;
  title: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`formal-panel ${className}`}>
      <header className="formal-panel-header">
        <div className="formal-panel-title">
          <span>{icon}</span>
          <div><h2>{title}</h2></div>
        </div>
        {actions}
      </header>
      {children}
    </section>
  );
}

function StatCard({ icon, label, value }: { icon: ReactNode; label: string; value: number | null | undefined }) {
  return (
    <div className="formal-stat">
      <span className="formal-stat-icon">{icon}</span>
      <div><small>{label}</small><strong>{formatCount(value)}</strong></div>
    </div>
  );
}

export function Rail({ mode, refresh, refreshing = false }: { mode: FormalWorkbenchMode; refresh: () => Promise<void>; refreshing?: boolean }) {
  const items: Array<{ mode: FormalWorkbenchMode; label: string; icon: ReactNode }> = [
    { mode: "home", label: "首页", icon: <Home size={22} /> },
    { mode: "accounts", label: "账号", icon: <UserRoundCheck size={22} /> },
    { mode: "follow-monitor", label: "检查", icon: <UserRoundCheck size={22} /> },
    { mode: "nurture", label: "养号", icon: <Leaf size={22} /> },
    { mode: "posting", label: "发帖", icon: <Send size={22} /> },
    { mode: "collection", label: "采集", icon: <Users size={22} /> },
    { mode: "review", label: "审核", icon: <ShieldCheck size={22} /> },
    { mode: "public", label: "公开", icon: <MessageCircle size={22} /> },
    { mode: "private", label: "私密", icon: <LockKeyhole size={22} /> },
    { mode: "reports", label: "报表", icon: <ClipboardList size={22} /> },
    { mode: "history", label: "历史", icon: <History size={22} /> },
  ];
  return (
    <aside className="formal-rail">
      <button type="button" className="formal-brand" aria-label="刷新" title="刷新" aria-busy={refreshing} disabled={refreshing} onClick={() => void refresh()}><img src="./war-wolf.svg" alt="" width="52" height="52"/></button>
      <nav className="formal-nav" aria-label="主导航">
        {items.map((item) => (
          <a key={item.mode} data-nav={item.mode} href={item.mode === "home" ? "#/" : `#/${item.mode}`} aria-current={mode === item.mode ? "page" : undefined}>
            {item.icon}<span>{item.label}</span>{item.mode==="accounts"&&<UnreadBadge/>}
          </a>
        ))}
      </nav>
      <nav className="formal-nav formal-nav-bottom" aria-label="设置导航">
        <a data-nav="settings" href="#/settings" aria-current={mode === "settings" ? "page" : undefined}><Settings size={22} /><span>设置</span></a>
      </nav>
    </aside>
  );
}

function WindowSelector({
  windows,
  selected,
  setSelected,
  setSelectedOrder,
  run,
  disabled,
  title = "窗口选择与排序",
}: {
  windows: CoreBitBrowserWindow[];
  selected: Set<string>;
  setSelected: (next: Set<string>) => void;
  setSelectedOrder: Dispatch<SetStateAction<string[]>>;
  run: RunCoreAction;
  disabled: (key?: string) => boolean;
  title?: string;
}) {
  const [query, setQuery] = useState("");
  const [groupFilter, setGroupFilter] = useState("all");
  const [orderedIds, setOrderedIds] = useState<string[]>([]);

  const windowGroupCounts = useMemo(() => countWindowGroups(windows), [windows]);
  const windowGroups = useMemo(() => [...windowGroupCounts.keys()].sort((a, b) => a.localeCompare(b, "zh-CN")), [windowGroupCounts]);

  const providerOrder = useMemo(() => sortBitBrowserWindows(windows), [windows]);

  useEffect(() => {
    setOrderedIds((previous) => reconcileWindowOrder(previous, providerOrder.map((window) => window.id)));
  }, [providerOrder]);

  const ordered = useMemo(() => {
    const byId = new Map(windows.map((window) => [window.id, window]));
    const base = orderedIds.map((id) => byId.get(id)).filter((value): value is CoreBitBrowserWindow => Boolean(value));
    const rank = (item: CoreBitBrowserWindow) => item.locked ? 3 : selected.has(item.id) ? 0 : (item.ready || item.opened || ["opened", "open", "running"].includes(String(item.window_state || "").toLowerCase()) ? 1 : 2);
    return base.map((item, index) => ({ item, index }))
      .sort((a, b) => rank(a.item) - rank(b.item) || a.index - b.index)
      .map(({ item }) => item);
  }, [orderedIds, selected, windows]);

  useEffect(() => {
    const live = new Set(windows.filter((window) => !window.locked).map((window) => window.id));
    const nextSelected = [...selected].filter((id) => live.has(id));
    const orderedSelected = ordered.filter((item) => selected.has(item.id) && live.has(item.id)).map((item) => item.id);
    setSelectedOrder((previous) => retainEqualOrder(previous, orderedSelected));
    if (nextSelected.length !== selected.size) setSelected(new Set(nextSelected));
  }, [ordered, orderedIds, selected, setSelected, setSelectedOrder, windows]);

  const filtered = ordered.filter((window) => {
    const needle = query.trim().toLocaleLowerCase();
    const groupMatches = groupFilter === "all" || String(window.group || "").trim() === groupFilter;
    const textMatches = !needle
      || window.name.toLocaleLowerCase().includes(needle)
      || window.id.toLocaleLowerCase().includes(needle)
      || String(window.serial_number ?? window.provider_order ?? "").includes(needle)
      || String(window.group || "").toLocaleLowerCase().includes(needle);
    return groupMatches && textMatches;
  });

  function toggle(id: string) {
    if (windows.find((window) => window.id === id)?.locked) return;
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  }

  function move(id: string, delta: number) {
    setOrderedIds((current) => {
      const index = current.indexOf(id);
      const destination = index + delta;
      if (index < 0 || destination < 0 || destination >= current.length) return current;
      const next = [...current];
      [next[index], next[destination]] = [next[destination], next[index]];
      return next;
    });
  }

  return (
    <Panel
      icon={<Monitor size={19} />}
      title={title}

      actions={<><span className="formal-selected-count">已选 {selected.size} 个</span><button className="formal-button compact" disabled={disabled("windows-refresh")} onClick={() => void run("windows-refresh", (client) => client.refreshBitBrowserWindows(), "窗口列表已刷新")}><RefreshCw size={15} />刷新</button></>}
    >
      <div className="formal-panel-body">
        <div className="formal-toolbar">
          <label className="formal-field" style={{ minWidth: 170 }}>
            <span>窗口分组</span>
            <select className="formal-input" value={groupFilter} onChange={(event) => setGroupFilter(event.target.value)}>
              <option value="all">全部分组（{windows.length}）</option>
              {windowGroups.map((group) => <option key={group} value={group}>{group}（{windowGroupCounts.get(group) || 0}）</option>)}
            </select>
          </label>
          <label className="formal-field formal-grow">
            <span>按窗口名称、序号或分组搜索</span>
            <span style={{ position: "relative", display: "block" }}>
              <Search size={16} style={{ position: "absolute", left: 11, top: 13, color: "#7d899f" }} />
              <input className="formal-input" style={{ paddingLeft: 36 }} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入名称、序号或分组" />
            </span>
          </label>
          <button className="formal-button" disabled={!selected.size || disabled("windows-open")} onClick={() => void run("windows-open", (client) => client.openSelectedBitBrowserWindows([...selected]), "所选窗口已提交打开")}><Plus size={16} />添加窗口</button>
        </div>
      </div>
      <div className="formal-panel-body formal-scroll short">
        <div className="formal-list">
          {filtered.map((window) => {
            const actionCounts = asRecord(window.action_counts);
            const greetSuccesses = firstNumber(actionCounts, ["greet_successes"]);
            const followSuccesses = firstNumber(actionCounts, ["follow_successes"]);
            const greetCurrent = firstNumber(actionCounts, ["greet_current"]);
            const followCurrent = firstNumber(actionCounts, ["follow_current"]);
            const hasCurrentWindow = Boolean(firstText(actionCounts, ["greet_reset_at", "follow_reset_at"], ""));
            const isLocked = Boolean(window.locked);
            const lockLabel = window.lock_operation === "collection" ? "采集任务占用" : window.lock_operation === "monitor" ? "关注检测占用" : window.lock_operation === "greet" ? "打招呼任务占用" : window.lock_operation === "follow" ? "点关注任务占用" : "任务占用";
            return (
            <div key={window.id} className={`formal-row ${selected.has(window.id) ? "is-selected" : ""} ${isLocked ? "is-locked" : ""}`}>
              <label className="formal-check">
                <input type="checkbox" checked={selected.has(window.id)} disabled={isLocked} onChange={() => toggle(window.id)} />
              </label>
              <Monitor size={21} color={window.ready || window.opened ? "#6ee7b7" : "#8190aa"} />
              <div className="formal-row-main">
                <strong>{window.name}</strong>
                {windowSequenceLabel(window) || window.group ? <small>{[windowSequenceLabel(window), window.group].filter(Boolean).join(" · ")}</small> : null}
                <small className="formal-window-counts">打招呼 {formatCount(greetSuccesses)} / 关注 {formatCount(followSuccesses)}{hasCurrentWindow ? ` · 本轮打招呼 ${formatCount(greetCurrent)} / 关注 ${formatCount(followCurrent)}` : ""}</small>
                {isLocked ? <small className="formal-window-lock"><LockKeyhole size={13} />{lockLabel}，其他页面不可选择</small> : null}
              </div>
              <StatusBadge status={window.ready ? "ready" : window.window_state || (window.opened ? "opened" : "closed")} />
              <div className="formal-row-actions">
                <button className="formal-button compact" aria-label="上移窗口" disabled={isLocked} onClick={() => move(window.id, -1)}><ArrowUp size={15} /></button>
                <button className="formal-button compact" aria-label="下移窗口" disabled={isLocked} onClick={() => move(window.id, 1)}><ArrowDown size={15} /></button>
                <button className="formal-button compact" disabled={isLocked || disabled(`window-open-${window.id}`)} onClick={() => void run(`window-open-${window.id}`, (client) => client.openBitBrowserWindow(window.id), `${window.name} 已提交打开`)}><ExternalLink size={15} />打开</button>
              </div>
            </div>
            );
          })}
          {!filtered.length ? <EmptyState icon={<Monitor size={28} />} title="没有匹配的窗口" /> : null}
        </div>
      </div>
    </Panel>
  );
}

type SplitWindowAssignmentEditor = {
  candidateId: string | null;
  usernames: string[];
  initialWindowIds: string[];
  candidate?: CoreSplitCandidate;
  savedWindowIds?: string[];
  savedCandidates?: CoreSplitCandidate[];
};

function SplitWindowAssignmentDialog({
  editor,
  windows,
  tasks,
  saving,
  onClose,
  onSave,
}: {
  editor: SplitWindowAssignmentEditor;
  windows: CoreBitBrowserWindow[];
  tasks: CoreCollectionTask[];
  saving: boolean;
  onClose: () => void;
  onSave: (windowIds: string[]) => Promise<void>;
}) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(editor.initialWindowIds));
  const [query, setQuery] = useState("");
  const [groupFilter, setGroupFilter] = useState("all");
  const ordered = useMemo(() => sortBitBrowserWindows(windows), [windows]);
  const windowGroupCounts = useMemo(() => countWindowGroups(windows), [windows]);
  const groups = useMemo(() => [...windowGroupCounts.keys()].sort((a, b) => a.localeCompare(b, "zh-CN")), [windowGroupCounts]);
  const visible = ordered.filter((window) => {
    const needle = query.trim().toLocaleLowerCase();
    return (groupFilter === "all" || String(window.group || "").trim() === groupFilter)
      && (!needle
        || window.name.toLocaleLowerCase().includes(needle)
        || window.id.toLocaleLowerCase().includes(needle)
        || String(window.serial_number ?? window.provider_order ?? "").includes(needle)
        || String(window.group || "").toLocaleLowerCase().includes(needle));
  });
  const knownIds = new Set(windows.map((window) => window.id));
  const missingIds = [...selected].filter((profileId) => !knownIds.has(profileId));
  const subject = editor.candidateId
    ? `@${editor.usernames[0] || "该分裂号"}`
    : `${editor.usernames.length} 个新分裂号`;

  const committed = editor.savedWindowIds !== undefined;
  const sourceTask = assignmentSourceTask(tasks, editor.candidate);

  function toggle(window: CoreBitBrowserWindow) {
    if (saving || committed || (!selected.has(window.id) && assignmentWindowReason(window, tasks, editor.candidate))) return;
    const next = new Set(selected);
    if (next.has(window.id)) next.delete(window.id); else next.add(window.id);
    setSelected(next);
  }

  return (
    <div className="formal-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !saving) onClose(); }}>
      <section className="formal-modal collection-assignment-dialog" role="dialog" aria-modal="true" aria-labelledby="split-window-dialog-title">
        <header className="formal-modal-header">
          <div>
            <h2 id="split-window-dialog-title">为 {subject} 指定采集窗口</h2>

          </div>
          <button className="formal-button compact" type="button" disabled={saving} onClick={onClose} aria-label="关闭指定窗口对话框"><X size={16} /></button>
        </header>
        <div className="formal-modal-body">
          {committed ? <p className="collection-affinity-warning" role="status">已保存的目标不会重复添加，未成功添加的账号保留在输入框。窗口加入尚未完成，可重试；若需修改选择，请关闭后从等待列表重新编辑。</p> : sourceTask ? <p className="collection-affinity-warning">继续原任务</p> : null}
          <div className="formal-toolbar collection-assignment-toolbar">
            <label className="formal-field">
              <span>窗口分组</span>
              <select className="formal-input" value={groupFilter} onChange={(event) => setGroupFilter(event.target.value)}>
                <option value="all">全部分组（{windows.length}）</option>
                {groups.map((group) => <option key={group} value={group}>{group}（{windowGroupCounts.get(group) || 0}）</option>)}
              </select>
            </label>
            <label className="formal-field formal-grow">
              <span>按窗口名称、序号或分组搜索</span>
              <span className="collection-assignment-search"><Search size={16} /><input className="formal-input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索窗口" /></span>
            </label>
            <span className="formal-selected-count">已指定 {selected.size} 个</span>
          </div>
          <div className="formal-list formal-scroll collection-assignment-list">
            {visible.map((window) => {
              const blockedReason = assignmentWindowReason(window, tasks, editor.candidate);
              const lockedByAnotherTask = Boolean(blockedReason);
              return (
                <label key={window.id} className={`formal-row collection-window-option ${selected.has(window.id) ? "is-selected" : ""} ${lockedByAnotherTask ? "is-locked" : ""}`}>
                  <span className="formal-check"><input type="checkbox" checked={selected.has(window.id)} disabled={saving || committed || (lockedByAnotherTask && !selected.has(window.id))} onChange={() => toggle(window)} /></span>
                  <Monitor size={20} color={window.ready || window.opened ? "#6ee7b7" : "#8190aa"} />
                  <span className="formal-row-main">
                    <strong className="collection-window-option-name">{window.name}</strong>
                    {windowSequenceLabel(window) || window.group ? <small>{[windowSequenceLabel(window), window.group].filter(Boolean).join(" · ")}</small> : null}
                    {lockedByAnotherTask ? <small className="formal-window-lock"><LockKeyhole size={13} />{blockedReason}</small> : window.locked ? <small>当前采集完成后可领取</small> : null}
                  </span>
                </label>
              );
            })}
            {!visible.length ? <EmptyState icon={<Monitor size={28} />} title="没有匹配的窗口" /> : null}
            {missingIds.map((profileId) => <div className="formal-row collection-window-option is-missing" key={`missing-${profileId}`}><AlertTriangle size={20} /><div className="formal-row-main"><strong>窗口当前不在窗口列表</strong></div><button className="formal-button compact danger" type="button" disabled={saving || committed} onClick={() => setSelected((current) => { const next = new Set(current); next.delete(profileId); return next; })}>移除</button></div>)}
          </div>
        </div>
        <footer className="formal-modal-footer">
          <button className="formal-button" type="button" disabled={saving || committed} onClick={() => setSelected(new Set())}><RotateCcw size={16} />恢复自动分配</button>
          <span className="formal-grow" />
          <button className="formal-button" type="button" disabled={saving} onClick={onClose}>{committed ? "关闭" : "取消"}</button>
          <button className="formal-button primary" type="button" disabled={saving} onClick={() => void onSave([...selected])}>{saving ? <LoaderCircle className="animate-spin" size={17} /> : <Check size={17} />}{committed ? "重试窗口加入" : "保存设置"}</button>
        </footer>
      </section>
    </div>
  );
}


const StableAccountWorkspace = memo(AccountWorkspace, sameAccountWorkspaceInputs);

function useFormalCore() {
  const { platform } = useWorkbenchPlatform();
  const [snapshot, setSnapshot] = useState<CoreWorkbenchSnapshot | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const snapshotErrorRef = useRef<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyKeys, setBusyKeys] = useState<Set<string>>(new Set());
  const busyKeysRef = useRef<Set<string>>(new Set());
  const [pendingKeys, setPendingKeys] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState<Notice>(null);
  const clientRef = useRef<CollectorCoreClient | null>(null);
  const refreshRef = useRef<(() => Promise<CoreWorkbenchSnapshot>) | null>(null);
  const refreshInFlightRef = useRef<Promise<void> | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const liveStatusRef = useRef<CoreWorkbenchLiveStatus | null>(null);
  const [liveStatus, setLiveStatus] = useState<CoreWorkbenchLiveStatus | null>(null);

  useEffect(() => {
    let controller: ReturnType<typeof startWorkbenchSnapshotPolling> | null = null;
    let liveTimer: number | null = null;
    let liveInFlight = false;
    let disposed = false;
    try {
      const client = getCollectorCoreClient(undefined, platform);
      clientRef.current = client;
      const startPolling = () => {
        if (disposed || controller || document.visibilityState === "hidden") return;
        controller = startWorkbenchSnapshotPolling({
          // Five seconds keeps task controls responsive while reducing the lifetime
          // history/database load by 40% compared with the former three-second loop.
          intervalMs: 5_000,
          limit: SNAPSHOT_PAGE_LIMIT,
          historyLimit: SNAPSHOT_PAGE_LIMIT,
          // Keep every canonical row and freshness flag, but avoid decoding and
          // IPC-copying the same historical lists under seven legacy aliases.
          compact: true,
          platform,
          onSnapshot(value) {
            setSnapshot(current => value.revision < client.lastCommandSnapshotSeq ? current
              : shareUnchangedJson(current, applyLiveStatusOverlay(value, liveStatusRef.current, client.lastCommandSnapshotSeq)));
            snapshotErrorRef.current = null;
            setError(null);
            setLoading(false);
          },
          onError(reason) {
            snapshotErrorRef.current = reason;
            setError(reason);
            setLoading(false);
          },
        });
        refreshRef.current = controller.refresh;
        const pollLiveStatus = async () => {
          if (disposed || document.visibilityState === "hidden" || liveInFlight) return;
          liveInFlight = true;
          try {
            const rawLive = await client.liveStatus();
            const live = { ...rawLive, tasks: rawLive.tasks.filter(item => collectionPlatform(item.task) === platform) };
            if (disposed) return;
            liveStatusRef.current = live;
            setLiveStatus(current => live.revision < client.lastCommandSnapshotSeq ? current : live);
            // React may execute this updater after another control completes.
            // Read the command fence here, beside the actual state delivery.
            setSnapshot((current) => current ? applyLiveStatusOverlay(current, live, client.lastCommandSnapshotSeq) : current);
          } catch {
            // The full authoritative snapshot owns error state.  A missed lightweight
            // heartbeat must never disable task controls or trigger extra browser probes.
          } finally {
            liveInFlight = false;
          }
        };
        void pollLiveStatus();
        liveTimer = window.setInterval(() => void pollLiveStatus(), 2_500);
      };
      const handleVisibilityChange = () => {
        if (document.visibilityState === "hidden") {
          controller?.stop();
          controller = null;
          if (liveTimer !== null) window.clearInterval(liveTimer);
          liveTimer = null;
          refreshRef.current = null;
          return;
        }
        startPolling();
      };
      document.addEventListener("visibilitychange", handleVisibilityChange);
      startPolling();
      return () => {
        disposed = true;
        document.removeEventListener("visibilitychange", handleVisibilityChange);
        controller?.stop();
        controller = null;
        if (liveTimer !== null) window.clearInterval(liveTimer);
        liveTimer = null;
        clientRef.current = null;
        refreshRef.current = null;
        liveStatusRef.current = null;
      };
    } catch (reason) {
      snapshotErrorRef.current = reason instanceof Error ? reason : new Error(String(reason));
      setError(snapshotErrorRef.current);
      setLoading(false);
    }
    return () => {
      disposed = true;
      controller?.stop();
      if (liveTimer !== null) window.clearInterval(liveTimer);
      liveTimer = null;
      clientRef.current = null;
      refreshRef.current = null;
      liveStatusRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 3_600);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const execute = useCallback(async (key: string, action: (client: CollectorCoreClient) => Promise<unknown>, success?: RunSuccessMessage, backgroundRefresh = false) => {
    const client = clientRef.current;
    if (!client) {
      setNotice({ kind: "error", text: "桌面 Core 不可用，操作已阻断" });
      return false;
    }
    // Recheck at dispatch time: an asynchronous UI continuation can outlive the
    // button render that originally allowed it. Safety controls and the independently
    // loaded review view opt into the background refresh path.
    if (!backgroundRefresh && snapshotErrorRef.current) {
      setNotice({ kind: "error", text: "列表尚未恢复，新增或变更操作已阻断；已有任务仍可暂停或停止" });
      return false;
    }
    if (busyKeysRef.current.has(key)) return false;
    busyKeysRef.current.add(key);
    setBusyKeys((current) => new Set(current).add(key));
    setPendingKeys((current) => new Set(current).add(key));
    try {
      const result = await runWithSnapshotRefresh({
        action: () => action(client),
        refresh: async () => { await refreshRef.current?.(); },
        backgroundRefresh,
        onCommandSettled: () => setPendingKeys((current) => {
          const next = new Set(current);
          next.delete(key);
          return next;
        }),
      });
      if (success) setNotice({ kind: "success", text: typeof success === "function" ? success(result) : success });
      return true;
    } catch (reason) {
      setNotice({ kind: "error", text: reason instanceof Error ? reason.message : String(reason) });
      return false;
    } finally {
      busyKeysRef.current.delete(key);
      setBusyKeys((current) => {
        const next = new Set(current);
        next.delete(key);
        return next;
      });
    }
  }, []);

  const run = useCallback((key: string, action: (client: CollectorCoreClient) => Promise<unknown>, success?: RunSuccessMessage) =>
    execute(key, action, success), [execute]);
  // Review has its own paginated authoritative reader and local error gate.
  // A slow full-history snapshot must not block a freshly loaded review page,
  // nor hold a completed review command for another 30-second full refresh.
  const reviewRun = useCallback((key: string, action: (client: CollectorCoreClient) => Promise<unknown>, success?: RunSuccessMessage) =>
    execute(key, action, success, true), [execute]);
  const reviewDisabled = useCallback((key?: string) => key ? busyKeys.has(key) : busyKeys.size > 0, [busyKeys]);
  const safetyRun = useCallback((key: string, requests: readonly CollectionSafetyRequest[], success: string) =>
    execute(key, (client) => performCollectionSafetyControls(client, requests), success, true), [execute]);
  const actionSafetyRun = useCallback((key: string, request: ActionSafetyRequest, success: string) =>
    execute(key, (client) => performActionSafetyControl(client, request), success, true), [execute]);
  const commandFeedback = useCallback((key: string) =>
    workbenchCommandFeedback(Boolean(error), busyKeys.has(key), pendingKeys.has(key)), [error, busyKeys, pendingKeys]);

  return {
    snapshot,
    liveStatus,
    error,
    loading,
    notice,
    run,
    reviewRun, reviewDisabled,
    collectionControls: { run: safetyRun, feedback: commandFeedback, snapshotStale: Boolean(error) },
    actionControls: { run: actionSafetyRun, feedback: commandFeedback },
    // Snapshot-dependent mutations stay blocked; independently loaded review
    // rows and explicitly bound safety controls use the separate entries above.
    disabled: useCallback((key?: string) => Boolean(error) || (key ? busyKeys.has(key) : busyKeys.size > 0), [busyKeys, error]),
    refreshing,
    refresh: useCallback(() => {
      // One shared lock survives route changes and closes the same-tick click gap.
      // Refresh only the existing data subscription; never reload browser windows.
      if (refreshInFlightRef.current) return refreshInFlightRef.current;
      const readSnapshot = refreshRef.current;
      if (!readSnapshot) return Promise.resolve();
      setRefreshing(true);
      const request = Promise.resolve().then(readSnapshot).then(() => undefined).catch(() => {
        // The polling controller reports failures and retains bounded retries.
        // UI events must not create unhandled rejections or clear the stale guard.
      }).finally(() => {
        if (refreshInFlightRef.current === request) {
          refreshInFlightRef.current = null;
          setRefreshing(false);
        }
      });
      refreshInFlightRef.current = request;
      return request;
    }, []),
  };
}

type CollectionControls = ReturnType<typeof useFormalCore>["collectionControls"];
type ActionControls = ReturnType<typeof useFormalCore>["actionControls"];

function CollectionWorkspace({ snapshot, run, disabled, collectionControls }: WorkspaceProps & { collectionControls: CollectionControls }) {
  const { platform } = useWorkbenchPlatform();
  // Completed and recoverable tasks are the durable Core-owned configuration
  // source. This survives app restarts without browser-side storage.
  const saved = useMemo(() => {
    let latest: CoreCollectionTask | undefined;
    for (const task of snapshot.tasks) {
      if (collectionPlatform(task) !== platform) continue;
      if (!latest || String(task.updated_at || task.created_at || "").localeCompare(String(latest.updated_at || latest.created_at || "")) > 0) latest = task;
    }
    const settings = asRecord(latest?.settings);
    return {
      followers: Boolean(latest?.modes?.includes("followers")),
      following: Boolean(latest?.modes?.includes("following")),
      autoClassify: settings.auto_classify === true,
      openAiReview: settings.gpt_enabled === true,
      excludeVerified: latest ? settings.exclude_verified === true : true,
      // Missing means a task created before this switch existed.  Default the
      // next task to the safer policy while preserving an explicitly saved off.
      excludePublicZeroPosts: settings.exclude_public_zero_posts !== false,
      discardCountLimits: readDiscardCountLimits(settings),
      parallelScreeningWorkers: firstNumber(settings, ["parallel_screening_workers"]) || 1,
      selectedWindows: latest?.windows?.map((item) => item.profile_id) || [],
    } as Record<string, unknown>;
  }, [snapshot.tasks, platform]);
  const [targetDraft, setTargetDraft] = useCollectionDraft();
  const draftSeeds = useMemo(() => parseCollectionSeedDraft(targetDraft, platform), [targetDraft, platform]);
  const collectionWindows = useMemo(() => collectionPlatformWindows(snapshot.windows, platform), [snapshot.windows, platform]);
  // Navigation and resize only affect presentation. Core owns running
  // task lifetimes; no UI cleanup is allowed to pause or stop a task.
  function updateTargetDraft(value: string) {
    setTargetDraft(value);
  }
  const createTaskRef = useRef(false);
  const waitingCandidates = snapshot.split_candidates.filter((candidate) =>
    candidate.kind === "manual"
    && candidate.queue_state === "queued"
    && !candidate.queued_target_id
  );
  const targets = waitingCandidates.map((candidate) => candidate.username);
  const splitClaimLocked = snapshot.split_claim_locked === true;
  const individuallyLockedCount = waitingCandidates.filter(candidate => candidate.locked === true).length;
  const claimableWaitingCount = splitClaimLocked ? 0 : waitingCandidates.length - individuallyLockedCount;
  const [selectedWindows, setSelectedWindows] = useState<Set<string>>(() => new Set(Array.isArray(saved.selectedWindows) ? saved.selectedWindows.filter((id): id is string => typeof id === "string") : []));
  const [selectedWindowOrder, setSelectedWindowOrder] = useState<string[]>([]);
  const [assignmentEditor, setAssignmentEditor] = useState<SplitWindowAssignmentEditor | null>(null);
  const assignmentSaveRef = useRef(false);
  const draftAddRef = useRef(false);
  const assignmentSnapshotRef = useRef(snapshot);
  assignmentSnapshotRef.current = snapshot;
  const savedBool = (key: string) => saved[key] === true;
  const [followers, setFollowers] = useState(() => savedBool("followers"));
  const [following, setFollowing] = useState(() => savedBool("following"));
  const [autoClassify, setAutoClassify] = useState(() => savedBool("autoClassify"));
  const [openAiReview, setOpenAiReview] = useState(() => savedBool("openAiReview"));
  const [excludeVerified, setExcludeVerified] = useState(() => savedBool("excludeVerified"));
  const [excludePublicZeroPosts, setExcludePublicZeroPosts] = useState(() => savedBool("excludePublicZeroPosts"));
  const [discardLimits, setDiscardLimits] = useState(() => saved.discardCountLimits as ReturnType<typeof readDiscardCountLimits>);
  const [parallelScreeningWorkers, setParallelScreeningWorkers] = useState<ParallelScreeningWorkers>(() => {
    const value = Number(saved.parallelScreeningWorkers);
    return value === 2 || value === 3 ? value : 1;
  });
  const discardLimitsError = discardCountLimitsError(discardLimits);
  const filtersValid = (!discardLimitsError);

  function setAllDiscardLimitsUnlimited() {
    setDiscardLimits(current => ({ ...current, limits: Object.fromEntries(DISCARD_LIMIT_KEYS.map(key => [key, "0"])) as typeof current.limits }));
  }

  const runningTasks = snapshot.tasks.filter((task) => ACTIVE_STATES.has(normalizeStatus(task.status)) || normalizeStatus(task.status) === "waiting_network");
  const resumableTasks = snapshot.tasks.filter((task) => RESUMABLE_STATES.has(normalizeStatus(task.status)));
  const acceptingTask = collectionAssignmentTasks(snapshot.tasks).find(task => collectionPlatform(task) === platform);

  function parsedDraftTargets() {
    const parsed = parseCollectionSeedDraft(targetDraft, platform);
    return parsed.valid ? parsed.targets : [];
  }

  async function ensureAssignedWindowsJoined(allowedWindowIds: string[], candidates: CoreSplitCandidate[] = []): Promise<boolean> {
    if (!allowedWindowIds.length) return true;
    setSelectedWindows((current) => new Set([...current, ...allowedWindowIds]));
    return run("collection-join-assigned-windows", async (client) => {
      try {
        const current = assignmentSnapshotRef.current;
        const joins = planAssignmentJoins(current.tasks, current.windows, allowedWindowIds, candidates);
        for (const join of joins) await client.addCollectionWindows(join.taskId, join.windowIds);
      } catch (reason) {
        throw new Error(`窗口指派已保存，但窗口加入未完成：${reason instanceof Error ? reason.message : String(reason)}。可重试加入，不会重复添加目标。`);
      }
    }, "窗口指派已保存，等待符合条件的窗口领取");
  }

  async function addDraftTargets(allowedWindowIds: string[] = [], draftTargets?: string[], onSaved?: (candidates: CoreSplitCandidate[]) => void): Promise<boolean> {
    if (draftAddRef.current) return false;
    const additions = draftTargets || parsedDraftTargets();
    if (!additions.length) {
      return run("collection-validate-targets", async () => {
        throw new Error("请输入有效的 Instagram 账号名称或个人主页链接。");
      });
    }
    draftAddRef.current = true;
    try {
    const savedCandidates: CoreSplitCandidate[] = [];
    const added = await run("collection-add-waiting-targets", async client => {
      await addSplitTargetsWithConfirmation(client, additions, allowedWindowIds, candidates => savedCandidates.push(...candidates));
    }, "分裂号添加检查已完成");
    if (savedCandidates.length) {
      const persistedNames = new Set(savedCandidates.map(candidate => candidate.username.toLowerCase()));
      setTargetDraft((current) => {
        const parsed = parseCollectionSeedDraft(current, platform);
        return parsed.valid && parsed.targets.join(",") === additions.join(",") ? parsed.targets.filter(name => !persistedNames.has(collectionSeedIdentity(name, platform))).join("\n") : current;
      });
      onSaved?.(savedCandidates);
      if (!added) return false; // Preserve confirmed writes even if a later requeue failed.
      return await ensureAssignedWindowsJoined(allowedWindowIds, savedCandidates);
    }
    return added;
    } finally { draftAddRef.current = false; }
  }

  function openDraftWindowAssignment() {
    const additions = parsedDraftTargets();
    if (!additions.length) { void addDraftTargets(); return; }
    setAssignmentEditor({
      candidateId: null,
      usernames: additions,
      initialWindowIds: selectedWindowOrder,
    });
  }

  function openWaitingWindowAssignment(candidate: CoreWorkbenchSnapshot["split_candidates"][number]) {
    setAssignmentEditor({
      candidateId: candidate.id,
      usernames: [candidate.username],
      initialWindowIds: Array.isArray(candidate.allowed_window_ids) ? candidate.allowed_window_ids : [],
      candidate,
    });
  }

  async function saveWindowAssignment(windowIds: string[]) {
    const editor = assignmentEditor;
    if (!editor || assignmentSaveRef.current) return;
    assignmentSaveRef.current = true;
    let editorVersion = editor;
    const committed = (candidates: CoreSplitCandidate[]) => {
      editorVersion = { ...editor, savedWindowIds: [...windowIds], savedCandidates: candidates };
      const savedEditor = editorVersion;
      setAssignmentEditor(current => current === editor ? savedEditor : current);
    };
    const closeSavedEditor = () => setAssignmentEditor(current => current === editor || current === editorVersion ? null : current);
    try {
      if (editor.savedWindowIds !== undefined) {
        if (await ensureAssignedWindowsJoined(editor.savedWindowIds, editor.savedCandidates)) closeSavedEditor();
        return;
      }
      if (!editor.candidateId) {
        if (await addDraftTargets(windowIds, editor.usernames, committed)) closeSavedEditor();
        return;
      }
      let savedCandidate: CoreSplitCandidate | undefined;
      const savedAssignment = await run(
        `collection-waiting-${editor.candidateId}`,
        async (client) => {
          const current = assignmentSnapshotRef.current;
          const currentCandidate = current.split_candidates.find(candidate => candidate.id === editor.candidateId);
          if (!currentCandidate || currentCandidate.queue_state !== "queued" || currentCandidate.queued_target_id) throw new Error("此目标已被领取或移除，请关闭弹窗查看最新任务");
          planAssignmentJoins(current.tasks, current.windows, windowIds, [currentCandidate]);
          savedCandidate = await client.assignWaitingSplitWindows(editor.candidateId as string, windowIds);
        },
      );
      if (savedAssignment && savedCandidate) {
        committed([savedCandidate]);
        if (await ensureAssignedWindowsJoined(windowIds, [savedCandidate])) closeSavedEditor();
      }
    } finally { assignmentSaveRef.current = false; }
  }

  async function removeWaitingTarget(candidateId: string) {
    await run(
      `collection-waiting-${candidateId}`,
      (client) => client.deleteWaitingSplitTarget(candidateId),
      "已从等待列表删除",
    );
  }

  async function addSelectedWindows() {
    if (!acceptingTask || !selectedWindowOrder.length) return;
    await run("collection-add-windows", (client) => client.addCollectionWindows(acceptingTask.id, selectedWindowOrder), "所选窗口已加入当前任务");
  }

  async function controlAllTasks(action: "pause" | "resume") {
    const tasks = action === "pause" ? runningTasks : resumableTasks;
    if (!tasks.length) return;
    if (action === "pause") {
      await collectionControls.run("collection-pause-all",
        tasks.map((task) => ({ scope: "task", taskId: task.id, action: "pause" })),
        "运行中的任务已全部暂停");
    } else {
      await run("collection-resume-all", async (client) => {
        for (const task of tasks) await client.controlCollectionTask(task.id, "resume");
      }, "可继续的任务已全部恢复");
    }
  }

  async function createTask(event?: FormEvent) {
    event?.preventDefault();
    const modes = ([followers ? "followers" as const : null, following ? "following" as const : null].filter((value): value is "followers" | "following" => Boolean(value)));
    if (createTaskRef.current || !claimableWaitingCount || !selectedWindowOrder.length || !modes.length || !filtersValid) return;
    const taskPlatform = platform;
    // Revalidate selected IDs against the latest lock feed before dispatch.
    const windowIds = selectedWindowOrder.filter(id => collectionWindows.some(item => item.id === id && !item.locked));
    if (!windowIds.length) return;
    createTaskRef.current = true;
    try {
      const ok = await run("collection-create", async (client) => {
        const created = await client.createCollectionTask({
          platform: taskPlatform,
          targets: [],
          window_ids: windowIds,
          modes,
          source_limits: {},
          assignment_mode: "sequential",
          dedupe: true,
          local_person_recognition: false,
          exclude_male_avatar: false,
          auto_classify: autoClassify,
          read_location: true,
          gpt_review: openAiReview,
          exclude_verified: excludeVerified,
          exclude_public_zero_posts: excludePublicZeroPosts,
          ...(discardCountLimitsPayload(discardLimits)),
          parallel_screening_workers: parallelScreeningWorkers,
          allow_completed_targets: false,
        });
        await client.controlCollectionTask(created.task_id, "start");
      }, "采集任务已创建并启动");
    } finally { createTaskRef.current = false; }
  }

  return (
    <>
      <div className="formal-stats">
        <StatCard icon={<Database size={22} />} label="总采集数量" value={snapshot.counts.total_collected || 0} />
        <StatCard icon={<Eye size={22} />} label="总公开数量" value={snapshot.counts.total_public || 0} />
        <StatCard icon={<LockKeyhole size={22} />} label="总私密数量" value={snapshot.counts.total_private || 0} />
        <StatCard icon={<Archive size={22} />} label="总分裂数量" value={snapshot.counts.total_split || 0} />
      </div>
      <Panel className="collection-settings-panel" icon={<ListFilter size={19} />} title="采集设置" actions={
        (<label className="formal-check collection-discard-toggle"><input type="checkbox" checked={discardLimits.enabled} onChange={(event) => setDiscardLimits(current => ({ ...current, enabled: event.target.checked }))} />启用直接丢弃</label>)
      }>
        <div className="formal-panel-body collection-settings-body">
          <div className="collection-source-settings">
            <div className="collection-condition-block">
              {(<div><strong>采集列表</strong><div className="formal-toolbar collection-mode-row"><label className="formal-check"><input type="checkbox" checked={followers} onChange={(event) => setFollowers(event.target.checked)} />粉丝</label><label className="formal-check"><input type="checkbox" checked={following} onChange={(event) => setFollowing(event.target.checked)} />关注</label></div></div>)}
            </div>
            {(<div className="collection-parallel-setting">
              <div className="collection-parallel-copy">
                <strong>推荐二档</strong>
              </div>
              <div className="collection-parallel-options" role="radiogroup" aria-label="单窗口并行档位">
                {([1, 2, 3] as ParallelScreeningWorkers[]).map((workers) => (
                  <label className={`collection-parallel-option ${parallelScreeningWorkers === workers ? "is-selected" : ""}`} key={workers}>
                    <input type="radio" name="parallel-screening-workers" value={workers} checked={parallelScreeningWorkers === workers} onChange={() => setParallelScreeningWorkers(workers)} />
                    <strong>1-{workers}</strong>
                  </label>
                ))}
              </div>

            </div>)}

          </div>

          {(<section className="collection-discard-body" aria-labelledby="collection-discard-title">
            <div className="collection-discard-heading"><h3 id="collection-discard-title"><Trash2 size={16} />直接丢弃规则</h3><button className="formal-button compact" type="button" onClick={setAllDiscardLimitsUnlimited} disabled={!discardLimits.enabled}><RotateCcw size={15} />全部不限</button></div>


            <div className="collection-discard-groups">
              {(["private", "public"] as const).map(visibility => (
                <fieldset className={`collection-discard-group is-${visibility}`} key={visibility} disabled={!discardLimits.enabled}>
                  <legend>{visibility === "private" ? "私密账号" : "公开账号"}</legend>
                  <div className="collection-discard-fields">
                    {([ ["followers", "粉丝"], ["following", "关注"], ["posts", "帖子"] ] as const).map(([metric, label]) => {
                      const key: DiscardLimitKey = `${visibility}_discard_${metric}_max`;
                      const invalid = discardLimits.enabled && parseDiscardLimit(discardLimits.limits[key]) === null;
                      return <label className="formal-field" key={key}><span>{label}数量上限</span><input className="formal-input" type="number" min="0" step="1" max={Number.MAX_SAFE_INTEGER} inputMode="numeric" value={discardLimits.limits[key]} onChange={(event) => setDiscardLimits(current => ({ ...current, limits: { ...current.limits, [key]: event.target.value } }))} aria-label={`${visibility === "private" ? "私密" : "公开"}账号直接丢弃${label}数量上限`} aria-invalid={invalid || undefined} aria-describedby={invalid ? "collection-discard-error" : undefined} /></label>;
                    })}
                  {visibility === "public" ? <>
                    <label className="formal-field"><span>活跃度上限</span><div className="collection-discard-days"><input className="formal-input" type="number" min="0" step="1" max={Number.MAX_SAFE_INTEGER} inputMode="numeric" value={discardLimits.limits.public_discard_active_days_max} onChange={(event) => setDiscardLimits(current => ({ ...current, limits: { ...current.limits, public_discard_active_days_max: event.target.value } }))} aria-label="公开帖子活跃度上限（天）" aria-invalid={(discardLimits.enabled && parseDiscardLimit(discardLimits.limits.public_discard_active_days_max) === null) || undefined} aria-describedby={discardLimitsError ? "collection-discard-error" : undefined} /><span>天</span></div></label>

                  </> : null}
                  </div>
                </fieldset>
              ))}
            </div>
            {discardLimitsError ? <div className="formal-error-banner" id="collection-discard-error" role="alert">{discardLimitsError}</div> : null}
          </section>)}
        </div>
      </Panel>

      <div className="collection-workbench-grid">
        <Panel className="collection-column collection-target-column" icon={<UserPlus size={19} />} title={("分裂号等待列表")} >
          <div className="formal-panel-body">
            {<CollectionClaimLockControl locked={splitClaimLocked} run={run} disabled={disabled} />}
            <label className="formal-field"><textarea aria-label={("目标账号 / 主页链接")} className="formal-textarea collection-target-input" value={targetDraft} onChange={(event) => updateTargetDraft(event.target.value)} placeholder={("输入 Instagram 账号或个人主页链接")} aria-invalid={!draftSeeds.valid && Boolean(targetDraft.trim())} /></label>
            {<>
            <div className="collection-add-target-actions">
              <button className="formal-button primary collection-add-targets-button" type="button" disabled={!targetDraft.trim() || disabled("collection-check-completed-targets")} onClick={() => void addDraftTargets()}><Plus size={17} />自动加入</button>
              <button className="formal-button collection-add-targets-button" type="button" disabled={!targetDraft.trim() || disabled("collection-check-completed-targets")} onClick={openDraftWindowAssignment}><Monitor size={17} />指定窗口后加入</button>
            </div>
            <div className="collection-queue-summary"><strong>{targets.length}</strong><span>个目标等待分配 · 单独锁定 {individuallyLockedCount} 个{splitClaimLocked ? " · 全部暂停领取" : ` · 可领取 ${claimableWaitingCount} 个`}</span></div>
            </>}
          </div>
          {<div className="formal-panel-body formal-scroll collection-column-scroll">
            <div className="formal-list">{waitingCandidates.map((candidate, index) => <WaitingSplitTargetRow key={candidate.id} candidate={candidate} index={index} tasks={snapshot.tasks} globallyLocked={splitClaimLocked} run={run} disabled={disabled} onAssign={openWaitingWindowAssignment} onDelete={removeWaitingTarget} />)}{!waitingCandidates.length ? <EmptyState title="等待列表暂无目标" /> : null}</div>
          </div>}
        </Panel>

        <div className="collection-middle-column">
          <WindowSelector windows={collectionWindows} selected={selectedWindows} setSelected={setSelectedWindows} setSelectedOrder={setSelectedWindowOrder} run={run} disabled={disabled} title={("窗口自动领取")} />
        </div>

        <div className="collection-execution-column"><CollectionTaskList snapshot={snapshot} run={run} disabled={disabled} collectionControls={collectionControls} onDeleteTask={async (task) => {
          await run(`collection-task-${task.id}-delete`, (client) => client.deleteCollectionTask(task.id), "任务已删除，采集结果和去重记录已保留");
        }} /></div>
      </div>

      <CollectionFixedFooter>
        <div className="collection-control-copy"><span className="formal-live" /> <div><strong>任务调度 · 已选 {selectedWindows.size} 窗口</strong>{collectionControls.snapshotStale || splitClaimLocked ? <small>{collectionControls.snapshotStale ? "列表更新失败" : "领取已锁定"}</small> : null}</div></div>
        {(<div className="collection-filter-options">
          <div className="formal-option-grid collection-compact-options"><label className="formal-check"><input type="checkbox" checked={autoClassify} onChange={(event) => setAutoClassify(event.target.checked)} />自动分类</label><label className="formal-check"><input type="checkbox" checked={openAiReview} onChange={(event) => setOpenAiReview(event.target.checked)} />复审</label><label className="formal-check"><input type="checkbox" checked={excludeVerified} onChange={(event) => setExcludeVerified(event.target.checked)} />排除蓝V</label><label className="formal-check"><input type="checkbox" checked={excludePublicZeroPosts} onChange={(event) => setExcludePublicZeroPosts(event.target.checked)} />排除0帖（公开/私密）</label></div>

        </div>)}
        <div className="collection-control-actions">
          <button className="formal-button warning" type="button" onClick={() => void controlAllTasks("pause")} disabled={!runningTasks.length || collectionControls.feedback("collection-pause-all").safetyControlDisabled}><Pause size={19} />全部暂停</button>
          <button className="formal-button" type="button" onClick={() => void addSelectedWindows()} disabled={!acceptingTask || !selectedWindowOrder.length || disabled("collection-add-windows")}><Plus size={19} />窗口加入</button>
          <button className="formal-button success" type="button" onClick={() => void controlAllTasks("resume")} disabled={!resumableTasks.length || disabled("collection-resume-all")}><Play size={19} />全部继续</button>
          <button className="formal-button primary collection-start-button" type="button" onClick={() => void createTask()} disabled={!claimableWaitingCount || !selectedWindowOrder.length || ((!followers) && !following) || !filtersValid || disabled("collection-create")} title={!claimableWaitingCount ? "请先解锁领取或至少解锁一个等待目标；当前任务仍可继续" : undefined}>{collectionControls.feedback("collection-create").pending ? <LoaderCircle className="animate-spin" size={19} /> : <Play size={19} />}{collectionControls.feedback("collection-create").pending ? "正在启动" : collectionControls.feedback("collection-create").refreshing ? "更新列表中" : "开始采集"}</button>
        </div>
      </CollectionFixedFooter>
      {assignmentEditor ? <SplitWindowAssignmentDialog key={`${assignmentEditor.candidateId || "new"}-${assignmentEditor.usernames.join(",")}`} editor={assignmentEditor} windows={snapshot.windows} tasks={snapshot.tasks} saving={disabled()} onClose={() => setAssignmentEditor(null)} onSave={saveWindowAssignment} /> : null}
    </>
  );
}

function CollectionClaimLockControl({ locked, run, disabled }: { locked: boolean; run: WorkspaceProps["run"]; disabled: WorkspaceProps["disabled"] }) {
  const key = "collection-claim-lock";
  return <div className={`collection-claim-lock ${locked ? "is-locked" : ""}`}>
    <div className="collection-claim-lock-heading"><strong>{locked ? "新目标领取已锁定" : "新目标可自动领取"}</strong><button className={`formal-button compact ${locked ? "success" : "warning"}`} type="button" disabled={disabled(key)} aria-pressed={locked} onClick={() => void run(key, client => client.setSplitClaimLocked(!locked), locked ? "已解锁新目标领取；单独锁定的目标仍不会被领取" : "已锁定新目标领取；当前目标继续完成，新增和退回目标保持等待")}>{locked ? <LockKeyholeOpen size={15} /> : <LockKeyhole size={15} />}{locked ? "解锁领取" : "锁定领取"}</button></div>

  </div>;
}

function WaitingSplitTargetRow({ candidate, index, tasks, globallyLocked, run, disabled, onAssign, onDelete }: {
  candidate: CoreSplitCandidate; index: number; tasks: CoreCollectionTask[]; globallyLocked: boolean;
  run: WorkspaceProps["run"]; disabled: WorkspaceProps["disabled"];
  onAssign: (candidate: CoreSplitCandidate) => void; onDelete: (candidateId: string) => Promise<void>;
}) {
  const allowedWindowIds = Array.isArray(candidate.allowed_window_ids) ? candidate.allowed_window_ids : [];
  const waitingForSpecifiedWindow = Boolean(allowedWindowIds.length && !assignmentHasJoinedWindow(tasks, allowedWindowIds, candidate));
  const locked = candidate.locked === true;
  const key = `collection-waiting-${candidate.id}`;
  const assignmentText = allowedWindowIds.length ? `指定 ${allowedWindowIds.length} 个窗口${waitingForSpecifiedWindow ? " · 等待窗口加入任务" : "领取"}` : "自动分配 · 等待窗口领取";
  return <div className={`formal-row collection-waiting-row ${locked || globallyLocked ? "is-claim-locked" : ""}`}>
    <span className="collection-order">{String(index + 1).padStart(2, "0")}</span>
    <div className="formal-row-main"><strong>@{candidate.username}</strong><small className={locked || globallyLocked || waitingForSpecifiedWindow ? "collection-affinity-warning" : ""}>{locked ? "已单独锁定 · 不会被领取" : globallyLocked ? "全局领取已锁定 · 保持等待" : assignmentText}</small>{locked || globallyLocked ? <small>{assignmentText}</small> : null}</div>
    <div className="formal-row-actions">
      <button className={`formal-button compact ${locked ? "warning" : ""}`} type="button" disabled={disabled(key)} aria-pressed={locked} aria-label={`${locked ? "解锁" : "锁定"} ${candidate.username}`} title={locked ? "解除此目标的单独锁定；全局领取仍锁定时继续等待" : "锁定此目标，自动和指定窗口均不可领取；可继续编辑指定窗口或删除"} onClick={() => void run(key, client => client.setWaitingSplitTargetLocked(candidate.id, !locked), locked ? "目标已解锁；全局领取锁定期间仍会等待" : "目标已锁定，任何窗口都不会领取")}>{locked ? <LockKeyholeOpen size={14} /> : <LockKeyhole size={14} />}{locked ? "解锁" : "锁定"}</button>
      <button className={`formal-button compact collection-assignment-trigger ${allowedWindowIds.length ? "is-specified" : ""}`} type="button" disabled={disabled(key)} onClick={() => onAssign(candidate)}><Monitor size={14} />{allowedWindowIds.length ? `指定 ${allowedWindowIds.length}` : "自动"}</button>
      <button className="formal-button compact danger collection-waiting-delete" type="button" disabled={disabled(key)} title={`删除 @${candidate.username}`} aria-label={`删除 ${candidate.username}`} onClick={() => void onDelete(candidate.id)}><Trash2 size={15} /></button>
    </div>
  </div>;
}

function CollectionTaskList({ snapshot, run, disabled, onDeleteTask, collectionControls }: WorkspaceProps & { collectionControls: CollectionControls; onDeleteTask: (task: CoreCollectionTask) => Promise<void> }) {
  const sorted = [...snapshot.tasks].sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")) || a.id.localeCompare(b.id));
  const { rows: executionRows, orphanFailures: failedTargets } = collectionFailurePresentation(sorted.flatMap(collectionTaskRows), snapshot.split_candidates);
  // Completed sources disappear immediately without touching their window lease.
  // Only a real cleanup failure retains a recovery control; never backfill history.
  const completionCleanup = collectionCompletionCleanupRows(sorted).filter(({task, target}) =>
    !executionRows.some(row => row.task.id === task.id && row.profileId === target.current_window_id));
  const orderedWindows = new Map(sortBitBrowserWindows(snapshot.windows).map((window, index) => [window.id, index]));
  executionRows.sort((a, b) => (orderedWindows.get(a.profileId) ?? Number.MAX_SAFE_INTEGER) - (orderedWindows.get(b.profileId) ?? Number.MAX_SAFE_INTEGER) || a.task.id.localeCompare(b.task.id));
  const windowNames = new Map(snapshot.windows.map((window) => [window.id, window.name]));
  const sourcesById = new Map(snapshot.sources.map((source) => [source.id, source]));
  return (
    <Panel icon={<ListFilter size={19} />} title="采集任务列表" >
      <div className="formal-panel-body formal-scroll">
        <div className="formal-list">
          {executionRows.map(({ task, target, profileId, failure }) =>
            <CollectionTaskRow key={`${task.id}-${profileId}`} task={task} target={target} linkedFailure={failure} profileIdOverride={profileId} windowNames={windowNames} run={run} disabled={disabled} onDeleteTask={onDeleteTask} collectionControls={collectionControls} />
          )}
          {completionCleanup.map(({task, target}) => <CompletedCollectionTargetCard key={`cleanup-${task.id}-${target.id}`} task={task} target={target} run={run} disabled={disabled} />)}
          {failedTargets.map((candidate) => {
            const key = `collection-failure-${candidate.id}`;
            const progressSource = sourcesById.get(String(candidate.source_target_id || ""));
            const progressModes = Array.isArray(progressSource?.modes) ? progressSource.modes : [];
            const modeProgress = asRecord(progressSource?.mode_progress);
            return (
              <div className="formal-row collection-task-row has-runtime-warning" key={`failure-${candidate.id}`}>
                <span className="formal-stat-icon"><AlertTriangle size={20} /></span>
                <div className="formal-row-main">
                  <strong><span className={`collection-platform-badge is-${collectionPlatform(candidate)}`}>{("IG")}</span> 采集失败 · @{collectionProfileLabel(candidate)}</strong>
                  <small>{candidate.source_window_id ? `窗口：${windowNames.get(String(candidate.source_window_id)) || candidate.source_window_id}` : "窗口已释放"} · 失败于 {formatTime(candidate.updated_at)}</small>
                  <div className="collection-task-progress-list">
                    <div className={`collection-task-progress ${progressModes.length ? "" : "is-unavailable"}`}>
                      <CollectionProgressValues modes={progressModes} progress={modeProgress} coverage={asRecord(progressSource?.mode_coverage)} unavailableText="原任务进度记录不可用" />
                    </div>
                  </div>
                  {candidate.last_error ? <small className="collection-runtime-warning"><AlertTriangle size={14} />{String(candidate.last_error)}</small> : null}

                </div>
                <StatusBadge status="failed" />
                <div className="formal-row-actions">
                  {collectionPlatform(candidate) === "instagram" && <button className="formal-button compact success" disabled={disabled(`${key}-requeue`)} onClick={() => void run(`${key}-requeue`, (client) => client.requeueFailedSplitTarget(candidate.id), "分裂号已返回等待列表，已采集部分将按全局去重跳过")}><RotateCcw size={15} />返回等待</button>}
                  <button className="formal-button compact danger" disabled={disabled(`${key}-delete`)} onClick={() => void run(`${key}-delete`, (client) => client.deleteFailedSplitTarget(candidate.id), "失败记录已删除，已采集数据和全局去重记录已保留")}><Trash2 size={15} />删除失败</button>
                </div>
              </div>
            );
          })}
          {!executionRows.length && !failedTargets.length && !completionCleanup.length ? <EmptyState title="暂无采集任务" /> : null}
        </div>
      </div>
    </Panel>
  );
}

function CompletedCollectionTargetCard({task, target, run, disabled}: {
  task: CoreCollectionTask; target: CoreTaskTarget; run: WorkspaceProps["run"]; disabled: WorkspaceProps["disabled"];
}) {
  const {platform} = useWorkbenchPlatform();
  const [confirming, setConfirming] = useState(false);
  const deleting = useRef(false);
  const actionKey = `source-recheck-${task.id}-${target.id}`;
  const rechecked = collectionRecheckCompleted(target);
  const automaticCompletion = target.completion_policy === "automatic";
  const cleanupProfile = collectionCompletionCleanupProfile(task, target);
  const cleanupWindowId = firstText(cleanupProfile || {}, ["profile_id", "window_id"]);
  const cleanupFailed = automaticCompletion && Boolean(cleanupProfile);
  const driverCleanupFailed = cleanupProfile?.reason === "source_recheck_cleanup_failed";
  const name = String(target.username_display || target.username || "未知目标");
  const eligible = !automaticCompletion && target.status === "completed" && !target.collection_list_dismissed && collectionPlatform(task) === platform;
  useEffect(() => { setConfirming(false); }, [platform, task.id, target.id, target.status]);
  useEffect(() => {
    if (!confirming) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape" && !deleting.current) setConfirming(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirming]);
  async function dismiss() {
    if (!confirming || !eligible || deleting.current || disabled(actionKey)) return;
    deleting.current = true;
    try {
      const ok = await run(actionKey, client => client.controlCollectionTarget(task.id, target.id, "dismiss_completed"),
        "已删除这张完成任务卡；采集结果、实际数量、历史记录和全局去重均已保留");
      if (ok) setConfirming(false);
    } finally { deleting.current = false; }
  }
  return <>
    <div className={`formal-row collection-task-row ${!cleanupFailed && (rechecked || automaticCompletion) ? "" : "has-runtime-warning"}`} data-completed-target-id={target.id}>
      <span className="formal-stat-icon">{rechecked || automaticCompletion ? <Check size={20} /> : <Users size={20} />}</span>
      <div className="formal-row-main">
        <strong>@{name} · {cleanupFailed ? "采集已完成，窗口清理待处理" : automaticCompletion ? "采集已完成，等待窗口释放" : rechecked ? "复查已完成" : "可见名单已处理，仍有未发现差额"}</strong>
        <div className="collection-task-progress-list"><div className="collection-task-progress">
          <CollectionProgressValues modes={task.modes || []} progress={asRecord(target.mode_progress)} coverage={asRecord(target.mode_coverage)} />
        </div></div>
        <small>{cleanupFailed ? "窗口尚未确认关闭；请重试清理，成功前保留此卡和窗口占用，已采集数据不受影响" : automaticCompletion ? "窗口任务结束并确认清理释放后自动移除任务卡；采集结果、实际差额和去重记录保留" : rechecked ? "本次采集复查已完成，未发现差额如实保留；窗口清理完成后可删除此任务卡" : "可主动复查可见名单，也可删除此任务卡；已采集记录和去重保留"}</small>
      </div>
      <StatusBadge status="completed" />
      {cleanupFailed && cleanupWindowId && <button type="button" className="formal-button compact" disabled={disabled(actionKey)}
        onClick={() => void run(actionKey, client => client.controlCollectionWindow(task.id, cleanupWindowId, driverCleanupFailed ? "stop" : "resume", target.id), "已请求重试清理窗口；清理与释放确认后自动移除任务卡")}><RefreshCw size={15} />{driverCleanupFailed ? "重试清理" : "重试关闭"}</button>}
      {!automaticCompletion && <div className="formal-row-actions">
        <CollectionSourceRecheck task={task} target={target} state="completed" actionKey={actionKey} run={run} disabled={disabled} />
        <button type="button" className="formal-button compact danger" disabled={!eligible || disabled(actionKey)} onClick={() => setConfirming(true)}><Trash2 size={15} />删除任务卡</button>
      </div>}
    </div>
    {confirming && eligible ? <div className="formal-modal-backdrop" onMouseDown={event => {if (event.target === event.currentTarget && !deleting.current) setConfirming(false);}}>
      <section className="formal-modal" role="dialog" aria-modal="true" aria-labelledby={`dismiss-completed-${target.id}`}>
        <div className="formal-modal-header"><div><h2 id={`dismiss-completed-${target.id}`}>删除 @{name} 的任务卡？</h2><p>只从采集任务列表移除这一张已完成卡片，不退回等待，也不删除整个任务。</p></div></div>
        <div className="formal-modal-body"><p>已采集账号、实际数量与未发现差额、历史记录、检查点和全局去重全部保留。其他任务与窗口不受影响。若窗口尚未清理完成，将保留此卡并提示重试。</p></div>
        <div className="formal-modal-footer"><button type="button" className="formal-button" disabled={disabled(actionKey)} onClick={() => setConfirming(false)}>取消</button><button type="button" className="formal-button danger" disabled={disabled(actionKey)} onClick={() => void dismiss()}>确认删除任务卡</button></div>
      </section>
    </div> : null}
  </>;
}

function CollectionProgressValues({ modes, progress, coverage = {}, unavailableText = "采集进度记录不可用" }: { modes: readonly string[]; progress: Record<string, unknown>; coverage?: Record<string, unknown>; unavailableText?: string }) {
  if (!modes.length) return <span className="collection-task-progress-unavailable">{unavailableText}</span>;
  return <>{modes.map((mode) => {
    const evidence = asRecord(coverage[mode]) as CollectionCoverage;
    const displayMode = mode;
    const detail = evidence.discovery_finished ? collectionCoverageDisplay(displayMode, evidence) : null;
    return <span key={mode} className="collection-mode-progress"
      aria-label={collectionModeProgressText(displayMode, asRecord(progress[mode]))}
     >
      {collectionModeProgressItems(displayMode, asRecord(progress[mode])).map(item =>
        <span key={item.key} className={`collection-progress-value is-${item.key}`} title={item.title} aria-label={item.title ? `${item.text}：${item.title}` : item.text}>
          {item.label ? <><span className="collection-progress-label">{item.label}</span><span className="collection-progress-number">{item.value}</span></> : item.text}
        </span>)}
      {detail && <span className={`collection-coverage-note ${detail.warning ? "is-warning" : ""}`} title={detail.reason}>{detail.title} · {detail.reason}</span>}
    </span>;
  })}</>;
}

function CollectionSourceRecheck({task, target, state, actionKey, run, disabled}: {
  task: CoreCollectionTask; target: CoreTaskTarget | null; state: string; actionKey: string;
  run: WorkspaceProps["run"]; disabled: WorkspaceProps["disabled"];
}) {
  if (collectionPlatform(task) !== "instagram" || !target) return null;
  const liveProfile = (Array.isArray(task.runtime?.profile_states) ? task.runtime.profile_states : []).map(asRecord).find(profile => profile.current_target_id === target.id);
  const liveMode = liveProfile?.source_recheck_mode;
  const pendingMode = target.source_recheck?.state === "prepared" ? target.source_recheck.mode : null;
  const liveModes = liveMode === "followers" || liveMode === "following" ? [liveMode] as const : [];
  const modes = [...liveModes, ...collectionSourceRecheckModes(target), ...(pendingMode ? [pendingMode] : [])].filter((mode, index, all) => all.indexOf(mode) === index);
  const pending = pendingMode !== null;
  const blocked = collectionRecheckNeedsPause(state) || pending;
  return <>{modes.map(mode => <span key={mode} className="collection-source-recheck">
    <button type="button" className="formal-button compact"
      disabled={blocked || disabled(actionKey)}
      title={pending ? "请求已保存，等待父页完成当前操作后复查；子页继续采集" : blocked ? "窗口正在恢复或清理，请等待安全状态后复查" : "父页独立复查可见名单，子页继续采集；保留全部记录和去重"}
      onClick={() => void run(actionKey, client => client.recheckCollectionSource(task.id, target.id, mode), result =>
        asRecord(result).waiting_for_task_resume ? "复查已安排，原任务仍暂停；点击继续后复查，已有记录保留" : asRecord(result).waiting_for_safe_point ? "请求已保存，等待父页完成当前操作后复查；子页继续采集" : asRecord(result).parent_only ? "父页复查已安排，子页继续采集，已有记录和去重保留" : "已请求复查可见名单，已有记录和去重保留")}
    ><RefreshCw size={15} />{pending ? "等待父页完成当前操作后复查" : `复查未发现（${mode === "followers" ? "粉丝" : "关注"}）`}</button>
    <small className="collection-source-recheck-gap" title="主页总数减已识别的不同账号数；不包含已识别后的待处理队列">差额：{collectionModeUnseenCount(asRecord(target.mode_progress?.[mode])) ?? "未知"}</small>
  </span>)}</>;

}

function CollectionTaskRow({ task, target, linkedFailure, profileIdOverride, windowNames, run, disabled, onDeleteTask, collectionControls }: { collectionControls: CollectionControls; task: CoreCollectionTask; target: Record<string, any> | null; linkedFailure: CoreSplitCandidate | null; profileIdOverride: string; windowNames: Map<string, string>; run: WorkspaceProps["run"]; disabled: WorkspaceProps["disabled"]; onDeleteTask: (task: CoreCollectionTask) => Promise<void> }) {
  const runtime = asRecord(task.runtime);
  const profileStates = Array.isArray(runtime.profile_states) ? runtime.profile_states.map(asRecord) : [];
  const targetId = String(target?.id || "");
  const targetName = String(target?.username_display || target?.username || "未知目标").replace(/^@/, "");
  const explicitWindowId = profileIdOverride || String(target?.current_window_id || target?.preferred_window_id || "");
  const windowProfile = explicitWindowId
    ? profileStates.find((item) => firstText(item, ["profile_id", "window_id"]) === explicitWindowId)
    : profileStates.find((item) => targetId && firstText(item, ["current_target_id"]) === targetId);
  const activeProfile = targetId && windowProfile?.current_target_id && windowProfile.current_target_id !== targetId
    ? undefined : windowProfile;
  const profileId = activeProfile ? firstText(activeProfile, ["profile_id", "window_id"], explicitWindowId) : explicitWindowId;
  // Every mutation for one execution window shares one stable key.  This keeps
  // stop/delete/resume from racing each other even when the target or status is
  // replaced by the authoritative snapshot while a command is still in flight.
  const rowActionKey = `collection-window-${task.id}-${profileId || profileIdOverride || targetId || "empty"}`;
  const profileState = activeProfile ? normalizeStatus(firstText(activeProfile, ["state", "status"], "")) : "";
  const state = collectionRowStatus(target?.status, profileState, task.status);
  const assignedWindowCount = profileId ? 1 : 0;
  const recoverableWithoutWindow = (RESUMABLE_STATES.has(state) || FAILED_STATES.has(state)) && !profileId;
  const currentStage = activeProfile ? firstText(activeProfile, ["current_stage", "state", "step"]) : "";
  const currentStageText = currentStage === "disconnecting" ? "正在清理采集页面" : currentStage;
  const windowProgress = collectionWindowProgress(activeProfile, target, state);
  // Failed source cleanup owns a retained live driver even if cancellation has
  // already persisted recoverable/stopped on the target. Its current runtime
  // cleanup gate, not that older durable status, controls the safe Stop action.
  const retainedSourceCleanup = profileState === "manual_required" && activeProfile?.reason === "source_recheck_cleanup_failed";
  const recovery = collectionRecoveryDetails(activeProfile, retainedSourceCleanup ? profileState : state);
  const closingFailed = recovery.manualRequired && activeProfile?.reason === "browser_close_failed";
  const sourceCleanupFailed = recovery.manualRequired && activeProfile?.reason === "source_recheck_cleanup_failed";
  const displayState = retainedSourceCleanup ? "manual_required" : recovery.verifyingRecovery ? "recovering_page" : state;
  const stalled = windowProgress.stalled && !recovery.automatic && !recovery.manualRequired;
  const recoveryError = !recovery.automatic && (recovery.manualRequired || stalled || FAILED_STATES.has(state) || state === "recoverable")
    ? String(activeProfile?.message || target?.last_error
      || (!target && !activeProfile ? task.last_error : "") || "").trim() : "";
  const recoveryAction = sourceCleanupFailed ? null : closingFailed
    ? { action: "resume" as const, label: "重试关闭", message: "已请求重试关闭窗口；关闭确认前保留窗口占用" }
    : collectionRecoveryAction(state,
      { stalled, authRequired: false, manualRequired: recovery.manualRequired, pageRecoveryExhausted: false });
  const modes = Array.isArray(task.modes) ? task.modes : [];
  const progress = asRecord(target?.mode_progress);
  const canStopWindow = Boolean(profileId) && (ACTIVE_STATES.has(state) || PAUSED_STATES.has(state) || state === "waiting_network" || recovery.manualRequired);
  const safetyControlDisabled = targetId
    ? collectionControls.feedback(rowActionKey).safetyControlDisabled : disabled(rowActionKey);
  const controlWindow = (action: "pause" | "resume" | "stop" | "restart", message: string) => {
    if ((action === "pause" || action === "stop") && targetId) {
      return collectionControls.run(rowActionKey,
        [{ scope: "window", taskId: task.id, profileId, targetId, action }], message);
    }
    return run(rowActionKey, (client) => client.controlCollectionWindow(task.id, profileId, action, targetId || undefined), message);
  };
  const canReturnToWaiting = Boolean(target) && !SUCCESS_STATES.has(state);
  const deleteWindowTask = async (action: "delete" | "delete_only") => {
    if (!profileId) return;
    await run(rowActionKey, (client) => client.controlCollectionWindow(task.id, profileId, action, targetId || undefined), SUCCESS_STATES.has(state) ? "完成记录已从任务列表移除，采集结果和去重历史已保留" : target ? action === "delete_only" ? "该窗口任务已直接删除，不退回等待；已采集结果和去重记录已保留" : "该窗口任务已停止，目标已退回等待；已采集结果和去重记录已保留" : "该等待窗口已移除");
  };
  return (
    <div className={`formal-row collection-task-row ${recovery.manualRequired || stalled || linkedFailure ? "has-runtime-warning" : ""}`}>
      <span className="formal-stat-icon"><Users size={20} /></span>
      <div className="formal-row-main">
        <strong><span className={`collection-platform-badge is-${("instagram")}`}>{("IG")}</span> {profileId ? windowNames.get(profileId) || "未知窗口" : "等待窗口分配"}</strong>
        <small>{target ? `@${targetName}` : closingFailed ? "采集已完成，等待关闭窗口" : profileId ? "等待领取下一个分裂号" : "当前没有领取分裂号"} · 更新于 {formatTime(task.updated_at || task.created_at)}</small>
        <div className="collection-task-progress-list">
          <div className="collection-task-progress">
            <CollectionProgressValues modes={modes} progress={progress} coverage={asRecord(target?.mode_coverage)} />
            <StatusBadge status={displayState} />
          </div>
        </div>
        {recoveryError && recoveryError !== String(linkedFailure?.last_error || "").trim() ? <small className="collection-runtime-warning"><AlertTriangle size={14} />{recoveryError}</small> : null}
        {linkedFailure ? <small className="collection-runtime-warning"><AlertTriangle size={14} />失败记录待处理 · {String(linkedFailure.last_error || "可继续本任务，或将失败项返回等待")}</small> : null}
        {recoverableWithoutWindow ? <small className="collection-runtime-warning"><Monitor size={14} />任务没有执行窗口：请先勾选窗口，点击“窗口加入”，再继续</small> : null}
        {target ? <small className="collection-runtime-detail">当前：{closingFailed ? "等待关闭窗口" : recovery.automatic ? "等待自动续采" : recovery.verifyingRecovery ? "正在验证恢复" : recovery.manualRequired ? "等待处理页面提示" : currentStageText || "等待下一步"} · {windowProgress.lastProgressAt ? `本窗口最近推进 ${formatTime(windowProgress.lastProgressAt)}` : "等待本窗口进度记录"}</small> : null}
        {activeProfile?.parent_activity ? <small className="collection-runtime-detail">父页：{{collecting: "正在读取名单", waiting_recovery: "名单读取等待恢复，子页继续处理", waiting_safe_point: "等待父页完成当前操作后复查", rechecking: "正在复查名单", browsing_reels: "正在刷视频", idle: "等待下一步或视频暂不可用"}[String(activeProfile.parent_activity)] ?? "等待下一步"}</small> : null}
        <small className="collection-runtime-detail">{(<>单窗口并行档位：1-{firstNumber(asRecord(task.settings), ["parallel_screening_workers"]) || 1} · R59 分批读取 · 子页并行采集</>)}</small>
        {recovery.automatic ? <small className="collection-runtime-detail">{recovery.pageRecovery ? <RefreshCw size={14} /> : <WifiOff size={14} />}{recovery.reasonLabel}；{recovery.nextRetryAt ? `将于 ${formatTime(recovery.nextRetryAt)} 自动续采，无需点击继续` : "已保留检查点，正在安排自动恢复"}</small> : null}
        {recovery.verifyingRecovery ? <small className="collection-runtime-detail"><RefreshCw size={14} />{recovery.reasonLabel}；正在从检查点恢复，尚未确认新的采集进展</small> : null}
        {recovery.candidateUsername ? <small className="collection-runtime-detail">待补查账号：@{recovery.candidateUsername}{recovery.sourceDiscoveryComplete ? " · 名单已读取完成，未完成候选将继续补查" : " · 已保留候选与采集进度"}</small> : null}
        {recovery.manualRequired ? <small className="collection-runtime-warning"><AlertTriangle size={14} />{recovery.manualMessage}</small> : null}
        {stalled ? <small className="collection-runtime-warning"><AlertTriangle size={14} />{recovery.verifyingRecovery ? "本窗口超过 3 分钟没有确认新的采集进展，恢复尚未完成；可检查页面或使用检查点重启" : "本窗口超过 3 分钟没有新进度，可能页面卡住或元素未加载"}</small> : null}
      </div>
      <StatusBadge status={displayState} />
      <div className="formal-row-actions">
        <CollectionSourceRecheck task={task} target={target as CoreTaskTarget | null} state={retainedSourceCleanup ? "manual_required" : state} actionKey={rowActionKey} run={run} disabled={disabled} />
        {profileId && recoveryAction ? <button className="formal-button compact" disabled={disabled(rowActionKey)} title={closingFailed ? "仅重试关闭窗口；关闭确认前保留窗口占用，不会开始新的采集" : linkedFailure ? "从原任务检查点继续；处理后失败记录会自动消失" : undefined} onClick={() => void controlWindow(recoveryAction.action, recoveryAction.message)}>{recoveryAction.action === "restart" ? <RotateCcw size={15} /> : recovery.automatic && !recovery.pageRecovery ? <Wifi size={15} /> : <RefreshCw size={15} />}{linkedFailure && recoveryAction.label === "继续" ? "继续本任务" : recoveryAction.label}</button> : null}
        {profileId && (ACTIVE_STATES.has(state) || recovery.automatic) ? <button className="formal-button compact warning" disabled={safetyControlDisabled} onClick={() => void controlWindow("pause", "该窗口任务已暂停；其他窗口继续采集")}><Pause size={15} />暂停</button> : null}
        {canStopWindow ? <button className="formal-button compact danger" disabled={safetyControlDisabled} onClick={() => void controlWindow("stop", sourceCleanupFailed ? "页面清理已完成，原进度保留；可再次请求复查" : "该窗口任务已停止；现在可以直接删除")}><Square size={15} />{sourceCleanupFailed ? "重试清理" : "停止"}</button> : null}
        {profileId ? <button className="formal-button compact danger collection-return-button" disabled={disabled(rowActionKey)} title={canReturnToWaiting ? "停止并直接删除该窗口任务，不退回等待；保留已采集结果和全局去重记录" : "仅移除这条记录，不退回等待；保留已采集结果和全局去重记录"} onClick={() => void deleteWindowTask("delete_only")}><Trash2 size={15} />{SUCCESS_STATES.has(state) ? "移除完成记录" : target ? "直接删除" : "移除等待窗口"}</button> : null}
        {profileId && canReturnToWaiting ? <button className="formal-button compact" disabled={disabled(rowActionKey)} onClick={() => void deleteWindowTask("delete")}><RotateCcw size={15} />退回等待</button> : null}
        {linkedFailure ? <button className="formal-button compact success" disabled={disabled(rowActionKey)} title="把失败项转回等待队列；已有采集结果仍会保留并去重" onClick={() => void run(rowActionKey, client => client.requeueFailedSplitTarget(linkedFailure.id), "失败项已返回等待列表，已采集部分将按全局去重跳过")}><RotateCcw size={15} />失败项返回等待</button> : null}
        {linkedFailure ? <button className="formal-button compact danger" disabled={disabled(rowActionKey)} title="只清除失败提醒；不会删除原任务、已采集结果或去重记录" onClick={() => void run(rowActionKey, client => client.deleteFailedSplitTarget(linkedFailure.id), "失败记录已删除，原任务、已采集结果和全局去重记录已保留")}><Trash2 size={15} />仅删除失败记录</button> : null}
        {!profileId && !target ? <button className="formal-button compact danger collection-return-button" disabled={disabled(rowActionKey)} onClick={() => void onDeleteTask(task)}><Trash2 size={15} />清理空任务</button> : null}
      </div>
    </div>
  );
}

type WorkspaceProps = {
  snapshot: CoreWorkbenchSnapshot;
  run: RunCoreAction;
  disabled: (key?: string) => boolean;
};

function ReviewWorkspace({ snapshot, run, disabled }: Omit<WorkspaceProps, "snapshot"> & { snapshot: CoreWorkbenchSnapshot | null }) {
  const { platform } = useWorkbenchPlatform();
  type ReviewView = "public" | "private";
  const client = usePlatformCore();
  const [reviewView, setReviewView] = useState<ReviewView>("public");
  const [reviewStage, setReviewStage] = useState<CoreReviewStage>(1);
  const [reviewOffset, setReviewOffset] = useState(0);
  const [reviewPage, setReviewPage] = useState<{ key: string; data: CoreReviewQueuePage } | null>(null);
  const [reviewReadError, setReviewReadError] = useState<string | null>(null);
  const [reviewLoading, setReviewLoading] = useState(true);
  const [reviewListOpen, setReviewListOpen] = useState(true);
  const [selectedCandidateIds, setSelectedCandidateIds] = useState<Set<string>>(new Set());
  const [reviewMutationBusy, setReviewMutationBusy] = useState(false);
  const [reviewBatchProgress, setReviewBatchProgress] = useState<{ completed: number; total: number } | null>(null);
  const reviewMutationRef = useRef(false);
  const reviewReadyRef = useRef(false);
  const reviewPanelRef = useRef<HTMLDetailsElement | null>(null);
  const candidateIdsRef = useRef<Set<string>>(new Set());
  const reviewReaderRef = useRef<ReturnType<typeof createReviewQueueReader> | null>(null);
  const reviewQueryRef = useRef<ReviewQueueQuery>({ platform, visibility: reviewView, review_stage: reviewStage, offset: reviewOffset, limit: REVIEW_PAGE_SIZE });
  reviewQueryRef.current = { platform, visibility: reviewView, review_stage: reviewStage, offset: reviewOffset, limit: REVIEW_PAGE_SIZE };
  const currentQueryKey = reviewQueueKey(reviewQueryRef.current);
  const queue = reviewView;
  const page = reviewPage?.key === currentQueryKey ? reviewPage.data : null;
  const candidates = page?.items ?? [];
  const counts = reviewPage?.data.counts;
  const publicReviewCount = counts ? counts.public.stage1 + counts.public.stage2 : snapshot?.counts.pending_public ?? snapshot?.pending.public.length;
  const privateReviewCount = counts ? counts.private.stage1 + counts.private.stage2 : snapshot?.counts.pending_private ?? snapshot?.pending.private.length;
  const pendingReviewTotal = publicReviewCount !== undefined && privateReviewCount !== undefined ? publicReviewCount + privateReviewCount : undefined;
  const currentReviewTotal = page?.total ?? 0;
  const quickCandidates = useMemo(() => candidates.map((item) => ({ item, visibility: queue })), [candidates, queue]);
  const currentReviewLabel = reviewView === "public" ? "公开审核" : "私密审核";
  const currentStageLabel = reviewStage === 1 ? "新采集结果" : "等待筛选";
  const candidateIdKey = candidates.map((item) => item.id).join("\u0000");
  candidateIdsRef.current = new Set(candidates.map((item) => item.id));
  const selectedCount = selectedReviewIds(candidates, selectedCandidateIds).length;
  const allCandidatesSelected = candidates.length > 0 && candidates.every((item) => selectedCandidateIds.has(item.id));
  const batchKey = `review-batch-${reviewView}-${reviewStage}`;
  const reviewControlsBusy = reviewMutationBusy || reviewLoading || Boolean(reviewReadError) || !page;
  reviewReadyRef.current = !reviewControlsBusy;
  const splitUsernames = useMemo(() => new Set((snapshot?.split_candidates ?? []).map((item) => item.username.replace(/^@/, "").toLowerCase())), [snapshot?.split_candidates]);

  const loadReview = useCallback((fresh = false) => {
    if (fresh) {
      reviewReadyRef.current = false;
      setReviewLoading(true);
    }
    return reviewReaderRef.current?.refresh(fresh) || Promise.resolve();
  }, []);
  useEffect(() => {
    const reader = createReviewQueueReader({
      query: () => reviewQueryRef.current,
      read: (query) => client.reviewQueue(query),
      onValue: (value, query) => {
        if (reviewQueueKey(query) !== reviewQueueKey(reviewQueryRef.current)) return;
        // Fence handlers from the previous render until the new page is committed.
        reviewReadyRef.current = false;
        if (!value.items.length && query.offset > 0) {
          // Approving the final row on a page can remove that page entirely.
          const lastOffset = Math.max(0, Math.floor(Math.max(0, value.total - 1) / query.limit) * query.limit);
          if (lastOffset < query.offset) { setReviewLoading(true); setReviewOffset(lastOffset); return; }
        }
        setReviewPage({ key: reviewQueueKey(query), data: value });
        setReviewReadError(null);
        setReviewLoading(false);
      },
      onError: (reason, query) => {
        if (reviewQueueKey(query) !== reviewQueueKey(reviewQueryRef.current)) return;
        reviewReadyRef.current = false;
        setReviewReadError(reason instanceof Error ? reason.message : String(reason));
        setReviewLoading(false);
      },
    });
    reviewReaderRef.current = reader;
    return () => { reader.dispose(); reviewReadyRef.current = false; if (reviewReaderRef.current === reader) reviewReaderRef.current = null; };
  }, [client]);
  useEffect(() => {
    setReviewLoading(true);
    setReviewReadError(null);
    setSelectedCandidateIds(new Set());
    void loadReview(true);
  }, [currentQueryKey, loadReview]);
  useEffect(() => {
    const timer = window.setInterval(() => { if (!reviewMutationRef.current) void loadReview(); }, 5_000);
    return () => window.clearInterval(timer);
  }, [loadReview]);
  useEffect(() => {
    const liveIds = new Set(candidates.map((item) => item.id));
    setSelectedCandidateIds((current) => retainLiveSelection(current, liveIds));
  }, [candidateIdKey]);

  useEffect(() => {
    const panel = reviewPanelRef.current;
    const header = panel?.closest(".formal-main")?.querySelector<HTMLElement>(".formal-header");
    if (!panel || !header) return;
    const update = () => {
      const position = window.getComputedStyle(header).position;
      const height = position === "sticky" || position === "fixed" ? Math.ceil(header.getBoundingClientRect().height) : 0;
      panel.style.setProperty("--review-header-offset", `${height}px`);
    };
    update();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(header);
    window.addEventListener("resize", update);
    return () => { observer?.disconnect(); window.removeEventListener("resize", update); };
  }, [reviewView, reviewStage]);

  function selectReviewView(next: ReviewView) {
    if (reviewMutationRef.current || next === reviewView) return;
    reviewReadyRef.current = false;
    setReviewLoading(true);
    reviewReaderRef.current?.invalidate();
    setSelectedCandidateIds(new Set());
    setReviewView(next);
    setReviewListOpen(true);
    setReviewOffset(0);
  }

  function selectReviewStage(next: CoreReviewStage) {
    if (reviewMutationRef.current || next === reviewStage) return;
    reviewReadyRef.current = false;
    setReviewLoading(true);
    reviewReaderRef.current?.invalidate();
    setSelectedCandidateIds(new Set());
    setReviewStage(next);
    setReviewOffset(0);
    setReviewListOpen(true);
  }

  function selectReviewPage(offset: number) {
    if (reviewMutationRef.current) return;
    reviewReadyRef.current = false;
    setReviewLoading(true);
    reviewReaderRef.current?.invalidate();
    setSelectedCandidateIds(new Set());
    setReviewOffset(offset);
    reviewPanelRef.current?.scrollIntoView({ block: "start", behavior: "auto" });
  }

  const toggleCandidateSelection = useCallback((id: string) => {
    if (reviewMutationRef.current || !reviewReadyRef.current || !candidateIdsRef.current.has(id)) return;
    setSelectedCandidateIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  function toggleSelectAll() {
    if (reviewMutationRef.current || !reviewReadyRef.current || reviewControlsBusy) return;
    setSelectedCandidateIds(allCandidatesSelected ? new Set() : new Set(candidates.map((item) => item.id)));
  }

  const decideCandidate = useCallback(async (target: CoreCandidate, visibility: "public" | "private", decision: "approved" | "rejected") => {
    if (reviewMutationRef.current || !reviewReadyRef.current || !candidateIdsRef.current.has(target.id)) return;
    reviewMutationRef.current = true;
    reviewReaderRef.current?.invalidate();
    setReviewMutationBusy(true);
    try {
      await run(`review-${target.id}`, (core) => core.decideReview({ candidate_id: target.id, decision }), decision === "approved" ? `账号已进入${visibility === "public" ? "公开" : "私密"}合格区` : "账号已移入人工审核不合格历史");
    } finally {
      await reviewReaderRef.current?.refresh(true);
      reviewMutationRef.current = false;
      setReviewMutationBusy(false);
    }
  }, [run]);

  const addReviewCandidateToSplit = useCallback(async (target: CoreCandidate) => {
    if (reviewMutationRef.current || !reviewReadyRef.current || !candidateIdsRef.current.has(target.id)) return;
    await addCandidateToSplit(target, run);
  }, [run]);

  async function moveSelected() {
    if (reviewMutationRef.current || !reviewReadyRef.current || reviewControlsBusy) return;
    const candidateIds = selectedReviewIds(candidates, selectedCandidateIds);
    if (!candidateIds.length) return;
    const fromStage = reviewStage, toStage: CoreReviewStage = reviewStage === 1 ? 2 : 1;
    reviewMutationRef.current = true;
    reviewReaderRef.current?.invalidate();
    setReviewMutationBusy(true);
    try {
      await run(batchKey, async (core) => {
        const result = await core.moveReviewStage({ candidate_ids: candidateIds, visibility: queue, from_stage: fromStage, to_stage: toStage });
        const moved = new Set(result.moved_ids);
        setSelectedCandidateIds((current) => new Set([...current].filter((id) => !moved.has(id))));
        return result;
      }, (result) => {
        const moved = asRecord(result);
        const skipped = Array.isArray(moved.skipped_ids) ? moved.skipped_ids.length : 0;
        return `已${toStage === 2 ? "转入等待筛选" : "退回新采集结果"} ${Number(moved.moved_count) || 0} 个账号${skipped ? `；${skipped} 个账号状态已变化，未移动` : ""}${toStage === 2 ? "。点击“等待筛选”即可继续审核" : ""}`;
      });
    } finally {
      await loadReview(true);
      reviewMutationRef.current = false;
      setReviewMutationBusy(false);
    }
  }

  async function decideSelected(decision: "approved" | "rejected") {
    if (reviewMutationRef.current || !reviewReadyRef.current || reviewControlsBusy) return;
    const selectedCandidates = candidates.filter((item) => selectedCandidateIds.has(item.id));
    if (!selectedCandidates.length) return;
    const failedIds = new Set(selectedCandidates.map((item) => item.id));
    const failures: Array<{ username: string; message: string }> = [];
    reviewMutationRef.current = true;
    reviewReaderRef.current?.invalidate();
    setReviewMutationBusy(true);
    try {
      setReviewBatchProgress({ completed: 0, total: selectedCandidates.length });
      await run(batchKey, async (core) => {
        let completed = 0;
        for (const target of selectedCandidates) {
          try {
            await core.decideReview({ candidate_id: target.id, decision });
            failedIds.delete(target.id);
          } catch (reason) {
            failures.push({ username: target.username.replace(/^@/, ""), message: reason instanceof Error ? reason.message : String(reason) });
          } finally {
            setReviewBatchProgress({ completed: ++completed, total: selectedCandidates.length });
          }
        }
        if (failures.length) {
          const succeeded = selectedCandidates.length - failures.length;
          const details = failures.slice(0, 3).map((failure) => `@${failure.username}：${failure.message}`).join("；");
          throw new Error(`批量${decision === "approved" ? "合格" : "不合格"}完成：成功 ${succeeded} 个，失败 ${failures.length} 个。失败账号已保留勾选${details ? `；${details}` : ""}`);
        }
      }, `已批量标记${decision === "approved" ? "合格" : "不合格"} ${selectedCandidates.length} 个账号`);
    } finally {
      await loadReview(true);
      setSelectedCandidateIds(new Set([...failedIds].filter((id) => candidateIdsRef.current.has(id))));
      setReviewBatchProgress(null);
      reviewMutationRef.current = false;
      setReviewMutationBusy(false);
    }
  }

  return (
    <>
      <div className="formal-stats review-stats">
        <StatCard icon={<Eye size={22} />} label="公开待审核" value={publicReviewCount} />
        <StatCard icon={<LockKeyhole size={22} />} label="私密待审核" value={privateReviewCount} />
        <StatCard icon={<Clock3 size={22} />} label="待审核总量" value={pendingReviewTotal} />
        <StatCard icon={<Database size={22} />} label="全局去重数量" value={snapshot?.dedupe.total} />
      </div>
      <section className="review-stage-controls" aria-label="审核分层">
        <div className="review-stage-topline">
          <div className="formal-toolbar">
            <button className={`formal-button ${reviewView === "public" ? "primary" : ""}`} disabled={reviewMutationBusy} onClick={() => selectReviewView("public")}><Eye size={16} />公开审核 <span className="formal-status">{formatCount(publicReviewCount)}</span></button>
            <button className={`formal-button ${reviewView === "private" ? "primary" : ""}`} disabled={reviewMutationBusy} onClick={() => selectReviewView("private")}><LockKeyhole size={16} />私密审核 <span className="formal-status">{formatCount(privateReviewCount)}</span></button>
          </div>
          <button className="formal-button compact" disabled={reviewMutationBusy || reviewLoading} onClick={() => { setReviewLoading(true); void loadReview(true); }}><RefreshCw size={15} />刷新名单</button>
        </div>
        <div className="review-stage-tabs" role="tablist" aria-label={`${currentReviewLabel}筛选层级`}>
          <button type="button" role="tab" aria-selected={reviewStage === 1} className={reviewStage === 1 ? "is-active" : ""} disabled={reviewMutationBusy} onClick={() => selectReviewStage(1)}><span className="review-stage-number">01</span><span><strong>新采集结果</strong></span><b>{counts ? formatCount(counts[queue].stage1) : "—"}</b></button>
          <button type="button" role="tab" aria-selected={reviewStage === 2} className={reviewStage === 2 ? "is-active" : ""} disabled={reviewMutationBusy} onClick={() => selectReviewStage(2)}><span className="review-stage-number">02</span><span><strong>等待筛选</strong></span><b>{counts ? formatCount(counts[queue].stage2) : "—"}</b></button>
        </div>

      </section>
      <details ref={reviewPanelRef} key={`${currentReviewLabel}-${reviewStage}`} className={`formal-panel formal-history-details quick-review-panel review-layer-panel ${queue === "private" ? "private-review-list" : "public-review-list"}`} open={reviewListOpen} onToggle={(event) => setReviewListOpen(event.currentTarget.open)}>
        <summary className="formal-panel-header">
          <div className="formal-panel-title"><span>{reviewStage === 2 ? <ListFilter size={19} /> : <Inbox size={19} />}</span><div><h2>{currentReviewLabel} · {currentStageLabel}</h2></div></div>
          <div className="quick-review-summary"><span>本页 {formatCount(quickCandidates.length)} / 共 {page ? formatCount(currentReviewTotal) : "—"} 个</span><ChevronDown size={18} /></div>
        </summary>
        <div className="review-list-controls">
        <div className="quick-review-batch-toolbar">
          <span className="formal-selected-count">已选 {selectedCount} 个</span>
          {reviewBatchProgress ? <span className="review-batch-progress" role="status" aria-live="polite">已处理 {reviewBatchProgress.completed} / {reviewBatchProgress.total}</span> : null}
          <button type="button" className="formal-button compact" disabled={!candidates.length || reviewControlsBusy} onClick={toggleSelectAll}>{allCandidatesSelected ? "取消本页全选" : "全选本页"}</button>
          <button type="button" className="formal-button compact review-stage-transfer" disabled={!selectedCount || reviewControlsBusy || disabled(batchKey)} onClick={() => void moveSelected()}>{reviewStage === 1 ? <ArrowDown size={15} /> : <RotateCcw size={15} />}{reviewStage === 1 ? "转入等待筛选" : "退回第一层"}</button>
          <span className="review-batch-separator" aria-hidden="true" />
          <button type="button" className="formal-button compact danger" disabled={!selectedCount || reviewControlsBusy || disabled(batchKey)} onClick={() => void decideSelected("rejected")}>{reviewMutationBusy ? <LoaderCircle className="animate-spin" size={15} /> : <X size={15} />}{reviewMutationBusy ? "处理中" : `批量不合格（${selectedCount}）`}</button>
          <button type="button" className="formal-button compact primary" disabled={!selectedCount || reviewControlsBusy || disabled(batchKey)} onClick={() => void decideSelected("approved")}>{reviewMutationBusy ? <LoaderCircle className="animate-spin" size={15} /> : <Check size={15} />}{reviewMutationBusy ? "处理中" : `批量合格（${selectedCount}）`}</button>
          <nav className="review-page-navigation" aria-label="审核名单分页"><button type="button" className="formal-button compact" disabled={reviewControlsBusy || reviewOffset === 0} onClick={() => selectReviewPage(Math.max(0, reviewOffset - REVIEW_PAGE_SIZE))}>上一页</button><span>{Math.floor(reviewOffset / REVIEW_PAGE_SIZE) + 1} / {Math.max(1, Math.ceil(currentReviewTotal / REVIEW_PAGE_SIZE))}</span><button type="button" className="formal-button compact" disabled={reviewControlsBusy || !page?.has_more} onClick={() => selectReviewPage(reviewOffset + REVIEW_PAGE_SIZE)}>下一页</button></nav>
        </div>
          {queue === "private"
            ? <div className="quick-review-table-head"><span>粉丝</span><span>关注</span><span>帖子</span><span>采集时间</span><span>头像与账号</span><span className="quick-review-select-head">选择</span><span>是否合格</span></div>
            : <div className="quick-review-table-head"><span>采集时间</span><span>所在地</span><span>帖子数</span><span>粉丝数</span><span>关注数量</span><span>活跃度</span><span className="quick-review-avatar-head">头像</span><span>账号</span><span className="quick-review-select-head">多选</span><span>功能按键</span></div>}
        </div>
        {reviewReadError ? <div className="review-stage-error" role="alert"><AlertTriangle size={17} /><span>名单刷新失败：{reviewReadError}。请刷新名单后继续操作。</span></div> : null}
        <div className="quick-review-scroll" aria-busy={reviewLoading}>
          {quickCandidates.map(({ item, visibility }) => <ReviewCandidateRow key={item.id} item={item} queue={queue} visibility={visibility}
            selected={selectedCandidateIds.has(item.id)} reviewMutationBusy={reviewControlsBusy} disabled={disabled} splitUsernames={splitUsernames}
            toggleCandidateSelection={toggleCandidateSelection} decideCandidate={decideCandidate} addReviewCandidateToSplit={addReviewCandidateToSplit} />)}
          {!quickCandidates.length && reviewLoading ? <div className="review-stage-loading" role="status"><LoaderCircle className="animate-spin" size={22} />正在读取{currentStageLabel}…</div> : null}
          {!quickCandidates.length && !reviewLoading && !reviewReadError ? <EmptyState icon={<ShieldCheck size={28} />} title={`${currentStageLabel}暂无待审核账号`} /> : null}
        </div>
        <div className="review-stage-pagination">
          <span>{page && currentReviewTotal > 0 ? `第 ${reviewOffset + 1}–${reviewOffset + candidates.length} 个，共 ${formatCount(currentReviewTotal)} 个` : `每页最多 ${REVIEW_PAGE_SIZE} 个账号`}</span>
          <span>每页 {REVIEW_PAGE_SIZE} 个 · 连续滚动查看</span>
        </div>
      </details>
    </>
  );
}

const StableReviewWorkspace = memo(ReviewWorkspace, sameReviewWorkspaceInputs);

type ReviewCandidateRowProps = {
  item: CoreCandidate;
  queue: "public" | "private";
  visibility: "public" | "private";
  selected: boolean;
  reviewMutationBusy: boolean;
  disabled: WorkspaceProps["disabled"];
  splitUsernames: Set<string>;
  toggleCandidateSelection: (id: string) => void;
  decideCandidate: (item: CoreCandidate, visibility: "public" | "private", decision: "approved" | "rejected") => Promise<void>;
  addReviewCandidateToSplit: (item: CoreCandidate) => Promise<void>;
};

// Selecting one row does not rebuild every other account/thumbnail subtree.
// All props participate in equality, so busy state or changed controls render.
const ReviewCandidateRow = memo(function ReviewCandidateRow({ item, queue, visibility, selected, reviewMutationBusy, disabled, splitUsernames,
  toggleCandidateSelection, decideCandidate, addReviewCandidateToSplit }: ReviewCandidateRowProps) {
  const itemProfile = profileOf(item);
  const itemScreening = asRecord(item.screening);
  const itemFailures = reviewFailureFlags(itemScreening, itemProfile);
  const itemSuccesses = reviewSuccessFlags(itemScreening, itemProfile);
  const itemLocation = reviewLocation(itemProfile, itemScreening);
  return <div className="quick-review-row" key={item.id}>
    {queue === "private" ? <>
      <strong data-review-field="followers" data-label="粉丝数" className={itemFailures.followers ? "review-over-limit-value" : itemSuccesses.followers ? "review-within-limit-value" : ""}>{formatCount(firstNumber(itemProfile, ["followers", "followers_count", "follower_count"]))}</strong>
      <strong data-review-field="following" data-label={("关注数")} className={itemFailures.following ? "review-over-limit-value" : itemSuccesses.following ? "review-within-limit-value" : ""}>{formatCount(firstNumber(itemProfile, ["following", "following_count"]))}</strong>
      <strong data-review-field="posts" data-label="帖子数" className={itemFailures.posts ? "review-over-limit-value" : itemSuccesses.posts ? "review-within-limit-value" : ""}>{formatCount(firstNumber(itemProfile, ["posts", "posts_count", "media_count"]))}</strong>
      <span data-review-field="collected" data-label="采集时间">{formatTime(item.created_at)}</span>
      <a className="quick-review-account formal-profile-identity-link" href={collectionProfileUrl(item)} target="_blank" rel="noreferrer" title={("在软件内预览 Instagram 主页")}><Avatar candidate={item} small /><span><strong>{candidateName(item)} <ExternalLink size={13} /></strong><small><span className={`collection-platform-badge is-${collectionPlatform(item)}`}>{("IG")}</span> @{collectionProfileLabel(item)}</small></span></a>
    </> : <>
      <span data-review-field="collected" data-label="采集时间">{formatTime(item.created_at)}</span>
      <span data-review-field="location" data-label="所在地" className={itemLocation === "—" || itemFailures.location ? "review-over-limit-value" : itemSuccesses.location ? "review-within-limit-value" : ""}>{itemLocation === "—" ? "未知" : itemLocation}</span>
      <strong data-review-field="posts" data-label="帖子数" className={itemFailures.posts ? "review-over-limit-value" : itemSuccesses.posts ? "review-within-limit-value" : ""}>{formatCount(firstNumber(itemProfile, ["posts", "posts_count", "media_count"]))}</strong>
      <strong data-review-field="followers" data-label="粉丝数" className={itemFailures.followers ? "review-over-limit-value" : itemSuccesses.followers ? "review-within-limit-value" : ""}>{formatCount(firstNumber(itemProfile, ["followers", "followers_count", "follower_count"]))}</strong>
      <strong data-review-field="following" data-label={("关注数")} className={itemFailures.following ? "review-over-limit-value" : itemSuccesses.following ? "review-within-limit-value" : ""}>{formatCount(firstNumber(itemProfile, ["following", "following_count"]))}</strong>
      <span data-review-field="activity" data-label="活跃度" className={itemFailures.activity ? "review-over-limit-value" : itemSuccesses.activity ? "review-within-limit-value" : ""}>{reviewActivityLabel(itemProfile)}</span>
      <a className="quick-review-avatar-link" href={collectionProfileUrl(item)} target="_blank" rel="noreferrer" title={("在软件内预览 Instagram 主页")}><Avatar candidate={item} small /></a>
      <a className="quick-review-account formal-profile-identity-link" href={collectionProfileUrl(item)} target="_blank" rel="noreferrer" title={("在软件内预览 Instagram 主页")}><span><strong>{candidateName(item)} <ExternalLink size={13} /></strong><small><span className={`collection-platform-badge is-${collectionPlatform(item)}`}>{("IG")}</span> @{collectionProfileLabel(item)}</small></span></a>
    </>}
    <label className="formal-check quick-review-select" title={`选择 @${item.username.replace(/^@/, "")}`}><input type="checkbox" aria-label={`选择 @${item.username.replace(/^@/, "")}`} checked={selected} disabled={reviewMutationBusy} onChange={() => toggleCandidateSelection(item.id)} /></label>
    <div className="formal-row-actions quick-review-actions">{queue === "public" && collectionPlatform(item) === "instagram" ? <button type="button" className="formal-button compact" disabled={reviewMutationBusy || disabled(`review-split-${item.id}`)} onClick={() => void addReviewCandidateToSplit(item)}><Plus size={15} />{splitUsernames.has(item.username.replace(/^@/, "").toLowerCase()) ? "已加入" : "分裂号"}</button> : null}<button type="button" className="formal-button compact danger" disabled={reviewMutationBusy || disabled(`review-${item.id}`)} onClick={() => void decideCandidate(item, visibility, "rejected")}><X size={15} />不合格</button><button type="button" className="formal-button compact primary" disabled={reviewMutationBusy || disabled(`review-${item.id}`)} onClick={() => void decideCandidate(item, visibility, "approved")}><Check size={15} />合格</button></div>
  </div>;
}, sameComponentProps);



async function addSplitTargetsWithConfirmation(client: ReturnType<typeof getCollectorCoreClient>, usernames: string[], windowIds: string[] = [], onSaved?: (candidates: CoreSplitCandidate[]) => void): Promise<CoreSplitCandidate[]> {
  const outcome = await client.addWaitingSplitTargets(usernames, false, windowIds);
  const saved = outcome.candidates.filter(c => outcome.accepted_ids.includes(c.id));
  onSaved?.(saved);
  const repeatable = outcome.duplicates.filter(d => ["completed", "history", "ignored"].includes(String(d.disposition)));
  if (repeatable.length && window.confirm(`${repeatable.map(d => `@${d.username}`).join("、")} 已有分裂记录。\n\n是否再次加入？已采集账号仍会自动去重，历史记录保留。`)) {
    const repeated = await client.addWaitingSplitTargets(repeatable.map(d => String(d.username)), true, windowIds);
    const accepted = repeated.candidates.filter(c => repeated.accepted_ids.includes(c.id));
    saved.push(...accepted); onSaved?.(accepted);
    if (repeated.duplicates.length) window.alert(repeated.duplicates.map(d => `@${d.username}：状态已变化，已保留原任务，请刷新查看。`).join("\n"));
  }
  for (const duplicate of outcome.duplicates.filter(d => d.disposition === "failure")) {
    const id = (Array.isArray(duplicate.candidate_ids) ? duplicate.candidate_ids : []).find((id): id is string => typeof id === "string" && Boolean(id));
    if (id && window.confirm(`@${duplicate.username} 上次分裂未完成。是否返回等待继续采集？已采集账号会自动去重。`)) {
      const accepted = await client.requeueFailedSplitTarget(id, windowIds);
      saved.push(accepted); onSaved?.([accepted]);
    }
  }
  const active = outcome.duplicates.filter(d => ["waiting", "running"].includes(String(d.disposition)));
  if (active.length) window.alert(active.map(d => `@${d.username} 已在${d.disposition === "running" ? "执行任务" : "等待列表"}中，将沿用现有任务。`).join("\n"));
  return saved;
}

async function addCandidateToSplit(target: CoreCandidate, run: RunCoreAction) {

  const username = target.username.replace(/^@/, "");
  await run(`review-split-${target.id}`, client => addSplitTargetsWithConfirmation(client, [username]), "分裂号添加检查已完成");
}

function CandidateRow({ candidate, selected, onToggle, selectionDisabled = false, actions, profileLink = false }: { candidate: CoreCandidate; selected?: boolean; onToggle?: () => void; selectionDisabled?: boolean; actions?: ReactNode; profileLink?: boolean }) {
  const profile = profileOf(candidate);
  const identity = <><Avatar candidate={candidate} small /><div className="formal-row-main">
    <strong>{candidateName(candidate)}{profileLink && <ExternalLink size={13} />}</strong>
    <small><span className={`collection-platform-badge is-${collectionPlatform(candidate)}`}>{("IG")}</span> @{collectionProfileLabel(candidate)} · {reviewLocation(profile, asRecord(candidate.screening)) === "—" ? "所在地未知" : reviewLocation(profile, asRecord(candidate.screening))}</small>
    <small>粉丝 {formatCount(firstNumber(profile, ["followers", "followers_count", "follower_count"]))} · {("关注")} {formatCount(firstNumber(profile, ["following", "following_count"]))}</small>

  </div></>;
  return <div className={`formal-row ${profileLink ? "public-candidate-row" : ""} ${selected ? "is-selected" : ""}`}>
    {onToggle ? <label className="formal-check"><input type="checkbox" checked={Boolean(selected)} disabled={selectionDisabled} onChange={onToggle} /></label> : null}
    {profileLink ? <a className="formal-profile-identity-link public-candidate-identity" href={collectionProfileUrl(candidate)} target="_blank" rel="noreferrer" title="打开账号主页">{identity}</a> : identity}
    {actions ? <div className="formal-row-actions">{actions}</div> : null}
  </div>;
}

function ActionWorkspace({ snapshot, run, disabled, operation, actionControls }: WorkspaceProps & { operation: CoreActionOperation; actionControls: ActionControls }) {
  const client = usePlatformCore();
  const { platform } = useWorkbenchPlatform();
  const isPublic = operation === "greet";
  const approved = isPublic ? snapshot.approved.public : snapshot.approved.private;
  const splitUsernames = new Set(snapshot.split_candidates.map(item => item.username.replace(/^@/, "").toLowerCase()));
  const approvedTotal = snapshot.counts[isPublic ? "approved_public" : "approved_private"] ?? approved.length;
  const [selectedAccounts, setSelectedAccounts] = useState<Set<string>>(new Set());
  const [accountSelectionCount, setAccountSelectionCount] = useState("");
  const [accountSelectionNotice, setAccountSelectionNotice] = useState<string | null>(null);
  const [selectedWindows, setSelectedWindows] = useState<Set<string>>(new Set());
  const [selectedWindowOrder, setSelectedWindowOrder] = useState<string[]>([]);
  const [intervalMin, setIntervalMin] = useState("8");
  const [intervalMax, setIntervalMax] = useState("15");
  const [messages, setMessages] = useState("");
  const [query, setQuery] = useState("");
  const [exportBusy, setExportBusy] = useState(false);
  const exportBusyRef = useRef(false);
  const [deleteProgress, setDeleteProgress] = useState<{ completed: number; total: number } | null>(null);
  const deleteBusyRef = useRef(false);
  const exportOperationRef = useRef(operation);
  exportOperationRef.current = operation;
  const [exportNotice, setExportNotice] = useState<{ operation: CoreActionOperation; message: string; error: boolean } | null>(null);
  const operationCampaigns = snapshot.campaigns.filter((campaign) => campaign.operation === operation);
  const operationHistoryCampaigns = mergedCampaignHistory(snapshot).filter((campaign) => campaign.operation === operation);
  const greetingMessages = parseGreetingMessages(messages);
  const active = operationCampaigns.filter((campaign) => {
    const campaignState = normalizeStatus(campaign.status);
    if (campaignState === "recoverable") return true;
    if (!ACTIVE_STATES.has(campaignState) && !PAUSED_STATES.has(campaignState)) return false;
    if (campaign.targets_truncated === true) return true;
    return (campaign.targets || []).some((target) => {
      const state = normalizeStatus(target.status);
      return !SUCCESS_STATES.has(state) && !FAILURE_TARGET_STATES.has(state);
    });
  });
  // A campaign can remain active while one target has already failed.  Failed
  // rows therefore come from every campaign's target states, not campaign state.
  const failures = operationCampaigns.filter((campaign) => (campaign.targets || []).some((target) => {
    const state = normalizeStatus(target.status);
    return FAILURE_TARGET_STATES.has(state);
  }));
  // A sibling target may fail after another target was confirmed.  The success
  // ledger is target-based, so every campaign containing a confirmed target
  // belongs in immutable history regardless of aggregate campaign status.
  const historyActions = operationHistoryCampaigns.filter((campaign) => successfulActionRows([campaign]).length > 0);
  const visibleApproved = approved.filter((candidate) => !query.trim() || candidate.username.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()) || candidateName(candidate).toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const allVisibleSelected = visibleApproved.length > 0 && visibleApproved.every((candidate) => selectedAccounts.has(candidate.id));
  const selectedApprovedCount = approved.filter((candidate) => selectedAccounts.has(candidate.id)).length;
  const deleteBusy = deleteProgress !== null;
  const deleteKey = `approved-dismiss-selected-${operation}`;
  const requestedAccountCount = Number(accountSelectionCount.trim());
  const validAccountCount = /^\d+$/.test(accountSelectionCount.trim()) && Number.isSafeInteger(requestedAccountCount) && requestedAccountCount > 0;

  useEffect(() => {
    const live = new Set(approved.map((candidate) => candidate.id));
    setSelectedAccounts((current) => retainLiveSelection(current, live));
    setAccountSelectionNotice(null);
  }, [approved]);

  function toggleAccount(id: string) {
    if (deleteBusyRef.current) return;
    setAccountSelectionNotice(null);
    setSelectedAccounts((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function selectVisibleAccounts(selected: boolean) {
    if (deleteBusyRef.current) return;
    setAccountSelectionNotice(null);
    setSelectedAccounts((current) => {
      const next = new Set(current);
      visibleApproved.forEach((candidate) => selected ? next.add(candidate.id) : next.delete(candidate.id));
      return next;
    });
  }

  function selectAccountCount() {
    if (deleteBusyRef.current || !validAccountCount || !visibleApproved.length) return;
    // Replace the selection, including hidden search results, so N means N total.
    // Only this click's displayed accounts are selected; later arrivals stay idle.
    const ids = visibleApproved.slice(0, requestedAccountCount).map((candidate) => candidate.id);
    setSelectedAccounts(new Set(ids));
    setAccountSelectionNotice(requestedAccountCount > ids.length ? `当前列表仅 ${ids.length} 个，已全部选中` : null);
  }

  async function deleteSelectedAccounts() {
    if (deleteBusyRef.current || disabled(deleteKey)) return;
    // Freeze the clicked selection; later arrivals or a switch of lane cannot
    // expand this batch. Use the existing dismissal API to preserve ledgers and
    // its deferred cancellation fence for targets already executing.
    const ids = approved.filter((candidate) => selectedAccounts.has(candidate.id)).map((candidate) => candidate.id);
    if (!ids.length) return;
    deleteBusyRef.current = true;
    setDeleteProgress({ completed: 0, total: ids.length });
    try {
      await run(deleteKey, async (client) => {
        let completed = 0;
        const failures: string[] = [];
        for (const id of ids) {
          try {
            await client.dismissApprovedCandidate(id);
            setSelectedAccounts((current) => {
              const next = new Set(current); next.delete(id); return next;
            });
          } catch (reason) {
            failures.push(reason instanceof Error ? reason.message : String(reason));
          } finally {
            setDeleteProgress({ completed: ++completed, total: ids.length });
          }
        }
        if (failures.length) throw new Error(`删除完成：成功 ${ids.length - failures.length} 个，失败 ${failures.length} 个。失败账号已保留勾选，可再次点击删除。${failures[0]}`);
      }, `已删除选中 ${ids.length} 个账号，去重记录和审核历史已保留`);
    } finally {
      deleteBusyRef.current = false;
      setDeleteProgress(null);
    }
  }

  async function exportAccounts(scope: "selected" | "all") {
    if (exportBusyRef.current || (scope === "selected" && !selectedAccounts.size)) return;
    exportBusyRef.current = true;
    setExportBusy(true);
    setExportNotice(null);
    const exportOperation = operation;
    try {
      const result = await client.exportApprovedAccounts({
        visibility: isPublic ? "public" : "private", scope,
        ...(scope === "selected" ? { candidate_ids: [...selectedAccounts] } : {}),
      });
      if (result.row_count) downloadAccountExport(result);
      if (exportOperationRef.current === exportOperation) setExportNotice({ operation: exportOperation, error: false,
        message: result.row_count
          ? `已发起 ${formatCount(result.row_count)} 个${isPublic ? "公开" : "私密"}账号的 CSV 下载，可用 Excel 打开。${result.skipped_count ? `有 ${result.skipped_count} 个所选账号已移出当前合格区，未导出。` : ""}`
          : "没有可导出的账号，所选账号可能已移出当前合格区。",
      });
    } catch (reason) {
      if (exportOperationRef.current === exportOperation) setExportNotice({ operation: exportOperation, error: true,
        message: reason instanceof Error ? reason.message : String(reason),
      });
    } finally {
      exportBusyRef.current = false;
      setExportBusy(false);
    }
  }

  async function startCampaign() {
    if (deleteBusyRef.current) return;
    const accounts = approved.filter((candidate) => selectedAccounts.has(candidate.id));
    const windowIds = selectedWindowOrder;
    if (!accounts.length || !windowIds.length || accounts.some(candidate => collectionPlatform(candidate) !== "instagram")) return;
    // Keep the function itself fail-closed even if a future UI change makes the
    // disabled button reachable. Never create a greeting campaign with an empty
    // or partially accepted message library.
    if (isPublic && !greetingMessages.ok) return;
    const min = Math.max(1, Number.parseInt(intervalMin, 10) || 1);
    const max = Math.max(min, Number.parseInt(intervalMax, 10) || min);
    const groups = new Map<string, CoreCandidate[]>(windowIds.map((id) => [id, []]));
    accounts.forEach((candidate, index) => groups.get(windowIds[index % windowIds.length])?.push(candidate));
    const lines = greetingMessages.ok ? greetingMessages.messages : [];
    const ok = await run(`campaign-start-${operation}`, async (client) => {
      for (const [profileId, candidates] of groups) {
        if (!candidates.length) continue;
        await client.startActionCampaign({
          operation,
          profile_id: profileId,
          targets: candidates.map((candidate) => candidate.username),
          target_sources: Object.fromEntries(candidates.map((candidate) => [candidate.username, candidate.source_target || ""])),
          ...(isPublic ? { message: lines[0], messages: lines } : {}),
          interval: `${min}-${max}`,
          limit: candidates.length,
        });
      }
    }, `${isPublic ? "打招呼" : "点关注"}任务已按窗口分组启动`);
    if (ok) setSelectedAccounts(new Set());
  }

  async function manualAction(candidate: CoreCandidate) {
    if ((deleteBusyRef.current) || collectionPlatform(candidate) !== "instagram") return;
    const profileId = selectedWindowOrder[0];
    if (!profileId) return;
    // Manual greetings use the same validation contract as campaign greetings;
    // they must never reach Core without a valid message.
    if (isPublic && !greetingMessages.ok) return;
    const lines = greetingMessages.ok ? greetingMessages.messages : [];
    await run(`manual-${operation}-${candidate.id}`, (client) => client.runManualAction({ operation, profile_id: profileId, target: candidate.username, source_target: candidate.source_target || undefined, ...(isPublic && lines[0] ? { message: lines[0] } : {}) }), "单个任务已提交");
  }

  const dismissFailure = (campaignId: string, targetId: string) => run(
    `failure-dismiss-${campaignId}-${targetId}`,
    (client) => client.dismissActionFailure(campaignId, targetId),
    "失败记录已删除，未成功账号已回到合格区",
  );

  const dismissAllOrdinaryFailures = (items: Array<{ campaignId: string; targetId: string }>) => run(
    `failure-dismiss-all-${operation}`,
    async (client) => {
      const errors: string[] = [];
      let completed = 0;
      for (const item of items) {
        try {
          await client.dismissActionFailure(item.campaignId, item.targetId);
          completed += 1;
        } catch (reason) {
          errors.push(reason instanceof Error ? reason.message : String(reason));
        }
      }
      if (errors.length) throw new Error(`已删除并退回 ${completed} 条，另有 ${errors.length} 条失败，请重试。${errors[0] ? ` ${errors[0]}` : ""}`);
      return { completed };
    },
    (result) => `已一键删除并退回 ${(result as { completed: number }).completed} 条普通失败记录`,
  );

  const resolveUnknown = (campaignId: string, targetId: string, outcome: "not_completed" | "completed") => run(
    `unknown-resolve-${outcome}-${campaignId}-${targetId}`,
    (client) => client.resolveUnknownAction(campaignId, targetId, outcome),
    outcome === "completed" ? "该账号已按人工确认记为成功并进入永久历史" : "该账号已按人工确认退回合格区等待执行",
  );

  const authoritativeSuccessCount = firstNumber(
    asRecord(snapshot.dedupe),
    [isPublic ? "greet_successes" : "follow_successes"],
  ) ?? countSuccessfulTargets(historyActions);
  const failedTargetCount = failures.reduce((total, campaign) => total + (campaign.targets || []).filter((target) => FAILURE_TARGET_STATES.has(normalizeStatus(target.status))).length, 0);

  return (
    <>
      <div className="formal-stats">
        <StatCard icon={isPublic ? <MessageCircle size={22} /> : <LockKeyhole size={22} />} label={isPublic ? "公开合格账号" : "私密合格账号"} value={approvedTotal} />
        {(<><StatCard icon={<Play size={22} />} label="正在执行任务" value={active.length} />
        <StatCard icon={<AlertTriangle size={22} />} label="失败任务" value={failedTargetCount} />
        <StatCard icon={<Check size={22} />} label={isPublic ? "成功发送总数" : "成功关注总数"} value={authoritativeSuccessCount} /></>)}
      </div>

      <div className={`formal-action-grid ${("")}`}>
        <Panel
          icon={isPublic ? <UserCheck size={19} /> : <LockKeyhole size={19} />}
          title={isPublic ? "公开合格账号" : "私密合格账号"}

          actions={<div className="formal-toolbar"><span className="formal-chip">显示 {formatCount(approved.length)} / 总数 {formatCount(approvedTotal)}</span><span className="formal-chip">已选 {selectedAccounts.size}</span></div>}
          className="action-approved-panel"
        >
          <div className="formal-panel-body">
            <label className="formal-field"><span>搜索账号名称或用户名</span><input className="formal-input" value={query} onChange={(event) => { setQuery(event.target.value); setAccountSelectionNotice(null); }} placeholder="输入名称或用户名" /></label>
            <div className="formal-toolbar" style={{ marginTop: 10 }}>
              <label className="formal-check"><input type="checkbox" checked={allVisibleSelected} disabled={deleteBusy} onChange={(event) => selectVisibleAccounts(event.target.checked)} />全选当前列表</label>
              <div className="approved-count-selector">
                <input className="formal-input" type="number" inputMode="numeric" min={1} step={1} aria-label="选择账号数量" placeholder="选择数量" value={accountSelectionCount} disabled={deleteBusy} aria-invalid={Boolean(accountSelectionCount) && !validAccountCount} onChange={(event) => { setAccountSelectionCount(event.target.value); setAccountSelectionNotice(null); }} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); selectAccountCount(); } }} />
                <button type="button" className="formal-button compact" disabled={deleteBusy || !validAccountCount || !visibleApproved.length} onClick={selectAccountCount}>按数量选择</button>
              </div>
              <button type="button" className="formal-button compact" disabled={deleteBusy || !visibleApproved.length} onClick={() => selectVisibleAccounts(!allVisibleSelected)}>{allVisibleSelected ? "取消全选" : "一键全选"}</button>
              <button type="button" className="formal-button compact danger" disabled={deleteBusy || !selectedApprovedCount || disabled(deleteKey)} onClick={() => void deleteSelectedAccounts()}><Trash2 size={14} />{deleteBusy ? "正在删除…" : `一键删除选中（${selectedApprovedCount}）`}</button>

              {deleteProgress ? <span role="status" aria-live="polite" className="formal-chip">已处理 {deleteProgress.completed} / {deleteProgress.total}</span> : null}
              {accountSelectionNotice ? <span role="status" aria-live="polite" className="formal-chip">{accountSelectionNotice}</span> : null}
            </div>
            <div className="account-export-toolbar" aria-label={`${isPublic ? "公开" : "私密"}账号信息导出`}>
              <button type="button" className="formal-button compact" disabled={exportBusy || !selectedAccounts.size} onClick={() => void exportAccounts("selected")}><Download size={14} />导出所选（{selectedAccounts.size}）</button>
              <button type="button" className="formal-button compact" disabled={exportBusy} onClick={() => void exportAccounts("all")}><Download size={14} />{exportBusy ? "正在准备导出…" : "导出全部"}</button>

            </div>
            {exportNotice?.operation === operation ? <span className={`account-export-notice ${exportNotice.error ? "is-error" : ""}`} role={exportNotice.error ? "alert" : "status"}>{exportNotice.message}</span> : null}
          </div>
          <div className="formal-panel-body formal-scroll tall">
            <div className="formal-list">
              {visibleApproved.map((candidate) => <CandidateRow
                key={candidate.id}
                candidate={candidate}
                profileLink={isPublic}
                selected={selectedAccounts.has(candidate.id)}
                onToggle={() => toggleAccount(candidate.id)}
                selectionDisabled={deleteBusy}
                actions={<>
                  {isPublic && collectionPlatform(candidate) === "instagram" && <button type="button" className="formal-button compact" disabled={disabled(`review-split-${candidate.id}`)} onClick={() => void addCandidateToSplit(candidate, run)}><Plus size={14} />{splitUsernames.has(candidate.username.replace(/^@/, "").toLowerCase()) ? "已加入分裂号" : "加入分裂号"}</button>}
                  {(<button className="formal-button compact" title={(isPublic && !greetingMessages.ok ? greetingMessages.error : undefined)} disabled={collectionPlatform(candidate) !== "instagram" || !selectedWindows.size || (isPublic && !greetingMessages.ok) || deleteBusy || disabled(`manual-${operation}-${candidate.id}`)} onClick={() => void manualAction(candidate)}><Play size={14} />单个执行</button>)}

                  <button className="formal-button compact danger" disabled={deleteBusy || disabled(`approved-dismiss-${candidate.id}`)} onClick={() => void run(`approved-dismiss-${candidate.id}`, (client) => client.dismissApprovedCandidate(candidate.id), "账号已从合格区移除；全局去重记录仍保留")}><Trash2 size={14} />删除</button>
                </>}
             />)}
              {!visibleApproved.length ? <EmptyState title={`暂无${isPublic ? "公开" : "私密"}合格账号`} /> : null}
            </div>
          </div>
        </Panel>
        {(<><div className="action-window-column">
          <WindowSelector windows={snapshot.windows} selected={selectedWindows} setSelected={setSelectedWindows} setSelectedOrder={setSelectedWindowOrder} run={run} disabled={disabled} />
        </div>
        <div className="action-runtime-column">
          <Panel icon={<Clock3 size={19} />} title="执行设置"  className="action-settings-panel">
            <div className="formal-panel-body">
              <label className="formal-field"><span>大概间隔时间（秒）</span><span className="formal-range"><input className="formal-input" type="number" min="1" value={intervalMin} onChange={(event) => setIntervalMin(event.target.value)} /><span>至</span><input className="formal-input" type="number" min="1" value={intervalMax} onChange={(event) => setIntervalMax(event.target.value)} /></span></label>
              {isPublic ? <label className="formal-field" style={{ marginTop: 12 }}><span>打招呼话术（最多 {MAX_GREETING_MESSAGE_CHARACTERS} 字符/条）</span><textarea className="formal-textarea" aria-invalid={!greetingMessages.ok} aria-describedby="greeting-message-validation" value={messages} onChange={(event) => setMessages(event.target.value)} placeholder="输入至少一条打招呼内容" /><small id="greeting-message-validation" className={`formal-message-validation ${greetingMessages.ok ? "is-valid" : "is-error"}`} role={greetingMessages.ok ? undefined : "alert"}>{greetingMessages.ok ? `${greetingMessages.messages.length} 条有效话术` : greetingMessages.error}</small></label> : null}
              <button className="formal-button primary" title={isPublic && !greetingMessages.ok ? greetingMessages.error : undefined} style={{ width: "100%", marginTop: 14 }} disabled={!selectedAccounts.size || approved.some(candidate => selectedAccounts.has(candidate.id) && collectionPlatform(candidate) !== "instagram") || !selectedWindows.size || (isPublic && !greetingMessages.ok) || deleteBusy || disabled(`campaign-start-${operation}`)} onClick={() => void startCampaign()}><Play size={17} />启动自动{isPublic ? "打招呼" : "点关注"}</button>
            </div>
          </Panel>
          <div className="action-runtime-section">
            <CampaignList title="正在执行列表" campaigns={active} run={run} disabled={disabled} actionControls={actionControls} />
          </div>
        </div></>)}
      </div>
      {(<><div className="action-bottom-list">
        <FailureList campaigns={failures} dismiss={dismissFailure} dismissAll={dismissAllOrdinaryFailures} resolveUnknown={resolveUnknown} disabled={disabled} operation={operation} />
      </div>
      <ActionHistory campaigns={historyActions} windows={snapshot.windows} operation={operation} /></>)}
    </>
  );
}

type SuccessfulActionRow = {
  id: string;
  campaignId: string;
  targetId: string;
  username: string;
  operation: CoreActionOperation;
  profileId: string;
  completedAt: unknown;
  confirmationStatus: "confirmed" | "already_done";
  evidence: "attempt" | "target";
  message: string | null;
};

function successfulActionRows(campaigns: CoreActionCampaign[]): SuccessfulActionRow[] {
  // The Core ledger is unique by operation + normalized username, not by
  // campaign.  Keeping that same identity here prevents a later reconciled
  // campaign from rendering the immutable success a second time.
  const rows = new Map<string, SuccessfulActionRow>();
  const addRow = (row: SuccessfulActionRow) => {
    const key = `${row.operation}:${row.username.replace(/^@/, "").trim().toLocaleLowerCase()}`;
    const existing = rows.get(key);
    if (!existing) {
      rows.set(key, row);
      return;
    }
    // Attempt evidence carries the actual successful window.  A confirmed
    // attempt is stronger than an `already_done` reconciliation.  For equal
    // evidence retain the earliest immutable-ledger completion.
    const evidenceRank = (item: SuccessfulActionRow) => (item.evidence === "attempt" ? 2 : 1);
    const statusRank = (item: SuccessfulActionRow) => (item.confirmationStatus === "confirmed" ? 2 : 1);
    const shouldReplace = evidenceRank(row) > evidenceRank(existing)
      || (evidenceRank(row) === evidenceRank(existing) && statusRank(row) > statusRank(existing))
      || (evidenceRank(row) === evidenceRank(existing)
        && statusRank(row) === statusRank(existing)
        && String(row.completedAt || "").localeCompare(String(existing.completedAt || "")) < 0);
    if (shouldReplace) rows.set(key, row);
  };
  for (const campaign of campaigns) {
    const targets = Array.isArray(campaign.targets) ? campaign.targets : [];
    const targetById = new Map(targets.map((target) => [target.id || target.username, target]));
    const attempts = Array.isArray(campaign.attempts) ? campaign.attempts.map(asRecord) : [];
    for (const target of targets) {
      const targetId = target.id || target.username;
      const successfulAttempt = [...attempts].reverse().find((attempt) => {
        const attemptTarget = firstText(attempt, ["target_id"], "");
        const attemptUsername = firstText(attempt, ["username", "target", "target_username"], "").replace(/^@/, "");
        return ACTION_SUCCESS_STATES.has(normalizeStatus(attempt.status))
          && (attemptTarget === targetId || attemptUsername === target.username.replace(/^@/, ""));
      });
      const targetStatus = normalizeStatus(target.status);
      if (!ACTION_SUCCESS_STATES.has(targetStatus) && !successfulAttempt) continue;
      // `already_done` without a successful attempt means another campaign's
      // ledger row won a race.  This campaign did not execute the account and
      // therefore must not claim its window in history.
      if (targetStatus === "already_done" && !successfulAttempt) continue;
      const targetRecord = asRecord(target);
      const confirmationStatus = successfulAttempt
        ? normalizeStatus(successfulAttempt.status)
        : targetStatus;
      addRow({
        id: firstText(successfulAttempt || {}, ["id"], `${campaign.id}:${targetId}`),
        campaignId: campaign.id,
        targetId,
        username: target.username,
        operation: campaign.operation,
        profileId: campaign.profile_id,
        completedAt: firstText(successfulAttempt || {}, ["confirmed_at", "finished_at", "completed_at", "updated_at", "created_at"], "")
          || firstText(targetRecord, ["confirmed_at", "completed_at", "updated_at"], "")
          || campaign.updated_at
          || campaign.created_at,
        confirmationStatus: confirmationStatus === "already_done" ? "already_done" : "confirmed",
        evidence: successfulAttempt ? "attempt" : "target",
        message: resolveSuccessfulGreetingMessage(
          campaign.operation,
          successfulAttempt,
          campaign.message,
        ),
      });
    }
    for (const attempt of attempts) {
      if (!ACTION_SUCCESS_STATES.has(normalizeStatus(attempt.status))) continue;
      const targetId = firstText(attempt, ["target_id"], firstText(attempt, ["username", "target", "target_username"], ""));
      if (!targetId) continue;
      const target = targetById.get(targetId);
      const username = firstText(attempt, ["username", "target", "target_username"], target?.username || "");
      // Snapshot attempts intentionally do not invent usernames.  When the
      // matching target detail was truncated there is not enough evidence to
      // render an account row safely.
      if (!username) continue;
      const attemptStatus = normalizeStatus(attempt.status);
      addRow({
        id: firstText(attempt, ["id"], `${campaign.id}:${targetId}`),
        campaignId: campaign.id,
        targetId,
        username,
        operation: campaign.operation,
        profileId: campaign.profile_id,
        completedAt: firstText(attempt, ["confirmed_at", "finished_at", "completed_at", "updated_at", "created_at"], "") || campaign.updated_at || campaign.created_at,
        confirmationStatus: attemptStatus === "already_done" ? "already_done" : "confirmed",
        evidence: "attempt",
        message: resolveSuccessfulGreetingMessage(
          campaign.operation,
          attempt,
          campaign.message,
        ),
      });
    }
  }
  return [...rows.values()].sort((left, right) => String(right.completedAt || "").localeCompare(String(left.completedAt || "")));
}

function countSuccessfulTargets(campaigns: CoreActionCampaign[]): number {
  return successfulActionRows(campaigns).length;
}

function CampaignList({ title, campaigns, run, disabled, actionControls }: { title: string; campaigns: CoreActionCampaign[]; run: WorkspaceProps["run"]; disabled: WorkspaceProps["disabled"]; actionControls: ActionControls }) {
  return (
    <Panel icon={<CirclePlay size={19} />} title={title} actions={<span className="formal-chip">{campaigns.length} 个任务</span>}>
      <div className="formal-panel-body formal-scroll short">
        <div className="formal-list">
          {campaigns.map((campaign) => {
            const state = normalizeStatus(campaign.status);
            const key = `campaign-${campaign.id}`;
            return (
              <div key={campaign.id} style={{ display: "grid", gap: 8 }}>
                <div className="formal-row">
                  <div className="formal-row-main">
                    <strong>{campaign.operation === "greet" ? "自动打招呼" : "自动点关注"}</strong>
                    <small>{campaign.targets_truncated === true ? `已返回 ${campaign.targets?.length ?? 0} 个账号，明细已截断` : `${campaign.targets?.length ?? 0} 个账号`}</small>
                  </div>
                  <StatusBadge status={campaign.status} />
                  <div className="formal-row-actions">
                    {RESUMABLE_STATES.has(state) ? <button className="formal-button compact success" disabled={disabled(key)} onClick={() => void run(key, (client) => client.controlActionCampaign(campaign.id, "resume"), "整组任务已继续")}><Play size={14} />全部继续</button> : <button className="formal-button compact warning" disabled={actionControls.feedback(key).safetyControlDisabled} onClick={() => void actionControls.run(key, { scope: "campaign", campaignId: campaign.id, action: "pause" }, "整组任务已暂停")}><Pause size={14} />全部暂停</button>}
                    <button className="formal-button compact danger" disabled={actionControls.feedback(key).safetyControlDisabled} onClick={() => void actionControls.run(key, { scope: "campaign", campaignId: campaign.id, action: "stop" }, "整组任务已取消并转入失败列表")}><Square size={14} />全部取消</button>
                  </div>
                </div>
                {(campaign.targets || []).filter((target) => {
                  const targetState = normalizeStatus(target.status);
                  return !SUCCESS_STATES.has(targetState) && !FAILURE_TARGET_STATES.has(targetState);
                }).map((target) => {
                  const targetId = target.id || target.username;
                  const targetState = normalizeStatus(target.status);
                  const targetKey = `campaign-target-${campaign.id}-${targetId}`;
                  return (
                    <div className="formal-row" key={targetKey} style={{ marginLeft: 14 }}>
                      <div className="formal-row-main"><strong>@{target.username.replace(/^@/, "")}</strong>{target.last_error ? <small>{target.last_error}</small> : null}</div>
                      <StatusBadge status={target.status} />
                      <div className="formal-row-actions">
                        {PAUSED_STATES.has(targetState) ? <button className="formal-button compact success" disabled={disabled(targetKey)} onClick={() => void run(targetKey, (client) => client.controlActionTarget(campaign.id, targetId, "resume"), "单账号任务已继续")}><Play size={14} />继续</button> : <button className="formal-button compact warning" disabled={!target.id || actionControls.feedback(targetKey).safetyControlDisabled} onClick={() => void actionControls.run(targetKey, { scope: "target", campaignId: campaign.id, targetId: target.id || "", action: "pause" }, "单账号任务已暂停")}><Pause size={14} />暂停</button>}
                        <button className="formal-button compact danger" disabled={!target.id || actionControls.feedback(targetKey).safetyControlDisabled} onClick={() => void actionControls.run(targetKey, { scope: "target", campaignId: campaign.id, targetId: target.id || "", action: "cancel" }, "单账号任务已取消并转入失败列表")}><Trash2 size={14} />取消</button>
                      </div>
                    </div>
                  );
                })}
              </div>
            );
          })}
          {!campaigns.length ? <EmptyState title="当前没有执行任务" /> : null}
        </div>
      </div>
    </Panel>
  );
}

function FailureList({
  campaigns,
  dismiss,
  dismissAll,
  resolveUnknown,
  disabled,
  operation,
}: {
  campaigns: CoreActionCampaign[];
  dismiss: (campaignId: string, targetId: string) => Promise<boolean>;
  dismissAll: (items: Array<{ campaignId: string; targetId: string }>) => Promise<boolean>;
  resolveUnknown: (campaignId: string, targetId: string, outcome: "not_completed" | "completed") => Promise<boolean>;
  disabled: WorkspaceProps["disabled"];
  operation: CoreActionOperation;
}) {
  const failures = campaigns.flatMap((campaign) => {
    const targets = Array.isArray(campaign.targets) ? campaign.targets.filter((target) => {
      const state = normalizeStatus(target.status);
      return FAILURE_TARGET_STATES.has(state);
    }) : [];
    return targets.map((target) => {
      const targetId = target.id || target.username;
      // The target's current durable status is authoritative. A previous UNKNOWN
      // attempt may remain in immutable audit history after the user cancels or
      // resolves the target; using that old attempt made an ordinary FAILED card
      // show unknown-only buttons that the backend correctly rejected.
      const isUnknown = normalizeStatus(target.status) === "unknown";
      return { campaign, targetId, username: target.username, error: target.last_error, isUnknown };
    });
  });
  const ordinaryFailures = failures.filter((failure) => !failure.isUnknown);
  const bulkKey = `failure-dismiss-all-${operation}`;

  function confirmDismissAll() {
    if (!ordinaryFailures.length) return;
    if (!window.confirm(`确认将 ${ordinaryFailures.length} 条普通失败记录全部删除，并把未完成账号退回合格列表吗？结果未知项不会被处理。`)) return;
    void dismissAll(ordinaryFailures.map(({ campaign, targetId }) => ({ campaignId: campaign.id, targetId })));
  }

  function confirmUnknown(campaignId: string, targetId: string, username: string, outcome: "not_completed" | "completed") {
    const message = outcome === "completed"
      ? `二次确认：确定 @${username.replace(/^@/, "")} 已经执行成功吗？确认后会写入永久成功历史，不能再按未执行退回。`
      : `二次确认：确定 @${username.replace(/^@/, "")} 实际没有执行吗？确认后会退回合格区，等待重新选择执行。`;
    if (!window.confirm(message)) return;
    void resolveUnknown(campaignId, targetId, outcome);
  }

  return (
    <Panel
      icon={<AlertTriangle size={19} />}
      title="失败列表"

      actions={<button className="formal-button compact danger" disabled={!ordinaryFailures.length || disabled(bulkKey)} onClick={confirmDismissAll}><Trash2 size={14} />一键删除并退回</button>}
    >
      <div className="formal-panel-body formal-scroll short">
        <div className="formal-list">
          {failures.map(({ campaign, targetId, username, error, isUnknown }) => {
            const key = `failure-dismiss-${campaign.id}-${targetId}`;
            return (
              <div className={`formal-row ${isUnknown ? "formal-row-unknown" : ""}`} key={key}>
                <div className="formal-row-main">
                  <strong>@{username.replace(/^@/, "")}</strong>
                  <small>{error || (isUnknown ? "Core 无法确认操作是否真正完成" : "异常终止或已取消")}</small>
                  {isUnknown ? <small className="formal-risk-text">结果未知：禁止普通删除。请先在 Instagram / BitBrowser 中核对真实状态，再选择下方结果。</small> : null}
                </div>
                {isUnknown ? <div className="formal-row-actions formal-unknown-actions">
                  <button className="formal-button compact warning" disabled={disabled(`unknown-resolve-not_completed-${campaign.id}-${targetId}`)} onClick={() => confirmUnknown(campaign.id, targetId, username, "not_completed")}><RotateCcw size={14} />确认未执行，退回待执行</button>
                  <button className="formal-button compact success" disabled={disabled(`unknown-resolve-completed-${campaign.id}-${targetId}`)} onClick={() => confirmUnknown(campaign.id, targetId, username, "completed")}><Check size={14} />确认已执行，记为成功</button>
                </div> : <button className="formal-button compact danger" disabled={disabled(key)} onClick={() => void dismiss(campaign.id, targetId)}><Trash2 size={14} />删除并回退</button>}
              </div>
            );
          })}
          {!failures.length ? <EmptyState title="当前没有失败任务" /> : null}
        </div>
      </div>
    </Panel>
  );
}

function useHistoryPage<T>(rows: readonly T[]) {
  const [requestedPage, setPage] = useState(0);
  const page = useMemo(() => historyPage(rows, requestedPage), [rows, requestedPage]);
  return { ...page, setPage };
}

function HistoryPagination({ label, page }: { label: string; page: ReturnType<typeof useHistoryPage> }) {
  if (page.total <= HISTORY_RENDER_PAGE_SIZE) return null;
  return <nav className="formal-toolbar" aria-label={`${label}翻页`}>
    <button className="formal-button compact" disabled={page.page === 0} onClick={() => page.setPage(page.page - 1)}>上一页</button>
    <span className="formal-muted">第 {page.page + 1} / {page.pages} 页 · 已载入 {formatCount(page.total)} 条</span>
    <button className="formal-button compact" disabled={page.page + 1 >= page.pages} onClick={() => page.setPage(page.page + 1)}>下一页</button>
  </nav>;
}

function ActionHistory({ campaigns, windows, operation }: { campaigns: CoreActionCampaign[]; windows: CoreBitBrowserWindow[]; operation: CoreActionOperation }) {
  const windowName = useMemo(() => new Map(windows.map((window) => [window.id, window.name])), [windows]);
  const rows = useMemo(() => successfulActionRows(campaigns).filter((row) => row.operation === operation), [campaigns, operation]);
  const page = useHistoryPage(rows);
  const [open, setOpen] = useState(false);
  return (
    <details className="formal-panel formal-history-details" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
      <summary className="formal-panel-header">
        <div className="formal-panel-title"><span><History size={19} /></span><div><h2>任务历史记录</h2></div></div>
        <span className="formal-chip">{rows.length} 条 <ChevronDown size={15} /></span>
      </summary>
      {open && <><HistoryPagination label="任务历史记录" page={page} /><div className="formal-table-scroll">
        <table className="formal-history-table">
          <thead><tr><th>完成时间</th><th>账号</th><th>操作类型</th><th>成功窗口</th>{operation === "greet" ? <th>实际话术</th> : null}<th>结果</th></tr></thead>
          <tbody>
            {page.items.map((row) => <tr key={`${row.campaignId}:${row.targetId}`}><td>{formatTime(row.completedAt)}</td><td>@{row.username.replace(/^@/, "")}</td><td>{row.operation === "greet" ? "自动打招呼" : "自动点关注"}</td><td>{windowName.get(row.profileId) || row.profileId}</td>{operation === "greet" ? <td className="formal-history-message">{row.message || "—"}</td> : null}<td><span className="formal-status success">已确认成功</span></td></tr>)}
            {!rows.length ? <tr><td colSpan={operation === "greet" ? 6 : 5}>暂无成功历史记录</td></tr> : null}
          </tbody>
        </table>
      </div></>}
    </details>
  );
}

function HistoryWorkspace({ snapshot }: { snapshot: CoreWorkbenchSnapshot }) {
  const rejections = useMemo(() => [...snapshot.history.manual_rejections].sort((a, b) => String(b.reviewed_at || b.rejected_at || "").localeCompare(String(a.reviewed_at || a.rejected_at || ""))), [snapshot.history.manual_rejections]);
  const exclusions = useMemo(() => [...snapshot.history.collection_exclusions].sort((a, b) => String(b.excluded_at || b.created_at || "").localeCompare(String(a.excluded_at || a.created_at || ""))), [snapshot.history.collection_exclusions]);
  const tasks = useMemo(() => {
    const taskMap = new Map<string, CoreHistoryRecord>();
    for (const task of Array.isArray(snapshot.history.tasks) ? snapshot.history.tasks : []) taskMap.set(task.id, task);
    for (const task of snapshot.tasks) taskMap.set(task.id, task as unknown as CoreHistoryRecord);
    return [...taskMap.values()]
      .filter((task) => SUCCESS_STATES.has(normalizeStatus(task.task_status || task.status)) || FAILED_STATES.has(normalizeStatus(task.task_status || task.status)))
      .sort((a, b) => String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || "")));
  }, [snapshot.history.tasks, snapshot.tasks]);
  const actions = useMemo(() => mergedCampaignHistory(snapshot)
    .filter((campaign) => successfulActionRows([campaign]).length > 0)
    .sort((a, b) => String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || ""))), [snapshot.history.actions, snapshot.campaigns]);
  const approvals = useMemo(() => {
    const approvalMap = new Map<string, CoreApprovalHistoryCandidate>();
    for (const candidate of [...snapshot.approved.public, ...snapshot.approved.private]) approvalMap.set(candidate.id, candidate);
    for (const candidate of approvalHistoryOf(snapshot)) approvalMap.set(candidate.id, candidate);
    return [...approvalMap.values()].sort((a, b) => String(b.decided_at || b.reviewed_at || b.updated_at || "").localeCompare(String(a.decided_at || a.reviewed_at || a.updated_at || "")));
  }, [snapshot.approved.public, snapshot.approved.private, snapshot.history.approvals]);
  const rejectionPage = useHistoryPage(rejections);
  const exclusionPage = useHistoryPage(exclusions);
  const approvalPage = useHistoryPage(approvals);
  const retainedHistory = retainedHistoryOf(snapshot.storage);
  const rejectedTotal = snapshot.counts.rejected ?? rejections.length;
  const excludedTotal = snapshot.counts.collection_excluded ?? exclusions.length;
  const approvedTotal = retainedHistory.approved ?? approvals.length;
  return (
    <>
      <HistoryTotals />
      <div className="formal-grid-3">
        <Panel icon={<X size={19} />} title="人工审核不合格"  actions={<span className="formal-chip">显示 {formatCount(rejections.length)} / 总数 {formatCount(rejectedTotal)}</span>}>
          <HistoryPagination label="人工审核不合格" page={rejectionPage} />
          <div className="formal-panel-body formal-scroll tall"><div className="formal-list">
            {rejectionPage.items.map((record) => {
              const profile = asRecord(record.profile);
              const candidate: CoreCandidate = { id: record.id, username: record.username, visibility: record.visibility || "unknown", profile, screening: {} };
              return <CandidateRow key={record.id} candidate={candidate} actions={<span className="formal-muted">{formatTime(record.reviewed_at || record.rejected_at)}</span>} />;
            })}
            {!rejections.length ? <EmptyState title="没有人工审核不合格记录" /> : null}
          </div></div>
        </Panel>
        <Panel icon={<Archive size={19} />} title="采集阶段已排除"  actions={<span className="formal-chip">显示 {formatCount(exclusions.length)} / 总数 {formatCount(excludedTotal)}</span>}>
          <HistoryPagination label="采集阶段已排除" page={exclusionPage} />
          <div className="formal-panel-body formal-scroll tall"><div className="formal-list">
            {exclusionPage.items.map((record) => {
              return <div className="formal-row" key={record.id}><span className="formal-stat-icon"><Archive size={18} /></span><div className="formal-row-main"><strong>@{record.username.replace(/^@/, "")}</strong><small className="formal-exclusion-reason">{collectionExclusionReason(record)}</small><small>{formatTime(record.excluded_at || record.created_at)}</small></div></div>;
            })}
            {!exclusions.length ? <EmptyState title="没有采集排除记录" /> : null}
          </div></div>
        </Panel>
        <Panel icon={<ShieldCheck size={19} />} title="审核合格记录"  actions={<span className="formal-chip">显示 {formatCount(approvals.length)} / 总数 {formatCount(approvedTotal)}</span>}>
          <HistoryPagination label="审核合格记录" page={approvalPage} />
          <div className="formal-panel-body formal-scroll tall"><div className="formal-list">
            {approvalPage.items.map((candidate) => <CandidateRow key={candidate.id} candidate={candidate} actions={<><span className="formal-status success">{candidate.visibility === "private" ? "私密" : "公开"}</span>{candidate.dismissed_at || candidate.actionable === false ? <span className="formal-status">已从执行区移除</span> : null}<span className="formal-muted">{formatTime(candidate.decided_at || candidate.reviewed_at || candidate.updated_at)}</span></>} />)}
            {!approvals.length ? <EmptyState title="没有审核合格记录" /> : null}
          </div></div>
        </Panel>
      </div>
      <ImmutableOperationalHistory tasks={tasks} actions={actions} windows={snapshot.windows} />
    </>
  );
}

function ImmutableOperationalHistory({ tasks, actions, windows }: { tasks: CoreHistoryRecord[]; actions: CoreActionCampaign[]; windows: CoreBitBrowserWindow[] }) {
  const windowName = useMemo(() => new Map(windows.map((window) => [window.id, window.name])), [windows]);
  const rows = useMemo<OperationalHistoryRow[]>(() => [
    ...collectionOperationalHistoryRows(tasks, windowName),
    ...successfulActionRows(actions).map((action) => ({ id: `action-${action.campaignId}-${action.targetId}`, time: action.completedAt, type: action.operation, target: `@${action.username.replace(/^@/, "")}`, window: windowName.get(action.profileId) || action.profileId, message: action.message, status: "confirmed" })),
  ].sort((a, b) => String(b.time || "").localeCompare(String(a.time || ""))), [tasks, actions, windowName]);
  const groups = useMemo(() => operationalHistoryGroups(rows), [rows]);
  const [selectedType, setSelectedType] = useState<string | null>(null);
  const selected = groups.find(group => group.id === selectedType) || groups.find(group => group.rows.length) || groups[0];
  const page = useHistoryPage(selected.rows);
  const isCollection = selected.id === "collection";
  const showMessage = selected.id === "greet";
  const columnCount = isCollection ? 7 : selected.id === "other" || showMessage ? 5 : 4;
  return (
    <Panel icon={<FileClock size={19} />} title="任务与执行历史" actions={<span className="formal-chip">已载入 {formatCount(rows.length)} 条</span>}>
      <div className="formal-panel-body">
        <div className="formal-toolbar" role="group" aria-label="按任务类型分类">
          {groups.map(group => <button key={group.id} type="button" className={`formal-button compact ${selected.id === group.id ? "primary" : ""}`} aria-pressed={selected.id === group.id} aria-controls="operational-history-table" onClick={() => { setSelectedType(group.id); page.setPage(0); }}>{group.label} · {formatCount(group.rows.length)}</button>)}
        </div>
        {isCollection ? <p className="formal-muted">按目标账号、粉丝 / 关注分别列出已载入记录；缺失数据以“—”显示{tasks.some(task => task.targets_truncated === true) ? "，部分目标明细尚未载入" : ""}</p> : null}
        <HistoryPagination label={`${selected.label}历史`} page={page} />
      </div>
      <div className="formal-table-scroll"><table id="operational-history-table" className="formal-history-table" aria-label={`${selected.label}历史`}><thead><tr><th>完成时间</th>{selected.id === "other" ? <th>类型</th> : null}<th>{isCollection ? "目标账号" : "账号"}</th><th>{isCollection ? "窗口" : "执行成功窗口"}</th>{isCollection ? <><th>粉丝 / 关注</th><th title="本来源、本模式已处理完成的账号数，含去重跳过；不使用已识别数">采集完成数量</th><th title="实际通过采集筛选并进入审核的账号数">合格数量</th></> : showMessage ? <th>实际话术</th> : null}<th>状态</th></tr></thead><tbody>
        {page.items.map((row) => <tr key={row.id}><td>{formatTime(row.time)}</td>{selected.id === "other" ? <td title={row.type}>{operationalHistoryTypeLabel(row.type)}</td> : null}<td>{row.target}</td><td>{row.window || "—"}</td>{isCollection ? <><td>{row.mode}</td><td>{formatCount(row.processed)}</td><td>{formatCount(row.qualified)}</td></> : showMessage ? <td className="formal-history-message">{row.message || "—"}</td> : null}<td><StatusBadge status={row.status} /></td></tr>)}
        {!selected.rows.length ? <tr><td colSpan={columnCount}>暂无{selected.label}历史</td></tr> : null}
      </tbody></table></div>
    </Panel>
  );
}

function SettingsWorkspace({ snapshot, run, disabled }: WorkspaceProps) {
  const { logout } = useAuth();
  const [port, setPort] = useState("");
  const [apiKey, setApiKey] = useState("");
  const connection = snapshot.connection || {};
  const storage = snapshot.storage || {};
  const retainedHistory = retainedHistoryOf(snapshot.storage);
  // approved_dismissed is a subset of approved, so it is shown separately
  // below but must not be added to the retained-history headline twice.
  const retainedHistoryTotal = ["approved", "action_successes", "manual_rejections", "collection_exclusions"]
    .reduce((total, key) => total + (retainedHistory[key] || 0), 0);
  const sqliteStorageBytes = (
    typeof storage.database_bytes === "number"
    || typeof storage.wal_bytes === "number"
  )
    ? (typeof storage.database_bytes === "number" ? storage.database_bytes : 0)
      + (typeof storage.wal_bytes === "number" ? storage.wal_bytes : 0)
    : null;
  const version = typeof snapshot.version === "string" ? snapshot.version : snapshot.version?.app || "—";

  async function saveIntegration(event: FormEvent) {
    event.preventDefault();
    const parsed = Number.parseInt(port, 10);
    if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > 65535) return;
    const ok = await run("settings-integration", (client) => client.configureIntegrations({ bitbrowserPort: parsed, ...(apiKey.trim() ? { bitbrowserApiKey: apiKey.trim() } : {}) }), "BitBrowser 设置已保存，Core 正在重启连接");
    if (ok) setApiKey("");
  }

  return (
    <>
      <div className="formal-stats">
        <StatCard icon={<Monitor size={22} />} label="BitBrowser 窗口" value={snapshot.windows.length} />
        <StatCard icon={<Database size={22} />} label="全局去重数量" value={snapshot.dedupe.total} />
        <StatCard icon={<Archive size={22} />} label="SQLite + WAL 占用（字节）" value={sqliteStorageBytes} />
        <StatCard icon={<History size={22} />} label="保留历史记录" value={retainedHistoryTotal} />
      </div>
      <ReviewAccountSettings/><MessageNotificationSettings/><StorageManagementSettings/>
      <div className="formal-grid-2">
        <Panel icon={connection.connected ? <Wifi size={19} /> : <WifiOff size={19} />} title="BitBrowser V2 连接" >
          <div className="formal-panel-body">
            <div className="formal-evidence-grid">
              <div className="formal-evidence-item"><span>连接状态</span><strong>{connection.connected ? "已连接" : connection.state || connection.phase || "未连接"}</strong></div>
              <div className="formal-evidence-item"><span>接口地址</span><strong className="formal-code">{typeof connection.endpoint === "string" ? connection.endpoint : "—"}</strong></div>
              <div className="formal-evidence-item"><span>状态详情</span><strong>{typeof connection.detail === "string" ? connection.detail : typeof connection.reason === "string" ? connection.reason : "—"}</strong></div>
              <div className="formal-evidence-item"><span>应用版本</span><strong>{version}{snapshot.source_revision ? ` · ${snapshot.source_revision}` : ""}</strong></div>
            </div>
            <div className="formal-toolbar" style={{ marginTop: 13 }}>
              <button className="formal-button" disabled={disabled("settings-refresh")} onClick={() => void run("settings-refresh", (client) => client.refreshBitBrowserWindows(), "连接状态已刷新")}><RefreshCw size={16} />刷新</button>
              <button className="formal-button primary" disabled={disabled("settings-reconnect")} onClick={() => void run("settings-reconnect", (client) => client.reconnectBitBrowser(), "已检查连接并唤醒等待恢复的任务；已暂停的任务请点击继续")}><RotateCcw size={16} />重新连接</button>
            </div>
          </div>
        </Panel>
        <Panel icon={<Settings size={19} />} title="连接参数" >
          <form className="formal-panel-body" onSubmit={(event) => void saveIntegration(event)}>
            <label className="formal-field"><span>BitBrowser API 端口</span><input className="formal-input" type="number" min="1" max="65535" value={port} onChange={(event) => setPort(event.target.value)} placeholder="输入 BitBrowser API 端口" /></label>
            <label className="formal-field" style={{ marginTop: 12 }}><span>BitBrowser API Key（未修改可留空）</span><input className="formal-input" type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="输入 API Key" /></label>
            <button className="formal-button primary" type="submit" style={{ width: "100%", marginTop: 14 }} disabled={!port || disabled("settings-integration")}><Check size={16} />保存并重连</button>
          </form>
        </Panel>
        <Panel icon={<Database size={19} />} title="SQLite 存储与预览缓存" >
          <div className="formal-panel-body">
            <div className="formal-evidence-grid">
              <div className="formal-evidence-item"><span>SQLite 数据库</span><strong>{formatBytes(storage.database_bytes)}</strong></div>
              <div className="formal-evidence-item"><span>WAL 日志</span><strong>{formatBytes(storage.wal_bytes)}</strong></div>
              <div className="formal-evidence-item"><span>可安全清理预览</span><strong>{formatBytes(storage.cache_bytes)}</strong></div>
              <div className="formal-evidence-item"><span>审核中预览（保留）</span><strong>{formatBytes(storage.pending_preview_bytes)}</strong></div>
              <div className="formal-evidence-item"><span>磁盘可用空间</span><strong>{formatBytes(storage.disk_free_bytes)}</strong></div>
              <div className="formal-evidence-item"><span>磁盘总容量</span><strong>{formatBytes(storage.disk_total_bytes)}</strong></div>
              <div className="formal-evidence-item"><span>上次自动维护</span><strong>{formatTime(storage.last_maintenance_at ?? storage.last_cleanup_at)}</strong></div>
              <div className="formal-evidence-item"><span>审核合格历史</span><strong>{formatCount(retainedHistory.approved)}</strong></div>
              <div className="formal-evidence-item"><span>已移出执行区</span><strong>{formatCount(retainedHistory.approved_dismissed)}</strong></div>
              <div className="formal-evidence-item"><span>自动执行成功</span><strong>{formatCount(retainedHistory.action_successes)}</strong></div>
              <div className="formal-evidence-item"><span>人工审核不合格</span><strong>{formatCount(retainedHistory.manual_rejections)}</strong></div>
              <div className="formal-evidence-item"><span>采集阶段排除</span><strong>{formatCount(retainedHistory.collection_exclusions)}</strong></div>
              <div className="formal-evidence-item"><span>全局去重记录</span><strong>{formatCount(retainedHistory.global_dedupe)}</strong></div>
            </div>
            {storage.low_space_warning === true ? (
              <div className="formal-capacity-banner formal-storage-space-warning" role="alert">
                <AlertTriangle size={17} />
                <div className="formal-capacity-copy">
                  <strong>本机磁盘空间不足</strong>
                  <span>当前可用 {formatBytes(storage.disk_free_bytes)}，低于安全提示线 {formatBytes(storage.low_space_threshold_bytes)}。请先释放磁盘空间，避免全天采集因数据库无法写入而中断。</span>
                </div>
              </div>
            ) : null}
            <button
              className="formal-button danger"
              style={{ marginTop: 14 }}
              disabled={disabled(STORAGE_CACHE_CLEAR_BUSY_KEY)}
              onClick={() => void run(
                STORAGE_CACHE_CLEAR_BUSY_KEY,
                (client) => client.clearStorageCache(),
                storageCleanupNotice,
              )}
            >
              <Trash2 size={16} />仅清理预览缓存
            </button>

          </div>
        </Panel>
        <Panel icon={<ShieldCheck size={19} />} title="会话与版本" >
          <div className="formal-panel-body">
            <div className="formal-evidence-item"><span>版本信息</span><strong className="formal-code">{typeof snapshot.version === "string" ? snapshot.version : JSON.stringify(snapshot.version || {})}</strong></div>
            {snapshot.source_revision ? <div className="formal-evidence-item"><span>Core 修订版本</span><strong className="formal-code">{snapshot.source_revision}</strong></div> : null}
            <button className="formal-button danger" style={{ marginTop: 14 }} disabled={disabled("settings-logout")} onClick={() => void run("settings-logout", () => logout(), "已退出本机会话")}><ExternalLink size={16} />退出登录</button>
          </div>
        </Panel>
      </div>
    </>
  );
}

function FollowMonitorWorkspace({ snapshot, disabled, refreshWindows }: WorkspaceProps & {refreshWindows:()=>Promise<unknown>}) {
  const client = usePlatformCore();
  const [monitor, setMonitor] = useState<CoreFollowMonitorSnapshot | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [resultQuery,setResultQuery]=useState("");
  const [resultWindow,setResultWindow]=useState("");
  const [resultKind,setResultKind]=useState("all");
  const matchesResult=(item:{profile_id:string;username?:string;owner_username?:string;result_kind?:string})=>(!resultWindow||item.profile_id===resultWindow)&&(!resultQuery.trim()||[item.username,item.owner_username].join(" ").toLowerCase().includes(resultQuery.trim().replace(/^@/, "").toLowerCase()))&&(view!=="follow"||resultKind==="all"||(item.result_kind==="repeat_following"?"repeat":"new")===resultKind);
  const [group, setGroup] = useState("all");
  const [concurrency, setConcurrency] = useState(2);
  const [view, setView] = useState<"follow" | "unfollow" | "dm" | "accounts" | "logs">("follow");
  const [monitorError, setMonitorError] = useState<string | null>(null);
  const [monitorReadError, setMonitorReadError] = useState<string | null>(null);
  const [exportingDiagnostic, setExportingDiagnostic] = useState(false);
  const [diagnosticNotice, setDiagnosticNotice] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [awaitingStartSnapshot, setAwaitingStartSnapshot] = useState(false);
  const [controlPending, setControlPending] = useState(false);
  const currentRun = monitor?.run;
  const activeStates = new Set(["running", "paused", "cancelling"]);
  const anyRunning = starting || awaitingStartSnapshot || Object.values(monitor?.runs || {}).some(item => item && activeStates.has(item.status));
  const running = anyRunning;
  const paused = currentRun?.status === "paused";
  const cancelling = currentRun?.status === "cancelling";
  const busyProfiles = new Set(Object.values(monitor?.runs || {}).flatMap(item => item?.active_profile_ids || []));
  const isBusy = (item: typeof snapshot.windows[number]) => busyProfiles.has(item.id)
    || Boolean(item.locked);
  const groups = useMemo(() => [...new Set(snapshot.windows.map(item => String(item.group || "").trim()).filter(Boolean))].sort((a,b) => a.localeCompare(b,"zh-CN")), [snapshot.windows]);
  const visibleWindows = [...snapshot.windows].sort((a,b)=>Number(selected.has(b.id))-Number(selected.has(a.id)) || Number(Boolean(b.opened))-Number(Boolean(a.opened)) || (a.provider_order??a.serial_number??0)-(b.provider_order??b.serial_number??0)).filter(item => (group === "all" || String(item.group || "") === group)
    && (!query.trim() || [item.id, item.name, windowSequenceLabel(item), item.group].join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())));
  const windowNames = new Map(snapshot.windows.map(item => [item.id, item.name]));
  const accountsByProfile = new Map((monitor?.accounts || []).map(item => [item.profile_id,item]));
  const dmByProfile = new Map((monitor?.dm_accounts || []).map(item => [item.profile_id,item]));
  const roundsByProfile = new Map((monitor?.rounds || []).map(item => [item.profile_id,item]));
  const shownLogs = monitor?.logs || [];
  const number = (value: number | null | undefined) => value == null ? "—" : formatCount(value);
  const monitorReaderRef = useRef<ReturnType<typeof createViewSnapshotReader<CoreFollowMonitorSnapshot>> | null>(null);
  const loadMonitor = useCallback((fresh = false) => monitorReaderRef.current?.refresh(fresh) || Promise.resolve(), []);
  useEffect(() => {
    const reader = createViewSnapshotReader({
      read: () => client.followMonitorSnapshot(),
      onValue: (value) => { setMonitor(value); setMonitorReadError(null); setAwaitingStartSnapshot(false); },
      onError: (reason) => setMonitorReadError(reason instanceof Error ? reason.message : String(reason)),
    });
    monitorReaderRef.current = reader;
    void reader.refresh();
    return () => { reader.dispose(); if (monitorReaderRef.current === reader) monitorReaderRef.current = null; };
  }, [client]);
  useEffect(() => {
    if (!anyRunning) return;
    const timer = window.setInterval(() => void loadMonitor(), 2_000);
    return () => window.clearInterval(timer);
  }, [loadMonitor, anyRunning]);
  function toggleWindow(id: string) {
    if (running) return;
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  }
  async function startCheck() {
    const reader = monitorReaderRef.current;
    const eligible = snapshot.windows.filter(item => selected.has(item.id) && !isBusy(item)).map(item => item.id);
    if (!reader || !eligible.length || running) return;
    setStarting(true); setMonitorError(null); setDiagnosticNotice(null);
    try {
      await client.startFollowMonitor(eligible, concurrency, "combined");
      // The run exists after ACK, even if its first snapshot is still queued.
      if (monitorReaderRef.current === reader) setAwaitingStartSnapshot(true);
    }
    catch (reason) { if (monitorReaderRef.current === reader) setMonitorError(reason instanceof Error ? reason.message : String(reason)); }
    finally { if (monitorReaderRef.current === reader) { void reader.refresh(true); setStarting(false); } }
  }
  async function controlCheck(action: "pause" | "resume" | "cancel") {
    const reader = monitorReaderRef.current;
    if (!reader || !currentRun || controlPending || awaitingStartSnapshot) return;
    setControlPending(true); setMonitorError(null);
    try {
      const ack = await client.controlFollowMonitor(currentRun.id, action);
      if (monitorReaderRef.current === reader) setMonitor(previous => {
        if (!previous) return previous;
        // Preserve ownership and results. A terminal state read while the command
        // was in flight must not be resurrected by its delayed ACK.
        const updateRun = (item: CoreFollowMonitorSnapshot["run"]) => item?.id === ack.run_id && activeStates.has(item.status)
          ? { ...item, status: ack.status } : item;
        return { ...previous, run: updateRun(previous.run), runs: {
          combined: updateRun(previous.runs.combined), following: updateRun(previous.runs.following), dm: updateRun(previous.runs.dm),
        } };
      });
    }
    catch (reason) { if (monitorReaderRef.current === reader) setMonitorError(reason instanceof Error ? reason.message : String(reason)); }
    finally { if (monitorReaderRef.current === reader) { void reader.refresh(true); setControlPending(false); } }
  }
  async function exportDiagnostic() {
    setExportingDiagnostic(true);
    setDiagnosticNotice(null);
    try {
      const result = await client.exportFollowMonitorDiagnostics();
      if (!result.reports.length) {
        setDiagnosticNotice("还没有检查诊断，请先选择一个窗口运行检查，结束后再导出。");
        return;
      }
      const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `Juxin-Check-Diagnostic-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
      setDiagnosticNotice("已发起诊断文件下载，请将生成的 JSON 文件发送给技术支持。");
    } catch (reason) {
      setMonitorError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setExportingDiagnostic(false);
    }
  }


  const counts = monitor?.counts || { total:0,month:0,week:0,today:0,repeated:0 };
  const eligibleCount = snapshot.windows.filter(item => selected.has(item.id) && !isBusy(item)).length;
  const emptyText = paused ? "任务已暂停，点击任务继续接着检查。" : cancelling ? "正在取消任务并释放窗口。" : currentRun?.status === "cancelled" ? "本轮已取消，已保存的结果保留。" : currentRun?.status === "failed" ? "本轮检查失败，请查看检查日志。" : currentRun?.status === "partial" ? "本轮部分检查失败，已成功的结果仍保留；请查看上方失败详情。" : running ? "检查进行中，结果将陆续显示。" : "最近一轮没有对应记录。";
  return <>
    <div className="formal-stats">
      <StatCard icon={<UserRoundCheck size={22} />} label="累计新增次数" value={counts.total} />
      <StatCard icon={<Clock3 size={22} />} label="本月新增次数" value={counts.month} />
      <StatCard icon={<Clock3 size={22} />} label="本周新增次数" value={counts.week} />
      <StatCard icon={<Check size={22} />} label="今天新增次数" value={counts.today} />
    </div><p className="formal-muted">累计重复新增 {number(counts.repeated)} 次</p>
    <Panel icon={<Monitor size={19} />} title="选择检查窗口"  actions={<span className="formal-selected-count">已选 {selected.size} 个</span>}>
      <div className="formal-panel-body"><div className="formal-toolbar">
        <label className="formal-field"><span>窗口分类</span><select className="formal-input" value={group} onChange={event => setGroup(event.target.value)} disabled={running}><option value="all">全部分类（{snapshot.windows.length}）</option>{groups.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
        <label className="formal-field formal-grow"><span>搜索窗口名称、ID、序号或分类</span><input className="formal-input" value={query} onChange={event => setQuery(event.target.value)} placeholder="输入关键词" /></label>
        <label className="formal-field follow-monitor-control"><span>同时检查</span><input className="formal-input" type="number" min={1} step={1} value={concurrency} onChange={event => setConcurrency(Math.max(1,Math.trunc(Number(event.target.value))||1))} disabled={running}/></label>
        <button className="formal-button follow-monitor-control-button" onClick={()=>void refreshWindows()}><RefreshCw size={15}/>刷新窗口</button>
        <button className="formal-button follow-monitor-control-button" disabled={running} onClick={() => setSelected(new Set(visibleWindows.filter(item => !isBusy(item)).map(item => item.id)))}>选择当前筛选</button>
        <button className="formal-button follow-monitor-control-button" disabled={running || !selected.size} onClick={() => setSelected(new Set())}>清空</button>
      </div></div>
      <div className="formal-panel-body formal-scroll short"><div className="formal-list">
        {visibleWindows.map(item => {
          const account = accountsByProfile.get(item.id);
          const dm = dmByProfile.get(item.id);
          const activeAccount = account || dm;
          const profileError = currentRun?.errors.find(error => error.profile_id === item.id);
          const checkStatus = profileError ? (profileError.partial ? "partial" : "failed") : activeAccount?.last_status;
          const awaitingFollowingResult = Boolean(monitor?.runs?.following?.profile_ids.includes(item.id)) && !roundsByProfile.has(item.id);
          const awaitingDmResult = Boolean(monitor?.runs?.dm?.profile_ids.includes(item.id)) && dm?.last_run_id !== monitor?.runs?.dm?.id;
          const gap = account?.homepage_count == null ? null : account.homepage_count - account.following_count;
          const metrics: Array<[string, number | null | undefined]> = [
            ["主页关注",account?.homepage_count],["实际读取",account?.following_count],["数量差额",gap],["总新增",account?.total_added_count],["本轮新增",awaitingFollowingResult ? null : account?.last_added_count],["累计待回复检出",dm?.total_dm_count],
            ["上轮主页",account?.previous_homepage_count],["上轮实读",account?.previous_generation ? account.previous_following_count : null],["本轮取关",awaitingFollowingResult ? null : account?.last_unfollow_count],["总取关",account?.total_unfollow_count],["本轮重复新增",awaitingFollowingResult ? null : account?.last_repeat_count],
            ["本轮待回复",awaitingDmResult ? null : dm?.last_dm_count]
          ];
          return <label key={item.id} className={`formal-row follow-monitor-window-row ${selected.has(item.id) ? "is-selected" : ""} ${isBusy(item) ? "is-locked" : ""}`}>
            <span className="formal-check"><input type="checkbox" checked={selected.has(item.id)} disabled={running || isBusy(item)} onChange={() => toggleWindow(item.id)} /></span><Monitor size={20} />
            <span className="formal-row-main"><strong>{item.name}</strong><small>{[windowSequenceLabel(item),item.group].filter(Boolean).join(" · ")}</small><small>{activeAccount ? `@${activeAccount.username}` : "尚未检查"}</small><small>{account ? `已保存第 ${account.generation} 轮` : activeAccount?.checked_at ? formatTime(activeAccount.checked_at) : ""}</small>{account?.last_status === "completed" && account.last_error ? <small>{account.last_error}</small> : null}</span>
            <span className="follow-monitor-window-metrics">{metrics.map(([label,value]) => <span key={label}><small>{label}</small><strong>{number(value)}</strong></span>)}</span>
            <StatusBadge status={isBusy(item) ? (paused ? "paused" : "任务占用") : checkStatus || item.window_state || "可检查"} />
          </label>;
        })}
        {!visibleWindows.length ? <EmptyState title="没有匹配窗口" /> : null}
      </div></div>
      <div className="formal-panel-body formal-toolbar follow-monitor-actions">
        <button className="formal-button primary" disabled={!eligibleCount || running || disabled("follow-monitor-run")} onClick={() => void startCheck()}>{running ? <LoaderCircle className="animate-spin" size={16} /> : <Play size={16} />}{running ? `${paused ? "已暂停" : cancelling ? "正在取消" : "正在检查"} ${currentRun?.processed || 0} / ${currentRun?.profile_ids.length || selected.size}` : `检查已选 ${selected.size} 个窗口`}</button>
        <button className="formal-button warning" disabled={!currentRun || currentRun.status !== "running" || controlPending || starting || awaitingStartSnapshot} onClick={() => void controlCheck("pause")} title="当前页面操作结束后暂停，保留读取位置"><Pause size={16} />任务暂停</button>
        <button className="formal-button success" disabled={!paused || controlPending || starting || awaitingStartSnapshot} onClick={() => void controlCheck("resume")}><Play size={16} />任务继续</button>
        <button className="formal-button danger" disabled={!currentRun || !activeStates.has(currentRun.status) || cancelling || controlPending || starting || awaitingStartSnapshot} onClick={() => void controlCheck("cancel")}><X size={16} />{cancelling ? "正在取消…" : "任务取消"}</button>
        <span className="formal-muted">每个窗口查完关注和私信后立即关闭并释放；只读取“已关注”账号，缺量最多补读一次；不设比例门槛保存实读账号，名单未确认完整时不判定取关。</span>
      </div>
    </Panel>
    {monitorError || monitorReadError ? <div className="formal-error-banner" role="alert">{monitorError || monitorReadError}</div> : null}
    {currentRun?.errors.length ? <div className="formal-error-banner" role="status">{currentRun.errors.slice(0,3).map(item => `${windowNames.get(item.profile_id) || "未知窗口"}：${item.message}`).join("；")}</div> : null}
    <div className="formal-toolbar">
        <button className={`formal-button ${view === "follow" ? "primary" : ""}`} onClick={() => setView("follow")}>本轮新增 {monitor?.latest_follow.length || 0}（重复 {monitor?.latest_follow.filter(item => item.result_kind === "repeat_following").length || 0}）</button>
        <button className={`formal-button ${view === "unfollow" ? "warning" : ""}`} onClick={() => setView("unfollow")}>本轮取关 {monitor?.latest_unfollow.length || 0}</button>
        <button className={`formal-button ${view === "accounts" ? "primary" : ""}`} onClick={() => setView("accounts")}>账号基准 {monitor?.accounts.length || 0}</button>
      <button className={`formal-button ${view === "dm" ? "primary" : ""}`} onClick={() => setView("dm")}>私信待回复 {monitor?.latest_dm.length || 0}</button>
      <button className={`formal-button ${view === "logs" ? "primary" : ""}`} onClick={() => setView("logs")}>检查日志</button>
      <button className="formal-button" onClick={() => void loadMonitor()}><RefreshCw size={15} />刷新结果</button>
      <button className="formal-button" disabled={anyRunning || exportingDiagnostic} onClick={() => void exportDiagnostic()}><Download size={16} />导出检查诊断</button>
    </div>
    {diagnosticNotice ? <p className="formal-muted" role="status">{diagnosticNotice}</p> : null}
    <Panel icon={view === "dm" ? <Inbox size={19} /> : <UserRoundCheck size={19} />} title={view === "follow" ? "最近一轮新增关注" : view === "unfollow" ? "最近一轮取关账户" : view === "dm" ? "最近一轮私信待回复" : view === "accounts" ? "窗口账号基准" : "检查日志"} >
      {(view==="follow"||view==="unfollow")&&<div className="follow-result-controls"><label className="formal-field"><span>搜索结果账号</span><input className="formal-input" value={resultQuery} onChange={e=>setResultQuery(e.target.value)} placeholder="用户名 / 检测账号"/></label><label className="formal-field"><span>来源窗口</span><select className="formal-input" value={resultWindow} onChange={e=>setResultWindow(e.target.value)}><option value="">全部窗口</option>{snapshot.windows.map(w=><option key={w.id} value={w.id}>{w.name}</option>)}</select></label>{view==="follow"&&<label className="formal-field"><span>新增类型</span><select className="formal-input" value={resultKind} onChange={e=>setResultKind(e.target.value)}><option value="all">全部新增</option><option value="new">首次新增</option><option value="repeat">重复新增</option></select></label>}<span className="formal-chip">显示 {(view==="follow"?monitor?.latest_follow:monitor?.latest_unfollow)?.filter(matchesResult).length||0} 条</span></div>}
      <div className="formal-table-scroll"><table className="formal-history-table"><thead><tr>
        {view === "follow" || view === "unfollow" ? <><th>{view === "follow" ? "新增账号" : "被取关账号"}</th><th>检测账号</th><th>窗口</th><th>上轮实读关注数</th><th>本轮实读关注数</th><th>判定</th><th>发现时间</th></> : view === "dm" ? <><th>发信账号</th><th>内容预览</th><th>检测账号</th><th>窗口</th><th>发现时间</th></> : view === "accounts" ? <><th>窗口</th><th>登录账号</th><th>基准轮次</th><th>上次完成</th><th>状态</th></> : <><th>开始时间</th><th>类型</th><th>窗口数</th><th>完成 / 失败</th><th>新增（含重复）</th><th>重复新增</th><th>取关</th><th>待回复</th><th>状态</th></>}
      </tr></thead><tbody>
        {view === "follow" ? (monitor?.latest_follow || []).filter(matchesResult).map(item => { const round = roundsByProfile.get(item.profile_id); return <tr key={`${item.batch_id}:${item.profile_id}:${item.username}`}><td className="follow-monitor-account-name"><a href={`https://www.instagram.com/${encodeURIComponent(item.username)}/`} target="_blank" rel="noreferrer">@{item.username}</a></td><td>@{item.owner_username}</td><td>{windowNames.get(item.profile_id) || "未知窗口"}</td><td>{number(round?.previous_actual_count)}</td><td>{number(round?.actual_count)}</td><td><span className={`formal-status ${item.result_kind === "repeat_following" ? "warning" : "success"}`}>{item.result_kind === "repeat_following" ? "重复新增" : "新增"}</span></td><td>{formatTime(item.discovered_at)}</td></tr>; }) : null}
        {view === "unfollow" ? (monitor?.latest_unfollow || []).filter(matchesResult).map(item => { const round = roundsByProfile.get(item.profile_id); return <tr key={`${item.batch_id}:${item.profile_id}:${item.username}`}><td className="follow-monitor-account-name"><a href={`https://www.instagram.com/${encodeURIComponent(item.username)}/`} target="_blank" rel="noreferrer">@{item.username}</a></td><td>@{item.owner_username}</td><td>{windowNames.get(item.profile_id) || "未知窗口"}</td><td>{number(round?.previous_actual_count)}</td><td>{number(round?.actual_count)}</td><td><span className="formal-status warning">取关</span></td><td>{formatTime(item.discovered_at)}</td></tr>; }) : null}
        {view === "dm" ? (monitor?.latest_dm || []).map(item => <tr key={`${item.profile_id}:${item.thread_id}`}><td>{item.sender || "未识别"}</td><td className="formal-history-message">{item.preview || "未显示预览"}</td><td>@{item.owner_username}</td><td>{windowNames.get(item.profile_id) || "未知窗口"}</td><td>{formatTime(item.discovered_at)}</td></tr>) : null}
        {view === "accounts" ? (monitor?.accounts || []).map(item => <tr key={item.profile_id}><td>{windowNames.get(item.profile_id) || "未知窗口"}</td><td>@{item.username}</td><td>{item.generation}</td><td>{formatTime(item.checked_at)}</td><td><StatusBadge status={item.last_status} /></td></tr>) : null}
        {view === "logs" ? shownLogs.map(item => <tr key={item.id}><td>{formatTime(item.started_at)}</td><td>{item.check_kind === "following" ? "关注" : item.check_kind === "dm" ? "私信" : item.check_kind === "combined" ? "关注＋私信" : "旧版合并检查"}</td><td>{item.profile_ids.length}</td><td>{number(item.succeeded)} / {number(item.failed)}</td><td>{number(item.added_count)}</td><td>{number(item.repeat_count)}</td><td>{number(item.unfollow_count)}</td><td>{number(item.dm_count)}</td><td><StatusBadge status={item.status} /></td></tr>) : null}
        {((view === "follow" && !(monitor?.latest_follow||[]).filter(matchesResult).length) || (view === "unfollow" && !(monitor?.latest_unfollow||[]).filter(matchesResult).length) || (view === "dm" && !monitor?.latest_dm.length)) ? <tr><td colSpan={view === "dm" ? 5 : 7}>{resultQuery||resultWindow||resultKind!=="all"?"当前筛选条件下没有结果。":emptyText}{view === "follow" && !running && currentRun?.status !== "failed" ? "首次检查只建立基准。" : ""}</td></tr> : null}
      </tbody></table></div>
    </Panel>
    {view !== "logs" && view !== "dm" ? <Panel icon={<UserRoundCheck size={19} />} title="最近一轮读取数量" >
      <div className="formal-table-scroll"><table className="formal-history-table"><thead><tr><th>窗口</th><th>上轮主页关注数</th><th>上轮实读</th><th>本轮主页关注数</th><th>本轮实读</th><th>差额</th><th>首读 / 补读</th></tr></thead><tbody>
        {(monitor?.rounds || []).map(item => <tr key={item.profile_id}><td>{windowNames.get(item.profile_id) || "未知窗口"}</td><td>{number(item.previous_homepage_count)}</td><td>{number(item.previous_actual_count)}</td><td>{number(item.homepage_count)}</td><td>{number(item.actual_count)}</td><td>{number(item.homepage_count == null ? null : item.homepage_count-item.actual_count)}</td><td>{number(item.first_read_count)} / {item.second_read_count == null ? "未补读" : number(item.second_read_count)}{item.second_homepage_count != null && item.second_homepage_count !== item.homepage_count ? `（补读主页 ${number(item.second_homepage_count)}）` : ""}</td></tr>)}
        {!monitor?.rounds.length ? <tr><td colSpan={7}>{emptyText}</td></tr> : null}
      </tbody></table></div>
    </Panel> : null}
  </>;
}

function WorkbenchBody({ mode, snapshot, run, disabled, refresh, collectionControls, actionControls }: WorkspaceProps & { collectionControls: CollectionControls; actionControls: ActionControls; mode: FormalWorkbenchMode; refresh:()=>Promise<unknown> }) {
  const { platform } = useWorkbenchPlatform();
  const instagramOnly = ["public","private","follow-monitor","posting","nurture"].includes(mode);
  const visibleWindows = useMemo(() => instagramOnly
    ? snapshot.windows.filter(w=>!w.platform||w.platform==="instagram") : snapshot.windows, [instagramOnly, snapshot.windows]);

  if (mode === "home") return <HomeWorkspace />;
  if (mode === "accounts") return <StableAccountWorkspace snapshot={snapshot} onChanged={refresh} />;
  if (instagramOnly) {
    snapshot={...snapshot,windows:visibleWindows};
  }
  if (mode === "posting") return <PostingWorkspace snapshot={snapshot} refreshWindows={refresh} />;
  if (mode === "nurture") return <StudioWorkspace key={mode} mode={mode} snapshot={snapshot} refreshWindows={refresh} />;
  if (mode === "collection") return <CollectionWorkspace snapshot={snapshot} run={run} disabled={disabled} collectionControls={collectionControls} />;
  if (mode === "review") return <StableReviewWorkspace snapshot={snapshot} run={run} disabled={disabled} />;
  if (mode === "public") return <ActionWorkspace snapshot={snapshot} run={run} disabled={disabled} operation="greet" actionControls={actionControls} />;
  if (mode === "private") return <ActionWorkspace snapshot={snapshot} run={run} disabled={disabled} operation="follow" actionControls={actionControls} />;
  if (mode === "follow-monitor") return <FollowMonitorWorkspace snapshot={snapshot} run={run} disabled={disabled} refreshWindows={refresh} />;
  if (mode === "history") return <HistoryWorkspace snapshot={snapshot} />;
  return <SettingsWorkspace snapshot={snapshot} run={run} disabled={disabled} />;
}

function RecoveryCollectionControls({ live, controls }: { live: CoreWorkbenchLiveStatus | null; controls: CollectionControls }) {
  return <section className="formal-panel">
    <div className="formal-panel-header"><h2>当前采集任务</h2><span>{live ? `读取于 ${formatTime(live.generated_at)}` : "正在独立读取任务状态…"}</span></div>
    {!live ? <p>任务状态尚未返回，正在自动重试。</p> : <div className="formal-list">
      {live.tasks.map(({ task }) => {
        const key = `recovery-task-${task.id}`;
        const status = normalizeStatus(task.status);
        const canPause = ACTIVE_STATES.has(status) || status === "waiting_network";
        const stoppingTargets = (task.targets || []).filter(target => target.id && target.current_window_id
          && (ACTIVE_STATES.has(normalizeStatus(target.status)) || PAUSED_STATES.has(normalizeStatus(target.status))
            || ["waiting_network", "recoverable"].includes(normalizeStatus(target.status))));
        return <div className="formal-row" key={task.id}>
          <div className="formal-row-main"><strong>{task.name || task.id}</strong><StatusBadge status={status} /></div>
          {canPause ? <button className="formal-button compact warning" disabled={controls.feedback(key).safetyControlDisabled} onClick={() => void controls.run(key, [{ scope: "task", taskId: task.id, action: "pause" }], "已请求暂停当前采集任务")}><Pause size={15} />暂停</button> : null}
          {stoppingTargets.length ? <button className="formal-button compact danger" disabled={controls.feedback(key).safetyControlDisabled} onClick={() => void controls.run(key, stoppingTargets.map(target => ({ scope: "window", taskId: task.id, profileId: target.current_window_id!, targetId: target.id!, action: "stop" })), "已请求停止当前采集窗口")}><Square size={15} />停止当前窗口</button> : null}
        </div>;
      })}
      {!live.tasks.length ? <p>本次运行状态读取未发现活动采集任务。</p> : null}
    </div>}
  </section>;
}

function LiveFormalWorkbench({ mode, core }: FormalWorkbenchProps & {core:ReturnType<typeof useFormalCore>}) {
  const { platform } = useWorkbenchPlatform();
  const { snapshot, error, loading, notice, run, disabled, refresh, collectionControls, actionControls, reviewRun, reviewDisabled } = core;
  const page = (PAGE_COPY[mode]);
  const connection = snapshot?.connection;

  return (
    <div className="formal-shell">
      <Rail mode={mode} refresh={core.refresh} refreshing={core.loading || core.refreshing} />
      <main className="formal-main">
        <header className="formal-header">
          <div><h1>{("IG")} · {page.title}</h1></div>{mode==="accounts"&&<div id="account-header-actions" className="account-header-actions"/>}
          <div className="formal-header-meta">{mode==="accounts"&&<div id="account-translation-actions"/>}<ChatGPTButton /><GoogleTranslatorButton />
            {snapshot && mode!=="home" ? <span className="formal-chip"><Database size={15} />{("IG")} 去重 {formatCount(snapshot.dedupe.total)}</span> : null}
          </div>
        </header>
        {mode === "home" ? <div className="formal-content"><HomeWorkspace /></div> : mode === "accounts" ? (
          <div className="formal-content">
            {error ? <div className="formal-error-banner" role="status"><span>总览暂未更新，账号窗口独立读取；窗口操作由 Core 实时校验。</span><button className="formal-button compact" onClick={() => void refresh()}><RefreshCw size={15} />重试总览</button></div> : null}
            <StableAccountWorkspace snapshot={snapshot} onChanged={refresh} />
          </div>
        ) : mode === "review" ? (
          <div className="formal-content">
            {error ? <div className="formal-error-banner"><span>总览暂未更新：{error.message}。审核名单独立读取，加载成功后可继续操作。</span><button className="formal-button compact" onClick={() => void refresh()}><RefreshCw size={15} />重试总览</button></div> : null}
            <StableReviewWorkspace snapshot={snapshot} run={reviewRun} disabled={reviewDisabled} />
          </div>
        ) : mode === "collection" && !snapshot ? (
          <div className="formal-content">
            <div className="formal-error-banner"><span>{error ? `总览暂未更新：${error.message}` : "总览正在加载。"} 当前采集任务独立读取，可暂停或停止。</span><button className="formal-button compact" onClick={() => void refresh()}><RefreshCw size={15} />重试总览</button></div>
            <RecoveryCollectionControls live={core.liveStatus} controls={collectionControls} />
          </div>
        ) : loading && !snapshot ? (
          <div className="formal-blocked"><div className="formal-blocked-card"><LoaderCircle className="animate-spin" size={36} /><h2>正在连接本机 Core</h2></div></div>
        ) : !snapshot ? (
          <div className="formal-blocked"><div className="formal-blocked-card"><AlertTriangle size={38} color="#fb7185" /><h2>总览暂时无法加载</h2><p>{error?.message || "无法读取本机 Core 快照"}</p><p>正在自动重试；可切换到目标审核或采集任务查看独立读取的状态。</p><StorageStatus /><button className="formal-button primary" onClick={() => void refresh()}><RefreshCw size={16} />重试总览</button></div></div>
        ) : (
          <div className="formal-content">
            {error ? <div className="formal-error-banner"><span>本次快照更新失败，正在自动重试：{error.message}。总览数据暂未更新；独立读取正常的审核名单仍可操作，已有任务可暂停或停止。</span><button className="formal-button compact" onClick={() => void refresh()}><RefreshCw size={15} />立即重试</button></div> : null}
            {error ? <StorageStatus /> : null}
            {!error && connection?.inventory_stale === true ? <div className="formal-capacity-banner" role="status"><AlertTriangle size={18} /><span>窗口状态更新暂时延迟，正在自动重试。其他列表已更新；窗口占用仍由 Core 校验。</span></div> : null}
            <WorkbenchBody mode={mode} snapshot={snapshot} run={run} disabled={disabled} refresh={refresh} collectionControls={collectionControls} actionControls={actionControls} />
          </div>
        )}
        {notice ? <div className={`formal-notice ${notice.kind === "error" ? "is-error" : ""}`} role="status">{notice.text}</div> : null}
      </main>
    </div>
  );
}

function PlatformWorkbenchRoutes({ mode }: FormalWorkbenchProps) {
  // Keep one authenticated Core subscription across home, reports and workspace
  // routes. Only the page body changes; route navigation does not reconnect.
  const core=useFormalCore();
  if (mode === "home") return <div className="formal-shell"><Rail mode={mode} refresh={core.refresh} refreshing={core.loading || core.refreshing} /><main className="formal-main"><HomeWorkspace /></main></div>;
  if (mode === "reports") return <div className="formal-shell"><Rail mode={mode} refresh={core.refresh} refreshing={core.loading || core.refreshing} /><main className="formal-main"><header className="formal-header"><h1>报表</h1></header><div className="formal-content"><ReportsWorkspace snapshot={core.snapshot} snapshotError={core.error} snapshotLoading={core.loading} refreshSnapshot={core.refresh} /></div></main></div>;
  return <LiveFormalWorkbench mode={mode} core={core} />;
}

function WorkbenchRoutes(props: FormalWorkbenchProps) {
  // Ordinary navigation retains the single Core subscription and IG draft.
  return <PlatformWorkbenchRoutes {...props} />;
}

export default FormalWorkbench;

export function FormalWorkbench(props: FormalWorkbenchProps) { return <WorkbenchPlatformProvider><AccountUnreadProvider><WorkbenchRoutes {...props}/></AccountUnreadProvider></WorkbenchPlatformProvider>; }
