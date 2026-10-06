from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictBool, model_validator

from .person_recognition import normalize_person_category_fields


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(StrictModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=10, max_length=1024)


class LoginRequest(RegisterRequest):
    remember_login: bool = False
    auto_login: bool = False


class TaskSettingsRequest(StrictModel):
    platform: Literal["instagram"] = "instagram"
    # Zero is the unconfigured first-use value.  The execution layer treats a
    # zero screening threshold as disabled; positive values are opt-in filters.
    followers_min: int | None = Field(default=0, ge=0)
    followers_max: int | None = Field(default=0, ge=0)
    following_min: int | None = Field(default=0, ge=0)
    following_max: int | None = Field(default=0, ge=0)
    posts_min: int | None = Field(default=0, ge=0)
    posts_max: int | None = Field(default=0, ge=0)
    active_days_max: int | None = Field(default=0, ge=0)
    # Accepted only so a stale desktop payload upgrades cleanly.  CoreService
    # removes these retired post-liker settings before persistence.
    like_posts_to_check: int = Field(default=0, ge=0, le=20)
    max_likers_per_post: int | None = Field(default=0, ge=0)
    # Location is a mandatory screening gate after visible count screening. Literal[True]
    # makes attempts to bypass it fail during request validation.
    location_enabled: Literal[True] = True
    gpt_enabled: bool = False
    local_person_recognition: bool = False
    # Legacy/internal task creation remains opt-in.  New desktop tasks expose a
    # separate fixed-threshold switch and persist it explicitly.
    exclude_male_avatar: bool = False
    live_queue_enabled: bool = False
    exclude_verified: bool = False
    # Match the desktop and restored-task default. Only confirmed public zero
    # profiles are discarded; a saved False explicitly disables this shortcut.
    exclude_public_zero_posts: bool = True
    # Independent public/private collection discard limits; zero disables a field.
    discard_count_limits_enabled: bool = Field(default=True, strict=True)
    private_discard_followers_max: int = Field(default=4000, ge=0, strict=True)
    private_discard_following_max: int = Field(default=4000, ge=0, strict=True)
    private_discard_posts_max: int = Field(default=4000, ge=0, strict=True)
    public_discard_followers_max: int = Field(default=4000, ge=0, strict=True)
    public_discard_following_max: int = Field(default=4000, ge=0, strict=True)
    public_discard_posts_max: int = Field(default=4000, ge=0, strict=True)
    # Separate from qualification activity limits; zero preserves legacy behavior.
    public_discard_active_days_max: int = Field(default=0, ge=0, strict=True)
    # One relationship source page plus 1-3 isolated screening pages.
    parallel_screening_workers: int = Field(default=1, ge=1, le=3, strict=True)

    @model_validator(mode="after")
    def validate_ranges(self) -> "TaskSettingsRequest":
        for minimum, maximum in (
            (self.followers_min, self.followers_max),
            (self.following_min, self.following_max),
            (self.posts_min, self.posts_max),
        ):
            # Zero is the UI's persisted "not configured" sentinel, not an
            # explicit maximum of zero accounts.
            if minimum not in (None, 0) and maximum not in (None, 0) and minimum > maximum:
                raise ValueError("A minimum setting cannot exceed its maximum")
        return self


class TaskCreateRequest(StrictModel):
    name: str = Field(default="采集任务", min_length=1, max_length=100)
    # A plain string list lets CoreService safely discard the two retired aliases
    # (``likes``/``post_likers``) from an old saved client configuration while it
    # still rejects every genuinely unknown mode.
    modes: list[str] = Field(min_length=1)
    targets: list[str] = Field(default_factory=list)
    window_ids: list[str] = Field(min_length=1)
    settings: TaskSettingsRequest = Field(default_factory=TaskSettingsRequest)
    assignment_mode: Literal["sequential", "manual"] = "sequential"


class TargetsAddRequest(StrictModel):
    targets: list[str] = Field(min_length=1)


class TaskWindowsAddRequest(StrictModel):
    window_ids: list[str] = Field(min_length=1)


class SplitCandidateItem(StrictModel):
    username: str
    source_target: str | None = Field(default=None, max_length=150)
    profile: dict[str, Any] = Field(default_factory=dict)
    queued: bool = False
    allowed_window_ids: list[str] | None = None


class SplitCandidatesUpsertRequest(StrictModel):
    candidates: list[SplitCandidateItem] = Field(min_length=1)


class SplitCandidatesQueueRequest(StrictModel):
    candidate_ids: list[str] = Field(min_length=1)


class SplitCandidatesCategoryRequest(StrictModel):
    candidate_ids: list[str] = Field(min_length=1)
    category: Literal["waiting", "running", "completed"] | None = None


class SplitCandidateWindowsRequest(StrictModel):
    allowed_window_ids: list[str]


class SplitCandidateRequeueRequest(StrictModel):
    allowed_window_ids: list[str] | None = None


class BitBrowserProfilesRequest(StrictModel):
    profile_ids: list[str] = Field(min_length=1)


class TaskControlRequest(StrictModel):
    expected_version: int | None = Field(default=None, ge=1)


class CheckpointRequest(StrictModel):
    target_id: str
    # Legacy workers may finish/checkpoint an already-persisted liker task during
    # an in-place upgrade. New task creation no longer accepts this mode.
    mode: Literal["followers", "following", "post_likers"]
    stage: str = Field(min_length=1, max_length=100)
    cursor: dict[str, Any] = Field(default_factory=dict)
    counters: dict[str, Any] = Field(default_factory=dict)
    recoverable: bool = True


class ResultRequest(StrictModel):
    target_id: str
    username: str
    instagram_user_id: str | None = None
    source_mode: Literal["followers", "following", "post_likers"]
    visibility: Literal["public", "private", "unknown", "not_visible"] = "unknown"
    profile: dict[str, Any] = Field(default_factory=dict)
    screening: dict[str, Any] = Field(default_factory=dict)
    qualified: bool | None = None

    @model_validator(mode="after")
    def normalize_person_category_payload(self) -> "ResultRequest":
        self.profile, self.screening = normalize_person_category_fields(
            self.profile,
            self.screening,
        )
        return self


class DedupeCheckRequest(StrictModel):
    username: str


class WorkbenchCommandRequest(StrictModel):
    """Single desktop/IPC mutation envelope for the new-generation workbench."""

    command: Literal[
        "dedupe_claim",
        "create_candidate",
        "record_exclusion",
        "review_decision",
        "review_stage_move",
        "bitbrowser_refresh",
        "bitbrowser_reconnect",
        "bitbrowser_open",
        "bitbrowser_close",
        "bitbrowser_open_selected",
        "task_create",
        "task_completed_targets_check",
        "split_waiting_add",
        "split_waiting_assign_windows",
        "split_waiting_delete",
        "split_waiting_lock",
        "split_claim_lock",
        "split_failure_requeue",
        "split_failure_delete",
        "task_control",
        "task_delete",
        "task_retry_network",
        "task_add_targets",
        "task_add_windows",
        "task_window_control",
        "task_target_control",
        "task_source_recheck",
        "action_manual",
        "action_campaign_start",
        "action_campaign_control",
        "action_pause_active",
        "action_reset_counter",
        "action_failure_dismiss",
        "action_unknown_resolve",
        "action_target_control",
        "approved_candidate_dismiss",
        "storage_cache_clear",
    ] = Field(validation_alias=AliasChoices("command", "type"))
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkbenchDedupeClaimPayload(StrictModel):
    username: str
    instagram_user_id: str | None = Field(default=None, max_length=100)
    source: str = Field(default="collection", min_length=1, max_length=100)
    source_target: str | None = Field(default=None, max_length=150)


class WorkbenchCreateCandidatePayload(StrictModel):
    claim_id: str = Field(
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("claim_id", "claim_account_id"),
    )
    username: str | None = None
    visibility: Literal["public", "private"]
    profile: dict[str, Any] = Field(default_factory=dict)
    screening: dict[str, Any] = Field(default_factory=dict)
    review_cache: dict[str, Any] = Field(default_factory=dict)
    source_mode: str | None = Field(default=None, max_length=50)
    source_target: str | None = Field(default=None, max_length=150)


class WorkbenchRecordExclusionPayload(StrictModel):
    claim_id: str = Field(
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("claim_id", "claim_account_id"),
    )
    username: str | None = None
    reason_code: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)
    location_country: str | None = Field(default=None, max_length=100)
    profile: dict[str, Any] = Field(default_factory=dict)


class WorkbenchReviewDecisionPayload(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=100)
    decision: Literal["approved", "rejected"]


class WorkbenchAccountExportRequest(StrictModel):
    platform: Literal["instagram"] | None = None
    visibility: Literal["public", "private"]
    scope: Literal["selected", "all"]
    candidate_ids: list[str] | None = Field(default=None, max_length=5000)


class WorkbenchReviewQueueRequest(StrictModel):
    platform: Literal["instagram"] | None = None
    visibility: Literal["public", "private"]
    review_stage: Literal[1, 2]
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)


class WorkbenchReviewStageMovePayload(StrictModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=2000)
    visibility: Literal["public", "private"]
    from_stage: Literal[1, 2]
    to_stage: Literal[1, 2]


class ReviewDailyBounds(StrictModel):
    key: str = Field(min_length=10, max_length=10)
    start: str = Field(min_length=1, max_length=50)
    end: str = Field(min_length=1, max_length=50)


class SplitReviewReportRequest(StrictModel):
    platform: Literal["instagram"] | None = None
    utc_offset_minutes: int = Field(ge=-840, le=840, strict=True)
    start: str = Field(min_length=1, max_length=50)
    end: str = Field(min_length=1, max_length=50)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)
    daily_bounds: list[ReviewDailyBounds] | None = Field(default=None, min_length=7, max_length=7)


class PrivateFollowReviewReportRequest(SplitReviewReportRequest):
    pass


class ReportReviewDecisionRequest(StrictModel):
    platform: Literal["instagram"] | None = None
    kind: Literal['split', 'private_follow']
    record_id: str = Field(min_length=1, max_length=160)
    decision: Literal['passed', 'failed']
    start: str = Field(min_length=1, max_length=50)
    end: str = Field(min_length=1, max_length=50)
    utc_offset_minutes: int = Field(ge=-840, le=840, strict=True)


class WorkbenchProfileCommandPayload(StrictModel):
    profile_id: str = Field(min_length=1, max_length=128)


class WorkbenchCompletedTargetsCheckPayload(StrictModel):
    targets: list[str] = Field(min_length=1)


class WorkbenchSplitWaitingAddPayload(StrictModel):
    targets: list[str] = Field(min_length=1)
    allow_completed_targets: bool = False
    allowed_window_ids: list[str] | None = None


class WorkbenchSplitWaitingAssignWindowsPayload(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=100)
    allowed_window_ids: list[str]


class SplitClaimLockRequest(StrictModel):
    locked: StrictBool


class WorkbenchSplitWaitingLockPayload(SplitClaimLockRequest):
    candidate_id: str = Field(min_length=1, max_length=128)


class WorkbenchSplitWaitingDeletePayload(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=100)


class WorkbenchSplitFailureRequeuePayload(WorkbenchSplitWaitingDeletePayload):
    allowed_window_ids: list[str] | None = None


class WorkbenchProfilesCommandPayload(StrictModel):
    profile_ids: list[str] = Field(min_length=1)


class WorkbenchTaskControlPayload(StrictModel):
    task_id: str = Field(min_length=1, max_length=128)
    action: Literal["start", "pause", "resume", "stop", "restart", "close"]


class WorkbenchTaskDeletePayload(StrictModel):
    task_id: str = Field(min_length=1, max_length=128)


class WorkbenchTaskTargetsPayload(StrictModel):
    task_id: str = Field(min_length=1, max_length=128)
    targets: list[str] = Field(min_length=1)


class WorkbenchTaskWindowsPayload(StrictModel):
    task_id: str = Field(min_length=1, max_length=128)
    window_ids: list[str] = Field(min_length=1)


class WorkbenchTaskWindowControlPayload(StrictModel):
    task_id: str = Field(min_length=1, max_length=128)
    profile_id: str = Field(min_length=1, max_length=128)
    action: Literal["pause", "resume", "stop", "restart", "delete", "delete_only"]
    target_id: str | None = Field(default=None, max_length=128)


class WorkbenchTaskTargetControlPayload(StrictModel):
    task_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=128)
    action: Literal["retry", "delete", "dismiss_completed"]


class WorkbenchTaskSourceRecheckPayload(StrictModel):
    task_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=128)
    mode: Literal["followers", "following"]


class WorkbenchCampaignControlPayload(StrictModel):
    campaign_id: str = Field(min_length=1, max_length=128)
    action: Literal["pause", "resume", "stop"]


class WorkbenchActionFailureDismissPayload(StrictModel):
    campaign_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=128)


class WorkbenchActionUnknownResolvePayload(StrictModel):
    campaign_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=128)
    outcome: Literal["not_completed", "completed"]


class WorkbenchActionTargetControlPayload(StrictModel):
    campaign_id: str = Field(min_length=1, max_length=128)
    target_id: str = Field(min_length=1, max_length=128)
    action: Literal["pause", "resume", "cancel"]


class WorkbenchApprovedCandidateDismissPayload(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=128)


class SessionResumeRequest(StrictModel):
    session_token: str = Field(min_length=20, max_length=500)


class DesktopTaskCreateRequest(StrictModel):
    platform: Literal["instagram"] = "instagram"
    # A split-only live task may start with no materialized target.  Core validates
    # that at least one durable split candidate is actually waiting before accepting
    # this shape, so an empty ordinary task is still rejected.
    targets: list[str] = Field(default_factory=list)
    window_ids: list[str] = Field(min_length=1)
    modes: list[str] = Field(min_length=1)
    source_limits: dict[str, dict[str, int]] = Field(default_factory=dict)
    # Legacy shared limit. NewGen relationship collection runs until the visible
    # Instagram list reaches its natural end, but older clients may still send a
    # non-negative value and must not fail merely because it exceeds an obsolete
    # application cap.
    per_target_limit: int = Field(default=0, ge=0)
    # Retired fields remain parseable at zero so stale localStorage cannot make an
    # otherwise valid followers/following request fail during upgrade.
    like_posts_to_check: int = Field(default=0, ge=0, le=20)
    max_likers_per_post: int = Field(default=0, ge=0)
    assignment_mode: Literal["sequential", "manual"] = "sequential"
    manual_assignments: dict[str, str] = Field(default_factory=dict)
    dedupe: bool = True
    # Retired switches remain parseable for older clients; service normalization
    # always disables inference and gender filtering.
    local_person_recognition: bool = False
    exclude_male_avatar: bool = False
    # Existing, independent public/private auxiliary classification switch.
    auto_classify: bool = False
    read_location: Literal[True] = True
    gpt_review: bool = False
    exclude_verified: bool = False
    # Public-only zero-post switch. Private and unknown profiles retain review.
    exclude_public_zero_posts: bool = True
    discard_count_limits_enabled: bool = Field(default=True, strict=True)
    private_discard_followers_max: int = Field(default=4000, ge=0, strict=True)
    private_discard_following_max: int = Field(default=4000, ge=0, strict=True)
    private_discard_posts_max: int = Field(default=4000, ge=0, strict=True)
    public_discard_followers_max: int = Field(default=4000, ge=0, strict=True)
    public_discard_following_max: int = Field(default=4000, ge=0, strict=True)
    public_discard_posts_max: int = Field(default=4000, ge=0, strict=True)
    public_discard_active_days_max: int = Field(default=0, ge=0, strict=True)
    # UI choices are 1-1, 1-2 and 1-3.
    parallel_screening_workers: int = Field(default=1, ge=1, le=3, strict=True)
    # Re-collecting a source account that already reached completed is always
    # an explicit user decision.  False is the safe default for every caller.
    allow_completed_targets: bool = False


class CompletedTargetsCheckRequest(StrictModel):
    targets: list[str] = Field(min_length=1)


class FollowMonitorStartRequest(StrictModel):
    check_kind: Literal["combined", "following", "dm"] = "combined"
    profile_ids: list[str] = Field(min_length=1)
    concurrency: int = Field(default=2, ge=1, strict=True)


class FollowMonitorControlRequest(StrictModel):
    run_id: str = Field(min_length=1, max_length=128)
    action: Literal["pause", "resume", "cancel"]


class ManualActionRequest(StrictModel):
    operation: Literal["follow", "greet"]
    profile_id: str = Field(min_length=1, max_length=128)
    target: str
    source_target: str | None = Field(default=None, max_length=150)
    message: str | None = Field(default=None, max_length=200)


class CampaignActionRequest(StrictModel):
    operation: Literal["follow", "greet"]
    profile_id: str = Field(min_length=1, max_length=128)
    targets: list[str] = Field(min_length=1)
    target_sources: dict[str, str] = Field(default_factory=dict)
    message: str | None = Field(default=None, max_length=200)
    # `message` remains accepted for installed clients from older releases.
    # New clients send an arbitrary-size message library; individual entries
    # are normalized and validated by CoreService before persistence.
    messages: list[str] = Field(default_factory=list)
    interval: str
    limit: int = Field(ge=1)


class PauseActionRequest(StrictModel):
    operation: Literal["follow", "greet"]
    profile_id: str = Field(min_length=1, max_length=128)
