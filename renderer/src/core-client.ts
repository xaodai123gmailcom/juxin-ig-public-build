/**
 * Formal renderer -> local Core adapter.
 *
 * The installed application has one authoritative data source: the isolated
 * Electron preload bridge. This module deliberately contains no embedded sample records,
 * localStorage recovery, HTTP fallback, or browser-only simulation path.  A
 * missing/malformed bridge is therefore a hard, visible failure instead of an
 * invitation to display plausible but false data.
 */

export type CoreSocialPlatform = "instagram";

export type CoreRequestOptions = {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
};

export type ReviewAccountState={opened:boolean;lastTarget:string;message:string;httpStatus:number|null;resetting?:boolean;resetRequired?:boolean};
export type AccountPageDisplayState = 'blank' | 'loading' | 'ready' | 'load_failed' | 'crashed' | 'unresponsive';
export type AccountPageDisplay = {display_state?:AccountPageDisplayState;display_message?:string};
export type AccountManualControl = {active:true;grant:string;task_key:string;target:string};
export type AccountTaskFrame = AccountPageDisplay & {target:string;waiting_target?:string;screening_slots?:number;pages:Array<AccountPageDisplay & {id:string;label:string;title:string}>;image:string;captured_at:string;task_key:string|null;operation?:string|null;manual_control?:AccountManualControl|{active:false}|null;title:string};
export interface CollectorCoreBridge {
  reviewAccount?(input:{action:'status'|'login'|'home'|'target'|'hide'|'reset'}):Promise<ReviewAccountState>;
  chatTranslation?(input:Record<string,unknown>):Promise<any>;
  onProfilePreview?(listener: (url: string) => void): () => void;
  chatgptPage?(input:{hide?:boolean;top?:number;boundsOnly?:boolean}):Promise<{visible:boolean}>;
  onChatGPTVisibility?(listener:(visible:boolean)=>void):()=>void;
  googleTranslator?(input:{hide?:boolean;top?:number;boundsOnly?:boolean}):Promise<{visible:boolean}>;
  onTranslatorVisibility?(listener:(visible:boolean)=>void):()=>void;
  storageManagement?(input:Record<string,unknown>):Promise<any>;
  messageNotifications?(input:{enabled?:boolean;test?:boolean}):Promise<{enabled:boolean;supported:boolean;error?:string}>;
  onAccountFocus?(listener:(profile:string)=>void):()=>void;
  accountContextMenu?(input:{name:string;opened:boolean;disabled:boolean;configDisabled?:boolean;documentUrl:string}):Promise<string|null>;
  accountTaskWatch?(input:{id:string;documentUrl:string;target?:string}):Promise<AccountTaskFrame>;
  accountInterfere?(input:{id:string;documentUrl:string;taskKey:string;target:string}):Promise<{cancelled?:boolean;grant?:string;task_key?:string;target?:string;message?:string}>;
  accountResumeTask?(input:{id:string;documentUrl:string;taskKey:string;grant:string}):Promise<{resumed:boolean;message?:string}>;
  accountCloseTaskPage?(input:{id:string;documentUrl:string;taskKey:string;target:string}):Promise<{closed:boolean;target:string;message:string}>;
  onTaskInterference?(listener:(target:string)=>void):()=>void;
  accountSurface?(input: {surfaceId?:string;capture?:boolean;readOnly?:boolean;id?:string;visible:boolean;interferenceGrant?:string;viewTarget?:string;documentUrl?:string;bounds?:{x:number;y:number;width:number;height:number}}): Promise<AccountPageDisplay & {attached:boolean;message?:string;preview?:string}>;
  request<T = unknown>(path: string, options?: CoreRequestOptions): Promise<T>;
  secureSet(key: string, value: string): Promise<boolean>;
  secureGet(key: string): Promise<string | null>;
  secureDelete(key: string): Promise<boolean>;
  configureIntegrations(input: CoreIntegrationConfiguration): Promise<CoreIntegrationResult>;
}

export type CoreIntegrationResult = {
  restarted: boolean;
  saved?: boolean;
  cloud_configured?: boolean;
  cloud_activated?: boolean;
  restart_required?: boolean;
};

export type CoreIntegrationConfiguration = {
  bitbrowserPort?: number;
  bitbrowserApiKey?: string;
  openaiApiKey?: string;
  cloud?: {enabled:boolean;projectUrl:string;publishableKey:string};
};

export class CollectorCoreUnavailableError extends Error {
  readonly code = "collector_core_bridge_unavailable";

  constructor(message = "桌面 Core 连接组件未加载，正式运行已阻断") {
    super(message);
    this.name = "CollectorCoreUnavailableError";
  }
}

export class CollectorCoreProtocolError extends Error {
  readonly code = "collector_core_protocol_error";

  constructor(message: string) {
    super(message);
    this.name = "CollectorCoreProtocolError";
  }
}

export type CoreVisibility = "public" | "private" | "unknown" | "not_visible";
export type CoreReviewDecision = "approved" | "rejected";
export type CoreReviewStage = 1 | 2;
export type CoreCollectionMode = "followers" | "following";
export type CoreActionOperation = "greet" | "follow";

export type CoreMonitorKind = "combined" | "following" | "dm";
export type CoreMonitorRun = { id: string; check_kind: string; status: string; processed: number; succeeded: number; failed: number; added_count: number; unfollow_count: number; dm_count: number; repeat_count: number; started_at: string; profile_ids: string[]; active_profile_ids: string[]; finished_profile_ids: string[]; errors: Array<{ profile_id: string; message: string; partial?: boolean; stages?: Partial<Record<CoreMonitorKind, string>> }> };
export type CoreFollowMonitorSnapshot = {
  run: CoreMonitorRun | null;
  runs: Record<CoreMonitorKind, CoreMonitorRun | null>;
  latest_follow: Array<{ batch_id: string; profile_id: string; owner_username: string; username: string; result_kind: string; discovered_at: string }>;
  latest_unfollow: Array<{ batch_id: string; profile_id: string; owner_username: string; username: string; discovered_at: string }>;
  latest_dm: Array<{ profile_id: string; owner_username: string; thread_id: string; sender: string | null; preview: string; message_at: string | null; discovered_at: string }>;
  accounts: Array<{ profile_id: string; instagram_user_id: string; username: string; generation: number; previous_generation: number | null; previous_following_count: number; following_count: number; homepage_count: number | null; previous_homepage_count: number | null; total_added_count: number; last_added_count: number; total_unfollow_count: number; last_unfollow_count: number; total_repeat_count: number; last_repeat_count: number; total_dm_count: number; last_dm_count: number; last_status: string; last_error: string | null; checked_at: string | null }>;
  dm_accounts: Array<{ profile_id: string; username: string; last_run_id: string | null; total_dm_count: number; last_dm_count: number; last_status: string; last_error: string | null; checked_at: string | null }>;
  rounds: Array<{ batch_id: string; profile_id: string; owner_username: string; homepage_count: number | null; previous_homepage_count: number | null; actual_count: number; previous_actual_count: number | null; first_read_count: number; second_read_count: number | null; second_homepage_count: number | null; added_count: number; repeat_count: number; unfollow_count: number }>;
  counts: { total: number; month: number; week: number; today: number; repeated: number };
  logs: CoreMonitorRun[];
};

export type CoreCandidate = {
  id: string;
  account_id?: string | null;
  claim_account_id?: string | null;
  username: string;
  visibility: CoreVisibility;
  profile: Record<string, unknown>;
  screening: Record<string, unknown>;
  source_mode?: CoreCollectionMode | null;
  source_target?: string | null;
  review_cache?: Record<string, unknown> | null;
  decision?: CoreReviewDecision | null;
  reviewed_at?: string | null;
  review_stage?: CoreReviewStage;
  review_transferred_at?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type CoreAccountExportQuery = {
  visibility: "public" | "private";
  scope: "selected" | "all";
  candidate_ids?: string[];
};
export type CoreAccountExport = {
  platform?: CoreSocialPlatform;
  filename: string;
  mime_type: "text/csv;charset=utf-8";
  csv: string;
  row_count: number;
  skipped_count: number;
  visibility: "public" | "private";
  scope: "selected" | "all";
};

export type CoreReviewQueueQuery = {
  platform?: CoreSocialPlatform;
  visibility: "public" | "private";
  review_stage: CoreReviewStage;
  offset?: number;
  limit?: number;
};
export type CoreReviewQueuePage = {
  platform?: CoreSocialPlatform;
  items: CoreCandidate[];
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
  counts: Record<"public" | "private", { stage1: number; stage2: number }>;
  snapshot_seq: number;
};
export type CoreReviewStageMove = {
  candidate_ids: string[];
  visibility: "public" | "private";
  from_stage: CoreReviewStage;
  to_stage: CoreReviewStage;
};
export type CoreReviewStageMoveResult = {
  moved_ids: string[];
  skipped_ids: string[];
  moved_count: number;
};

export type CoreApprovalHistoryCandidate = CoreCandidate & {
  decided_at?: string | null;
  dismissed_at?: string | null;
  actionable?: boolean;
};

export type CoreManualRejection = {
  id: string;
  candidate_id?: string;
  username: string;
  visibility?: CoreVisibility;
  profile?: Record<string, unknown>;
  reason?: string;
  rejected_at?: string;
  reviewed_at?: string;
  [field: string]: unknown;
};

export type CoreCollectionExclusion = {
  id: string;
  account_id?: string;
  username: string;
  reason_code: string;
  reason: string;
  location_country?: string | null;
  profile?: Record<string, unknown>;
  excluded_at?: string;
  created_at?: string;
  [field: string]: unknown;
};

export type CoreWindowActionCounts = {
  profile_id: string;
  greet_successes: number;
  follow_successes: number;
  total_successes: number;
  greet_current: number;
  follow_current: number;
  greet_reset_at?: string | null;
  follow_reset_at?: string | null;
};

export type CoreDedupeStats = {
  total: number;
  claimed?: number;
  pending_review?: number;
  approved?: number;
  rejected?: number;
  collection_excluded?: number;
  duplicate_hits?: number;
  action_successes?: number;
  greet_successes?: number;
  follow_successes?: number;
  window_action_counts?: CoreWindowActionCounts[];
  [field: string]: unknown;
};

export type CoreBitBrowserConnection = {
  state?: string;
  phase?: string;
  connected?: boolean;
  endpoint?: string;
  reason?: string | null;
  detail?: string;
  retry_after_seconds?: number;
  next_probe_at?: string | number;
  [field: string]: unknown;
};

export type CoreBitBrowserWindow = {
  id: string;
  platform?: string;
  name: string;
  group?: string | null;
  serial_number?: number | null;
  provider_order?: number | null;
  opened?: boolean;
  window_state?: string | null;
  generation?: number;
  ready?: boolean;
  opening?: boolean;
  locked?: boolean;
  lock_state?: string | null;
  lock_operation?: string | null;
  lock_entity_id?: string | null;
  lock_owned?: boolean;
  action_counts?: CoreWindowActionCounts;
  [field: string]: unknown;
};

export type CoreTaskTarget = {
  id: string;
  username?: string;
  username_display?: string;
  status: string;
  current_window_id?: string | null;
  preferred_window_id?: string | null;
  current_stage?: string | null;
  collection_list_dismissed?: boolean;
  completion_policy?: "automatic";
  source_recheck?: { mode: "followers" | "following"; state: string; completed_at: string | null; requested_at?: string | null } | null;
  last_error?: string | null;
  mode_progress?: Record<string, Record<string, number | string | null>>;
  mode_coverage?: Record<string, import("./split-review-report").CollectionCoverage>;
  [field: string]: unknown;
};

export type CoreTaskWindow = {
  profile_id: string;
  status?: string;
  queue_order?: number;
  [field: string]: unknown;
};

/** One target-account source row shown by the collection workspace. */
export type CoreCollectionSource = {
  id: string;
  task_id: string;
  username: string;
  modes: CoreCollectionMode[];
  status: string;
  current_window_id?: string | null;
  processed?: number;
  total?: number | null;
  mode_progress?: Record<string, Record<string, number | string | null>>;
  mode_coverage?: Record<string, import("./split-review-report").CollectionCoverage>;
  created_at?: string;
  updated_at?: string;
  [field: string]: unknown;
};

export type CoreSplitCandidate = {
  id: string;
  username: string;
  kind: string;
  queue_state: string;
  locked?: boolean;
  lifecycle_state?: string | null;
  real_lifecycle_state?: string | null;
  queued_target_id?: string | null;
  disposition?: string;
  allowed_window_ids?: string[];
  window_assignment_mode?: "automatic" | "specified";
  source_task_id?: string | null;
  source_target_id?: string | null;
  source_window_id?: string | null;
  source_status?: string | null;
  last_error?: string | null;
  updated_at?: string;
  [field: string]: unknown;
};

export type CoreCollectionTask = {
  id: string;
  name?: string;
  status: string;
  modes?: CoreCollectionMode[];
  targets?: CoreTaskTarget[];
  targets_partial?: boolean;
  windows?: CoreTaskWindow[];
  settings?: Record<string, unknown>;
  runtime?: Record<string, unknown>;
  assignment_mode?: "sequential" | "manual";
  created_at?: string;
  updated_at?: string;
  last_error?: string | null;
  [field: string]: unknown;
};

export type CoreActionTarget = {
  id: string;
  username: string;
  source_target?: string | null;
  status: string;
  last_error?: string | null;
  updated_at?: string;
  [field: string]: unknown;
};

export type CoreActionCampaign = {
  id: string;
  operation: CoreActionOperation;
  execution_type?: "manual" | "campaign";
  profile_id: string;
  status: string;
  message?: string | null;
  messages?: string[];
  interval_min_seconds?: number;
  interval_max_seconds?: number;
  limit_count?: number;
  targets?: CoreActionTarget[];
  attempts?: Array<Record<string, unknown>>;
  created_at?: string;
  updated_at?: string;
  [field: string]: unknown;
};

export type CoreHistoryRecord = {
  id: string;
  task_id?: string;
  target_id?: string;
  source_target?: string;
  target_account?: string;
  window_id?: string | null;
  status?: string;
  task_status?: string;
  counts?: Record<string, number>;
  created_at?: string;
  updated_at?: string;
  [field: string]: unknown;
};

export type CoreVersionInfo = {
  app?: string;
  core?: string;
  schema?: number;
  [field: string]: string | number | undefined;
};

export type CoreStorageStatus = {
  database_bytes?: number;
  wal_bytes?: number;
  cache_bytes?: number;
  temporary_bytes?: number;
  pending_preview_bytes?: number;
  disk_total_bytes?: number;
  disk_used_bytes?: number;
  disk_free_bytes?: number;
  low_space_threshold_bytes?: number;
  low_space_warning?: boolean | null;
  disk_probe_available?: boolean;
  file_probe_available?: boolean;
  retained_history?: Record<string, number>;
  last_cleanup_at?: string | null;
  last_maintenance_at?: string | null;
  [field: string]: unknown;
};


export type CoreLiveTaskStatus = {
  task: CoreCollectionTask;
  runtime?: Record<string, unknown>;
};

export type CoreWorkbenchLiveStatus = {
  generated_at: string;
  revision: number;
  tasks: CoreLiveTaskStatus[];
};

export type CoreWorkbenchSnapshot = {
  platform?: CoreSocialPlatform;
  revision: number;
  generated_at: string;
  /** Client-only freshness marker for the independently polled task status. */
  collection_status_observation?: { generated_at: string; revision: number };
  counts: Record<string, number>;
  dedupe: CoreDedupeStats;
  pending: {
    public: CoreCandidate[];
    private: CoreCandidate[];
  };
  approved: {
    public: CoreCandidate[];
    private: CoreCandidate[];
  };
  history: {
    manual_rejections: CoreManualRejection[];
    collection_exclusions: CoreCollectionExclusion[];
    approvals?: CoreApprovalHistoryCandidate[];
    tasks?: CoreHistoryRecord[];
    actions?: CoreActionCampaign[];
  };
  windows: CoreBitBrowserWindow[];
  sources: CoreCollectionSource[];
  split_candidates: CoreSplitCandidate[];
  split_claim_locked?: boolean;
  tasks: CoreCollectionTask[];
  campaigns: CoreActionCampaign[];
  connection?: CoreBitBrowserConnection;
  version?: string | CoreVersionInfo;
  source_revision?: string;
  storage?: CoreStorageStatus;
  has_more?: Record<string, boolean>;
  truncated: boolean | Record<string, boolean>;
};

export type DedupeClaimPayload = {
  username: string;
  instagram_user_id?: string;
  source?: string;
  source_target?: string;
};

export type DedupeClaimResult = {
  outcome: "claimed" | "duplicate";
  claimed: boolean;
  duplicate: boolean;
  account_id: string;
  username: string;
  should_read_location: boolean;
  should_collect_profile: boolean;
  global_dedupe_count: number;
};

export type CandidateUpsertPayload = {
  claim_account_id: string;
  username: string;
  visibility: "public" | "private";
  profile: Record<string, unknown>;
  screening: Record<string, unknown>;
  source_mode?: CoreCollectionMode;
  source_target?: string;
  review_cache?: Record<string, unknown>;
};

export type CollectionExclusionPayload = {
  claim_account_id: string;
  username: string;
  reason_code: string;
  reason: string;
  location_country?: string;
  profile?: Record<string, unknown>;
};

export type ReviewDecisionPayload = {
  candidate_id: string;
  decision: CoreReviewDecision;
};

export type ReviewDecisionResult = {
  candidate_id: string;
  decision: CoreReviewDecision;
  visibility: "public" | "private";
  destination: "approved_public" | "approved_private" | "manual_rejections";
  reviewed_at: string;
  candidate: CoreCandidate;
};

export type CollectionTaskCreatePayload = {
  platform?: "instagram";
  targets: string[];
  window_ids: string[];
  modes: CoreCollectionMode[];
  source_limits: Record<string, Record<string, number>>;
  assignment_mode?: "sequential" | "manual";
  manual_assignments?: Record<string, string>;
  dedupe?: true;
  local_person_recognition?: boolean;
  exclude_male_avatar?: boolean;
  auto_classify?: boolean;
  read_location?: boolean;
  gpt_review?: boolean;
  exclude_verified?: boolean;
  exclude_public_zero_posts?: boolean;
  discard_count_limits_enabled?: boolean;
  private_discard_followers_max?: number;
  private_discard_following_max?: number;
  private_discard_posts_max?: number;
  public_discard_followers_max?: number;
  public_discard_following_max?: number;
  public_discard_posts_max?: number;
  public_discard_active_days_max?: number;
  parallel_screening_workers?: 1 | 2 | 3;
  allow_completed_targets?: boolean;
};

export type CompletedTargetConflict = {
  username: string;
  username_norm: string;
  completed_at: string;
  task_id: string;
  modes: string[];
};

export type ActionCampaignStartPayload = {
  operation: CoreActionOperation;
  profile_id: string;
  targets: string[];
  target_sources?: Record<string, string>;
  message?: string;
  messages?: string[];
  interval: string;
  limit: number;
};

export type WorkbenchCommandMap = {
  dedupe_claim: { payload: DedupeClaimPayload; result: DedupeClaimResult };
  create_candidate: { payload: CandidateUpsertPayload; result: CoreCandidate };
  record_exclusion: { payload: CollectionExclusionPayload; result: CoreCollectionExclusion };
  review_decision: { payload: ReviewDecisionPayload; result: ReviewDecisionResult };
  review_stage_move: { payload: CoreReviewStageMove; result: CoreReviewStageMoveResult };
  approved_candidate_dismiss: { payload: { candidate_id: string }; result: { candidate_id: string; status: "dismissed"; dismissed_at: string; global_dedupe_retained: true; affected_targets?: Array<Record<string, unknown>> } };
  bitbrowser_refresh: { payload: Record<string, never>; result: { windows: CoreBitBrowserWindow[]; connection?: CoreBitBrowserConnection; stale?: boolean } };
  bitbrowser_reconnect: { payload: Record<string, never>; result: { windows: CoreBitBrowserWindow[]; connection?: CoreBitBrowserConnection; stale?: boolean } };
  bitbrowser_open: { payload: { profile_id: string }; result: Record<string, unknown> };
  bitbrowser_close: { payload: { profile_id: string }; result: Record<string, unknown> };
  bitbrowser_open_selected: { payload: { profile_ids: string[] }; result: Record<string, unknown> };
  task_create: { payload: CollectionTaskCreatePayload; result: { task_id: string; status: string } };
  task_completed_targets_check: { payload: { targets: string[] }; result: { completed: CompletedTargetConflict[]; completed_count: number; clear_count: number } };
  split_waiting_add: { payload: { targets: string[]; allow_completed_targets?: boolean; allowed_window_ids?: string[] }; result: { candidates: CoreSplitCandidate[]; accepted_ids: string[]; duplicates: Array<Record<string, unknown>> } };
  split_waiting_assign_windows: { payload: { candidate_id: string; allowed_window_ids: string[] }; result: CoreSplitCandidate };
  split_waiting_lock: { payload: { candidate_id: string; locked: boolean }; result: { candidate: CoreSplitCandidate } };
  split_claim_lock: { payload: { locked: boolean }; result: { locked: boolean } };
  split_waiting_delete: { payload: { candidate_id: string }; result: { candidate_id: string; deleted: true } };
  split_failure_requeue: { payload: { candidate_id: string; allowed_window_ids?: string[] }; result: CoreSplitCandidate };
  split_failure_delete: { payload: { candidate_id: string }; result: { candidate_id: string; deleted: true; already_absent?: boolean } };
  task_control: { payload: { task_id: string; action: "start" | "pause" | "resume" | "stop" | "restart" | "close" }; result: CoreCollectionTask | { task_id: string; status: string } };
  task_delete: { payload: { task_id: string }; result: { task_id: string; status: "deleted"; global_dedupe_retained: true } };
  task_retry_network: { payload: { task_id: string }; result: Record<string, unknown> };
  task_add_targets: { payload: { task_id: string; targets: string[] }; result: CoreCollectionTask };
  task_add_windows: { payload: { task_id: string; window_ids: string[] }; result: CoreCollectionTask };
  task_window_control: { payload: { task_id: string; profile_id: string; action: "pause" | "resume" | "stop" | "restart" | "delete" | "delete_only"; target_id?: string }; result: CoreCollectionTask | Record<string, unknown> };
  task_target_control: { payload: { task_id: string; target_id: string; action: "retry" | "delete" | "dismiss_completed" }; result: CoreCollectionTask | Record<string, unknown> };
  task_source_recheck: { payload: { task_id: string; target_id: string; mode: "followers" | "following" }; result: { task: CoreCollectionTask; target: CoreTaskTarget; profile_id: string; mode: string; status: string; waiting_for_task_resume: boolean; parent_only?: boolean; waiting_for_safe_point?: boolean } };
  action_manual: { payload: { operation: CoreActionOperation; profile_id: string; target: string; source_target?: string; message?: string }; result: CoreActionCampaign | Record<string, unknown> };
  action_campaign_start: { payload: ActionCampaignStartPayload; result: { campaign_id: string; status: string } };
  action_campaign_control: { payload: { campaign_id: string; action: "pause" | "resume" | "stop" }; result: { campaign_id: string; status: string } };
  action_target_control: { payload: { campaign_id: string; target_id: string; action: "pause" | "resume" | "cancel" }; result: { campaign_id: string; target_id: string; status: string; deferred?: boolean } };
  action_failure_dismiss: { payload: { campaign_id: string; target_id: string }; result: { campaign_id: string; target_id: string; status: "dismissed" } };
  action_unknown_resolve: {
    payload: { campaign_id: string; target_id: string; outcome: "not_completed" | "completed" };
    result: {
      campaign_id: string;
      target_id: string;
      username: string;
      operation: CoreActionOperation;
      outcome: "not_completed" | "completed";
      status: string;
      returned_to_approved: boolean;
      approved_candidate_id?: string;
      success_ledger_recorded: boolean;
      global_dedupe_retained: boolean;
      snapshot_seq: number;
    };
  };
  action_pause_active: { payload: { operation: CoreActionOperation; profile_id: string }; result: { campaign_id: string; status: string } };
  action_reset_counter: { payload: { operation: CoreActionOperation; profile_id: string }; result: Record<string, unknown> };
  storage_cache_clear: {
    payload: Record<string, never>;
    result: {
      cleared_entries: number;
      cleared_bytes: number;
      last_cleanup_at: string;
      business_records_retained: true;
      retained: {
        global_dedupe: true;
        review_history: true;
        collection_exclusions: true;
        action_success_history: true;
        live_task_checkpoints: true;
        pending_review_previews: true;
      };
      snapshot_seq: number;
    };
  };
};

export type WorkbenchCommandType = keyof WorkbenchCommandMap;
export type WorkbenchCommandPayload<T extends WorkbenchCommandType> = WorkbenchCommandMap[T]["payload"];
export type WorkbenchCommandResult<T extends WorkbenchCommandType> = WorkbenchCommandMap[T]["result"];

export type SnapshotQuery = {
  platform?: CoreSocialPlatform;
  limit?: number;
  historyLimit?: number;
  /** Omit duplicate legacy aliases on the wire; canonical nested rows are unchanged. */
  compact?: boolean;
};

export type SnapshotPollingOptions = SnapshotQuery & {
  intervalMs?: number;
  immediate?: boolean;
  onSnapshot: (snapshot: CoreWorkbenchSnapshot) => void;
  onError?: (error: Error) => void;
};

export type SnapshotPollingController = {
  refresh(): Promise<CoreWorkbenchSnapshot>;
  stop(): void;
  readonly stopped: boolean;
  readonly current: CoreWorkbenchSnapshot | null;
};

function recordValue(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function bridgeFromGlobal(): CollectorCoreBridge | undefined {
  const value = (globalThis as { window?: { collectorCore?: CollectorCoreBridge } }).window?.collectorCore;
  return value;
}

export function isCollectorCoreBridge(value: unknown): value is CollectorCoreBridge {
  return recordValue(value)
    && typeof value.request === "function"
    && typeof value.secureSet === "function"
    && typeof value.secureGet === "function"
    && typeof value.secureDelete === "function"
    && typeof value.configureIntegrations === "function";
}

export function hasCollectorCoreBridge(value: unknown = bridgeFromGlobal()): value is CollectorCoreBridge {
  return isCollectorCoreBridge(value);
}

export function requireCollectorCoreBridge(value: unknown = bridgeFromGlobal()): CollectorCoreBridge {
  if (!isCollectorCoreBridge(value)) throw new CollectorCoreUnavailableError();
  return value;
}

function requireNonEmptyString(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new CollectorCoreProtocolError(`Core snapshot field is invalid: ${field}`);
  }
  return value;
}

function requireArray(value: unknown, field: string): unknown[] {
  if (!Array.isArray(value)) throw new CollectorCoreProtocolError(`Core snapshot field is invalid: ${field}`);
  return value;
}

function requireRecord(value: unknown, field: string): Record<string, unknown> {
  if (!recordValue(value)) throw new CollectorCoreProtocolError(`Core snapshot field is invalid: ${field}`);
  return value;
}

function normalizeSnapshot(value: unknown): CoreWorkbenchSnapshot {
  const snapshot = requireRecord(value, "snapshot");
  if (snapshot.platform != null && snapshot.platform !== "instagram") throw new CollectorCoreProtocolError("Invalid snapshot platform");
  const revision = snapshot.revision;
  if (typeof revision !== "number" || !Number.isSafeInteger(revision) || revision < 0) {
    throw new CollectorCoreProtocolError("Core snapshot field is invalid: revision");
  }
  requireNonEmptyString(snapshot.generated_at, "generated_at");
  const counts = requireRecord(snapshot.counts, "counts");
  if (Object.values(counts).some((count) => typeof count !== "number" || !Number.isFinite(count) || count < 0)) {
    throw new CollectorCoreProtocolError("Core snapshot field is invalid: counts");
  }
  const dedupe = requireRecord(snapshot.dedupe, "dedupe");
  if (typeof dedupe.total !== "number" || !Number.isFinite(dedupe.total) || dedupe.total < 0) {
    throw new CollectorCoreProtocolError("Core snapshot field is invalid: dedupe.total");
  }
  const pending = requireRecord(snapshot.pending, "pending");
  const approved = requireRecord(snapshot.approved, "approved");
  const history = requireRecord(snapshot.history, "history");
  requireArray(pending.public, "pending.public");
  requireArray(pending.private, "pending.private");
  requireArray(approved.public, "approved.public");
  requireArray(approved.private, "approved.private");
  requireArray(history.manual_rejections, "history.manual_rejections");
  requireArray(history.collection_exclusions, "history.collection_exclusions");
  if (history.approvals !== undefined) requireArray(history.approvals, "history.approvals");
  if (history.tasks !== undefined) requireArray(history.tasks, "history.tasks");
  if (history.actions !== undefined) requireArray(history.actions, "history.actions");

  // These operational arrays are additive in the Core contract.  They remain
  // required here (rather than silently becoming []) so an installer with an
  // old Core cannot present an empty task/window page as if it were authoritative.
  requireArray(snapshot.windows, "windows");
  requireArray(snapshot.sources, "sources");
  // Older saved test/snapshot envelopes predate the durable split queue.
  // Treat only an absent additive field as empty; malformed values still fail.
  if (snapshot.split_candidates === undefined) snapshot.split_candidates = [];
  requireArray(snapshot.split_candidates, "split_candidates");
  if (snapshot.split_claim_locked !== undefined && typeof snapshot.split_claim_locked !== "boolean")
    throw new CollectorCoreProtocolError("Invalid split_claim_locked in snapshot");
  requireArray(snapshot.tasks, "tasks");
  requireArray(snapshot.campaigns, "campaigns");
  if (snapshot.has_more !== undefined) {
    const hasMore = requireRecord(snapshot.has_more, "has_more");
    for (const [key, value] of Object.entries(hasMore)) {
      if (typeof value !== "boolean") {
        throw new CollectorCoreProtocolError(`Core snapshot field is invalid: has_more.${key}`);
      }
    }
  }
  if (typeof snapshot.truncated !== "boolean" && !recordValue(snapshot.truncated)) {
    throw new CollectorCoreProtocolError("Core snapshot field is invalid: truncated");
  }
  return snapshot as CoreWorkbenchSnapshot;
}

function positiveBoundedInteger(value: number | undefined, fallback: number): number {
  if (value === undefined) return fallback;
  if (!Number.isSafeInteger(value) || value < 1 || value > 5_000) {
    throw new RangeError("Snapshot limit must be an integer between 1 and 5000");
  }
  return value;
}

function snapshotPath(query: SnapshotQuery = {}): string {
  const limit = positiveBoundedInteger(query.limit, 500);
  const historyLimit = positiveBoundedInteger(query.historyLimit, 500);
  return `/api/workbench/snapshot?limit=${limit}&history_limit=${historyLimit}${query.platform ? `&platform=${query.platform}` : ""}${query.compact === true ? "&compact=1" : ""}`;
}

function validateCommandType(type: string): asserts type is WorkbenchCommandType {
  const allowed = new Set<WorkbenchCommandType>([
    "dedupe_claim", "create_candidate", "record_exclusion", "review_decision", "review_stage_move", "approved_candidate_dismiss",
    "bitbrowser_refresh", "bitbrowser_reconnect", "bitbrowser_open", "bitbrowser_close",
    "bitbrowser_open_selected", "task_create", "task_completed_targets_check", "split_waiting_add", "split_waiting_assign_windows", "split_waiting_lock", "split_claim_lock", "split_waiting_delete", "split_failure_requeue", "split_failure_delete", "task_control", "task_delete", "task_retry_network", "task_add_targets",
    "task_add_windows", "task_window_control", "task_target_control", "task_source_recheck", "action_manual",
    "action_campaign_start", "action_campaign_control", "action_target_control", "action_failure_dismiss", "action_unknown_resolve", "action_pause_active",
    "action_reset_counter",
    "storage_cache_clear",
  ]);
  if (!allowed.has(type as WorkbenchCommandType)) {
    throw new CollectorCoreProtocolError(`Unsupported workbench command: ${type}`);
  }
}

const coreRevisionFences = new WeakMap<CollectorCoreBridge, { latestApplied: number; lastCommand: number }>();

export class CollectorCoreClient {
  readonly #bridge: CollectorCoreBridge;
  readonly platform?: CoreSocialPlatform;
  #snapshotRequests = new Map<string, Promise<CoreWorkbenchSnapshot>>();
  readonly #fence: { latestApplied: number; lastCommand: number };

  /**
   * A snapshot can legitimately finish after a newer, differently-sized
   * snapshot (or a command response) has already advanced the renderer's
   * revision fence.  Keep the public request single-flight while replacing
   * that late response with a bounded number of fresh Core reads.  Four total
   * attempts are enough to absorb normal response reordering without hiding a
   * Core that is genuinely stuck behind its own command revision.
   */
  static readonly staleSnapshotMaxAttempts = 4;

  constructor(bridge: CollectorCoreBridge, platform?: CoreSocialPlatform) {
    this.#bridge = requireCollectorCoreBridge(bridge);
    if (platform !== undefined && platform !== "instagram") throw new CollectorCoreProtocolError("Invalid platform");
    this.platform = platform;
    let fence = coreRevisionFences.get(this.#bridge);
    if (!fence) { fence = { latestApplied: 0, lastCommand: 0 }; coreRevisionFences.set(this.#bridge, fence); }
    this.#fence = fence;
  }

  request<T>(path: string, options?: CoreRequestOptions): Promise<T> {
    if (!path.startsWith("/api/")) throw new CollectorCoreProtocolError("Core request must use an /api/ path");
    if (options?.body && typeof options.body === "object") {
      const scopedPaths = new Set(["/api/workbench/review/query", "/api/workbench/accounts/export", "/api/reports/query", "/api/reports/split-review", "/api/reports/private-follow-review", "/api/reports/review-decision"]);
      if (scopedPaths.has(path)) {
        if ("platform" in options.body && options.body.platform != null && options.body.platform !== "instagram") throw new CollectorCoreProtocolError("Invalid request platform");
        if (this.platform) options = { ...options, body: { ...options.body, platform: this.platform } };
      }
    }
    return this.#bridge.request<T>(path, options);
  }

  storageStatus() { return this.request<CoreStorageStatus>("/api/workbench/storage"); }

  snapshot(query: SnapshotQuery = {}): Promise<CoreWorkbenchSnapshot> {
    if (query.platform !== undefined && query.platform !== "instagram") throw new CollectorCoreProtocolError("Invalid snapshot platform");
    const path = snapshotPath({ ...query, ...(this.platform ? { platform: this.platform } : {}) });
    const existing = this.#snapshotRequests.get(path);
    if (existing) return existing;

    const readFreshSnapshot = async (): Promise<CoreWorkbenchSnapshot> => {
      let staleRevision = -1;
      let minimumRevision = 0;
      for (
        let attempt = 1;
        attempt <= CollectorCoreClient.staleSnapshotMaxAttempts;
        attempt += 1
      ) {
        const snapshot = normalizeSnapshot(await this.request<unknown>(path));
        if (this.platform && snapshot.platform !== this.platform) throw new CollectorCoreProtocolError("Core snapshot platform does not match the selected platform");
        const requiredRevision = Math.max(
          this.#fence.latestApplied,
          this.#fence.lastCommand,
        );
        if (snapshot.revision >= requiredRevision) {
          this.#fence.latestApplied = Math.max(
            this.#fence.latestApplied,
            snapshot.revision,
          );
          return snapshot;
        }

        staleRevision = snapshot.revision;
        minimumRevision = requiredRevision;
      }
      throw new CollectorCoreProtocolError(
        `Core returned stale snapshot revision ${staleRevision}; minimum accepted revision is ${minimumRevision} after ${CollectorCoreClient.staleSnapshotMaxAttempts} attempts`,
      );
    };

    const request = readFreshSnapshot()
      .finally(() => {
        if (this.#snapshotRequests.get(path) === request) this.#snapshotRequests.delete(path);
      });
    this.#snapshotRequests.set(path, request);
    return request;
  }

  async command<T extends WorkbenchCommandType>(
    type: T,
    payload: WorkbenchCommandPayload<T>,
  ): Promise<WorkbenchCommandResult<T>> {
    validateCommandType(type);
    if (!recordValue(payload)) throw new CollectorCoreProtocolError(`Command payload must be an object: ${type}`);
    if ("platform" in payload && payload.platform != null && payload.platform !== "instagram") throw new CollectorCoreProtocolError("Command platform does not match the selected platform");

    const response = await this.request<unknown>("/api/workbench/commands", {
      method: "POST",
      body: { type, payload: this.platform ? { ...payload, platform: this.platform } : payload },
    });
    const envelope = requireRecord(response, `command response: ${type}`);
    if (envelope.command !== type) {
      throw new CollectorCoreProtocolError(`Core returned a mismatched command response: ${String(envelope.command)}`);
    }
    if (!("result" in envelope)) {
      throw new CollectorCoreProtocolError(`Core command response is missing result: ${type}`);
    }
    if (typeof envelope.snapshot_seq !== "number" || !Number.isSafeInteger(envelope.snapshot_seq) || envelope.snapshot_seq < 0) {
      throw new CollectorCoreProtocolError(`Core command response has an invalid snapshot_seq: ${type}`);
    }
    this.#fence.lastCommand = Math.max(this.#fence.lastCommand, envelope.snapshot_seq);
    return envelope.result as WorkbenchCommandResult<T>;
  }

  claimDedupe(payload: DedupeClaimPayload) { return this.command("dedupe_claim", payload); }
  upsertCandidate(payload: CandidateUpsertPayload) { return this.command("create_candidate", payload); }
  recordCollectionExclusion(payload: CollectionExclusionPayload) { return this.command("record_exclusion", payload); }
  decideReview(payload: ReviewDecisionPayload) { return this.command("review_decision", payload); }
  async reviewQueue(query: CoreReviewQueueQuery) {
    const page = await this.request<CoreReviewQueuePage>("/api/workbench/review/query", { method: "POST", body: query });
    if ((this.platform && page.platform !== this.platform) || (page.platform != null && page.platform !== "instagram")) throw new CollectorCoreProtocolError("Core review page platform does not match the selected platform");
    return page;
  }
  async exportApprovedAccounts(query: CoreAccountExportQuery) {
    const result = await this.request<CoreAccountExport>("/api/workbench/accounts/export", { method: "POST", body: query });
    if ((this.platform && result.platform !== this.platform) || (result.platform != null && result.platform !== "instagram")) throw new CollectorCoreProtocolError("Core export platform does not match the selected platform");
    return result;
  }
  moveReviewStage(payload: CoreReviewStageMove) { return this.command("review_stage_move", payload); }
  dismissApprovedCandidate(candidateId: string) { return this.command("approved_candidate_dismiss", { candidate_id: candidateId }); }

  get lastCommandSnapshotSeq() { return this.#fence.lastCommand; }
  get latestAppliedSnapshotRevision() { return this.#fence.latestApplied; }

  async liveStatus(): Promise<CoreWorkbenchLiveStatus> {
    const live = await this.request<CoreWorkbenchLiveStatus>("/api/workbench/live-status");
    const minimumRevision = Math.max(this.#fence.lastCommand, this.#fence.latestApplied);
    if (!live || !Number.isSafeInteger(live.revision) || live.revision < minimumRevision) {
      // Skip this heartbeat and let the existing timer obtain a fresh one. Do
      // not add an immediate retry loop or let old status undo a completed control.
      throw new CollectorCoreProtocolError("Core live status predates the current command or snapshot");
    }
    return live;
  }

  refreshBitBrowserWindows() { return this.command("bitbrowser_refresh", {}); }
  reconnectBitBrowser() { return this.command("bitbrowser_reconnect", {}); }
  openBitBrowserWindow(profileId: string) { return this.command("bitbrowser_open", { profile_id: profileId }); }
  closeBitBrowserWindow(profileId: string) { return this.command("bitbrowser_close", { profile_id: profileId }); }
  openSelectedBitBrowserWindows(profileIds: string[]) { return this.command("bitbrowser_open_selected", { profile_ids: profileIds }); }

  createCollectionTask(payload: CollectionTaskCreatePayload) { return this.command("task_create", payload); }
  checkCompletedCollectionTargets(targets: string[]) {
    return this.command("task_completed_targets_check", { targets });
  }
  addWaitingSplitTargets(targets: string[], allowCompletedTargets = false, allowedWindowIds: string[] = []) {
    return this.command("split_waiting_add", {
      targets,
      allow_completed_targets: allowCompletedTargets,
      allowed_window_ids: allowedWindowIds,
    });
  }
  assignWaitingSplitWindows(candidateId: string, allowedWindowIds: string[]) {
    return this.command("split_waiting_assign_windows", {
      candidate_id: candidateId,
      allowed_window_ids: allowedWindowIds,
    });
  }
  deleteWaitingSplitTarget(candidateId: string) { return this.command("split_waiting_delete", { candidate_id: candidateId }); }
  setWaitingSplitTargetLocked(candidateId: string, locked: boolean) {
    return this.command("split_waiting_lock", { candidate_id: candidateId, locked });
  }
  setSplitClaimLocked(locked: boolean) { return this.command("split_claim_lock", { locked }); }
  requeueFailedSplitTarget(candidateId: string, allowedWindowIds?: string[]) {
    return this.command("split_failure_requeue", {
      candidate_id: candidateId,
      ...(allowedWindowIds ? { allowed_window_ids: allowedWindowIds } : {}),
    });
  }
  deleteFailedSplitTarget(candidateId: string) { return this.command("split_failure_delete", { candidate_id: candidateId }); }
  controlCollectionTask(taskId: string, action: WorkbenchCommandMap["task_control"]["payload"]["action"]) {
    return this.command("task_control", { task_id: taskId, action });
  }
  deleteCollectionTask(taskId: string) { return this.command("task_delete", { task_id: taskId }); }
  retryCollectionNetwork(taskId: string) { return this.command("task_retry_network", { task_id: taskId }); }
  addCollectionTargets(taskId: string, targets: string[]) { return this.command("task_add_targets", { task_id: taskId, targets }); }
  addCollectionWindows(taskId: string, windowIds: string[]) { return this.command("task_add_windows", { task_id: taskId, window_ids: windowIds }); }
  controlCollectionWindow(taskId: string, profileId: string, action: WorkbenchCommandMap["task_window_control"]["payload"]["action"], targetId?: string) {
    return this.command("task_window_control", { task_id: taskId, profile_id: profileId, action, ...(targetId ? { target_id: targetId } : {}) });
  }
  controlCollectionTarget(taskId: string, targetId: string, action: WorkbenchCommandMap["task_target_control"]["payload"]["action"]) {
    return this.command("task_target_control", { task_id: taskId, target_id: targetId, action });
  }
  recheckCollectionSource(taskId: string, targetId: string, mode: "followers" | "following") {
    return this.command("task_source_recheck", { task_id: taskId, target_id: targetId, mode });
  }

  runManualAction(payload: WorkbenchCommandMap["action_manual"]["payload"]) { return this.command("action_manual", payload); }
  startActionCampaign(payload: ActionCampaignStartPayload) { return this.command("action_campaign_start", payload); }
  controlActionCampaign(campaignId: string, action: WorkbenchCommandMap["action_campaign_control"]["payload"]["action"]) {
    return this.command("action_campaign_control", { campaign_id: campaignId, action });
  }
  controlActionTarget(campaignId: string, targetId: string, action: WorkbenchCommandMap["action_target_control"]["payload"]["action"]) {
    return this.command("action_target_control", { campaign_id: campaignId, target_id: targetId, action });
  }
  dismissActionFailure(campaignId: string, targetId: string) {
    return this.command("action_failure_dismiss", { campaign_id: campaignId, target_id: targetId });
  }
  resolveUnknownAction(campaignId: string, targetId: string, outcome: "not_completed" | "completed") {
    return this.command("action_unknown_resolve", { campaign_id: campaignId, target_id: targetId, outcome });
  }
  pauseActiveAction(operation: CoreActionOperation, profileId: string) {
    return this.command("action_pause_active", { operation, profile_id: profileId });
  }
  resetActionCounter(operation: CoreActionOperation, profileId: string) {
    return this.command("action_reset_counter", { operation, profile_id: profileId });
  }
  clearStorageCache() { return this.command("storage_cache_clear", {}); }
  cloudStatus<T>() { return this.request<T>("/api/cloud/status"); }
  cloudCommand<T>(body: Record<string,unknown>) { return this.request<T>("/api/cloud/command",{method:"POST",body}); }
  accountUnread<T>() { return this.request<T>("/api/accounts/unread"); }
  accountSnapshot<T>() { return this.request<T>("/api/accounts/snapshot"); }
  accountCommand(body: Record<string,unknown>) { return this.request<Record<string,unknown>>("/api/accounts/command",{method:"POST",body}); }
  workReport<T>(kind: "activity" | "history", start: string, end: string, options?: {summaryOnly?: boolean}) { return this.request<T>("/api/reports/query", {method:"POST", body:{kind,start,end,...(options?.summaryOnly === true ? {summary_only: true} : {})}}); }
  splitReviewReport<T>(start: string, end: string, offset = 0, limit = 100, utcOffsetMinutes = -new Date().getTimezoneOffset(), dailyBounds?: Array<{key: string; start: string; end: string}>) {
    return this.request<T>("/api/reports/split-review", { method: "POST", body: { start, end, offset, limit, utc_offset_minutes: utcOffsetMinutes, ...(dailyBounds ? {daily_bounds: dailyBounds} : {}) } });
  }
  privateFollowReviewReport<T>(start: string, end: string, offset = 0, limit = 100, utcOffsetMinutes = -new Date().getTimezoneOffset(), dailyBounds?: Array<{key: string; start: string; end: string}>) {
    return this.request<T>("/api/reports/private-follow-review", { method: "POST", body: { start, end, offset, limit, utc_offset_minutes: utcOffsetMinutes, ...(dailyBounds ? {daily_bounds: dailyBounds} : {}) } });
  }
  reportReviewDecision<T>(kind: "split" | "private_follow", recordId: string, decision: "passed" | "failed", start: string, end: string, utcOffsetMinutes: number) {
    return this.request<T>("/api/reports/review-decision", {method:"POST", body:{kind, record_id:recordId, decision, start, end, utc_offset_minutes:utcOffsetMinutes}});
  }
  studioSnapshot<T>() { return this.request<T>("/api/studio/snapshot"); }
  studioCommand(body: Record<string, unknown>) { return this.request<Record<string, unknown>>("/api/studio/command", { method: "POST", body }); }
  followMonitorSnapshot() { return this.request<CoreFollowMonitorSnapshot>("/api/follow-monitor/snapshot"); }
  exportFollowMonitorDiagnostics() {
    return this.request<{ app_version: string; exported_at: string; reports: Array<Record<string, unknown>> }>("/api/follow-monitor/diagnostics");
  }
  startFollowMonitor(profileIds: string[], concurrency: number, checkKind: CoreMonitorKind = "combined") {
    return this.request<{ run_id: string; status: string; total: number }>("/api/follow-monitor/runs", {
      method: "POST",
      body: { profile_ids: profileIds, concurrency, check_kind: checkKind },
    });
  }

  controlFollowMonitor(runId: string, action: "pause" | "resume" | "cancel") {
    return this.request<{ run_id: string; status: string }>("/api/follow-monitor/control", {
      method: "POST", body: { run_id: runId, action },
    });
  }

  configureIntegrations(input: CoreIntegrationConfiguration) {
    return this.#bridge.configureIntegrations(input);
  }

  secureSet(key: string, value: string) { return this.#bridge.secureSet(key, value); }
  secureGet(key: string) { return this.#bridge.secureGet(key); }
  secureDelete(key: string) { return this.#bridge.secureDelete(key); }
  async logout() {
    try {
      return await this.request<void>("/api/session/logout", { method: "POST" });
    } finally {
      this.#snapshotRequests.clear();
      for (const client of coreClientsByBridge.get(this.#bridge)?.values() || []) client.#snapshotRequests.clear();
      this.#fence.latestApplied = 0;
      this.#fence.lastCommand = 0;
    }
  }
}

const coreClientsByBridge = new WeakMap<CollectorCoreBridge, Map<string, CollectorCoreClient>>();

export function createCollectorCoreClient(bridge: CollectorCoreBridge, platform?: CoreSocialPlatform): CollectorCoreClient {
  return new CollectorCoreClient(bridge, platform);
}

export function getCollectorCoreClient(bridge: unknown = bridgeFromGlobal(), platform?: CoreSocialPlatform): CollectorCoreClient {
  const resolved = requireCollectorCoreBridge(bridge);
  let clients = coreClientsByBridge.get(resolved);
  if (!clients) { clients = new Map(); coreClientsByBridge.set(resolved, clients); }
  const existing = clients.get(platform || "default");
  if (existing) return existing;
  const client = new CollectorCoreClient(resolved, platform);
  clients.set(platform || "default", client);
  return client;
}

export function fetchWorkbenchSnapshot(query: SnapshotQuery = {}, bridge: unknown = bridgeFromGlobal()) {
  return getCollectorCoreClient(bridge).snapshot(query);
}

export function executeWorkbenchCommand<T extends WorkbenchCommandType>(
  type: T,
  payload: WorkbenchCommandPayload<T>,
  bridge: unknown = bridgeFromGlobal(),
) {
  return getCollectorCoreClient(bridge).command(type, payload);
}

export function startWorkbenchSnapshotPolling(
  options: SnapshotPollingOptions,
  bridge: unknown = bridgeFromGlobal(),
): SnapshotPollingController {
  const client = getCollectorCoreClient(bridge, options.platform);
  const intervalMs = options.intervalMs ?? 2_500;
  if (!Number.isSafeInteger(intervalMs) || intervalMs < 250 || intervalMs > 300_000) {
    throw new RangeError("Snapshot polling interval must be between 250 and 300000 milliseconds");
  }
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;
  let current: CoreWorkbenchSnapshot | null = null;
  let inFlight: Promise<CoreWorkbenchSnapshot> | null = null;
  let consecutiveFailures = 0;

  const readDeliverableSnapshot = async (): Promise<CoreWorkbenchSnapshot> => {
    // Keep acquisition separate from delivery. A command continuation may run
    // after this await and before deliverSnapshot, where the final fence is
    // checked immediately beside onSnapshot.
    return await client.snapshot(options);
  };

  const deliverSnapshot = async (
    initiallyAcceptedSnapshot: CoreWorkbenchSnapshot,
  ): Promise<CoreWorkbenchSnapshot> => {
    let snapshot = initiallyAcceptedSnapshot;
    let staleRevision = -1;
    let minimumRevision = 0;
    for (
      let attempt = 1;
      attempt <= CollectorCoreClient.staleSnapshotMaxAttempts;
      attempt += 1
    ) {
      // A command can advance its revision fence after snapshot() accepted a
      // response but before the actual delivery callback gets its turn.  The
      // final fence read and onSnapshot call intentionally remain in this same
      // synchronous callback: no Promise boundary may sit between them.
      const requiredRevision = Math.max(
        client.latestAppliedSnapshotRevision,
        client.lastCommandSnapshotSeq,
      );
      if (snapshot.revision >= requiredRevision) {
        if (!stopped) {
          current = snapshot;
          options.onSnapshot(snapshot);
        }
        return snapshot;
      }
      staleRevision = snapshot.revision;
      minimumRevision = requiredRevision;
      if (attempt < CollectorCoreClient.staleSnapshotMaxAttempts) {
        snapshot = await client.snapshot(options);
      }
    }
    throw new CollectorCoreProtocolError(
      `Core snapshot became stale before renderer delivery at revision ${staleRevision}; minimum accepted revision is ${minimumRevision} after ${CollectorCoreClient.staleSnapshotMaxAttempts} attempts`,
    );
  };

  const schedule = () => {
    if (stopped || timer) return;
    timer = setTimeout(() => {
      timer = null;
      void refresh().catch(() => undefined);
    // Repeated failed full reads must give a busy Core time to finish its work.
    // Manual refresh stays immediate; success restores the normal cadence.
    }, Math.max(intervalMs, Math.min(30_000, intervalMs * 2 ** consecutiveFailures)));
  };
  const refresh = (): Promise<CoreWorkbenchSnapshot> => {
    if (stopped) return Promise.reject(new CollectorCoreUnavailableError("Snapshot polling has been stopped"));
    if (inFlight) return inFlight;
    if (timer) clearTimeout(timer);
    timer = null;
    const request = readDeliverableSnapshot()
      .then(deliverSnapshot)
      .then((snapshot) => {
        consecutiveFailures = 0;
        return snapshot;
      })
      .catch((reason: unknown) => {
        consecutiveFailures = Math.min(consecutiveFailures + 1, 7);
        const error = reason instanceof Error ? reason : new Error(String(reason));
        if (!stopped) options.onError?.(error);
        throw error;
      })
      .finally(() => {
        if (inFlight === request) inFlight = null;
        schedule();
      });
    inFlight = request;
    return request;
  };

  const controller: SnapshotPollingController = {
    refresh,
    stop() {
      stopped = true;
      if (timer) clearTimeout(timer);
      timer = null;
    },
    get stopped() { return stopped; },
    get current() { return current; },
  };
  if (options.immediate !== false) void refresh().catch(() => undefined);
  else schedule();
  return controller;
}
