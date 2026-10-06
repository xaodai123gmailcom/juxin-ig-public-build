from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError

from . import __source_revision__, __version__
from .action_manager import ActionCampaignManager
from .async_cleanup import finish_owned
from .owned_requests import OwnedRequests
from .bitbrowser_api import BitBrowserClient
from .config import Settings
from .database import Database
from .errors import (
    AuthenticationError,
    ConflictError,
    DomainError,
    UpstreamUnavailableError,
    ValidationError,
)
from .execution_manager import ExecutionManager
from .follow_monitor import FollowMonitorManager
from .studio import StudioManager
from .posting_workflow import PostingManager
from .account_workspace import AccountWorkspace
from .schemas import (
    CampaignActionRequest,
    BitBrowserProfilesRequest,
    CheckpointRequest,
    DedupeCheckRequest,
    DesktopTaskCreateRequest,
    FollowMonitorStartRequest,
    FollowMonitorControlRequest,
    CompletedTargetsCheckRequest,
    LoginRequest,
    ManualActionRequest,
    PauseActionRequest,
    RegisterRequest,
    ResultRequest,
    SessionResumeRequest,
    SplitCandidatesCategoryRequest,
    SplitCandidateRequeueRequest,
    SplitCandidateWindowsRequest,
    SplitCandidatesQueueRequest,
    SplitCandidatesUpsertRequest,
    TargetsAddRequest,
    TaskControlRequest,
    TaskCreateRequest,
    TaskWindowsAddRequest,
    WorkbenchCampaignControlPayload,
    WorkbenchActionFailureDismissPayload,
    WorkbenchActionUnknownResolvePayload,
    WorkbenchActionTargetControlPayload,
    WorkbenchApprovedCandidateDismissPayload,
    WorkbenchAccountExportRequest,
    WorkbenchCommandRequest,
    WorkbenchCompletedTargetsCheckPayload,
    WorkbenchCreateCandidatePayload,
    WorkbenchDedupeClaimPayload,
    WorkbenchProfileCommandPayload,
    WorkbenchProfilesCommandPayload,
    WorkbenchRecordExclusionPayload,
    WorkbenchReviewDecisionPayload,
    WorkbenchReviewQueueRequest,
    WorkbenchReviewStageMovePayload,
    SplitReviewReportRequest,
    PrivateFollowReviewReportRequest,
    ReportReviewDecisionRequest,
    WorkbenchSplitWaitingAddPayload,
    WorkbenchSplitWaitingAssignWindowsPayload,
    WorkbenchSplitWaitingDeletePayload,
    WorkbenchSplitWaitingLockPayload,
    SplitClaimLockRequest,
    WorkbenchSplitFailureRequeuePayload,
    WorkbenchTaskControlPayload,
    WorkbenchTaskDeletePayload,
    WorkbenchTaskTargetControlPayload,
    WorkbenchTaskSourceRecheckPayload,
    WorkbenchTaskTargetsPayload,
    WorkbenchTaskWindowControlPayload,
    WorkbenchTaskWindowsPayload,
)
from .service import CoreService
from .platform_scope import validate_platform
from .snapshot_runtime import SnapshotInventoryReader, SnapshotJSONResponse, run_transient_maintenance, observe_snapshot_stage, compact_workbench_snapshot

STARTUP_HEADER = "X-Startup-Token"


def get_current_session(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> tuple[dict[str, Any], str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing application session token")
    token = authorization[7:].strip()
    service: CoreService = request.app.state.service
    return service.authenticate(token), token


CurrentSession = Annotated[tuple[dict[str, Any], str], Depends(get_current_session)]


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    bitbrowser: BitBrowserClient | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate_bind()
    database = database or Database(settings.database_path)
    service = CoreService(database, session_hours=settings.session_hours)
    # Private process memory only. The desktop owns encrypted persistence and
    # redaction; provider requests resolve this supplier anew after key changes.
    runtime_pexels = {"key": settings.pexels_api_key or ""}
    os.environ.pop('IGAC_PEXELS_API_KEY', None)  # Do not inherit it into browser subprocesses.

    if bitbrowser is None:
        from .native_browser import BrowserHub, NativeBrowser
        from .embedded_browser import EmbeddedBrowser
        native_type = EmbeddedBrowser if os.environ.get("IGAC_EMBEDDED_BROWSER_URL") else NativeBrowser
        bitbrowser = BrowserHub(native_type(database,settings.data_dir), BitBrowserClient(settings.bitbrowser_url, settings.bitbrowser_api_key))
    execution_manager = ExecutionManager(service, bitbrowser)
    action_manager = ActionCampaignManager(service, bitbrowser)
    follow_monitor = FollowMonitorManager(service, bitbrowser)
    # Legacy Studio posting stays disabled; the separately fenced posting queue
    # below is the only admitted publishing route. Preserve historical records.
    studio = StudioManager(service, bitbrowser, posting_enabled=False)
    posting = PostingManager(service, bitbrowser, key_supplier=lambda: runtime_pexels['key'])
    def cleanup_profile_active(profile_id):
        # Terminal database status is not proof that a coordinator has finished
        # draining. Consult every active manager before reconciling an orphaned
        # historical hold, including after the desktop returns closure evidence.
        collection_ids = execution_manager.active_task_ids()
        action_ids = action_manager.active_campaign_ids()
        monitor_ids = follow_monitor.active_run_ids()
        posting_ids = posting.active_ids()
        with database.read() as c:
            if collection_ids and any(r[0] in collection_ids for r in c.execute(
                    'SELECT task_id FROM task_windows WHERE profile_id=? UNION SELECT task_id FROM task_targets WHERE current_window_id=?',
                    (profile_id,profile_id))):return True
            if action_ids and any(r[0] in action_ids for r in c.execute(
                    'SELECT id FROM action_campaigns WHERE profile_id=?',(profile_id,))):return True
            if monitor_ids and any(r[0] in monitor_ids for r in c.execute(
                    'SELECT run.id FROM follow_monitor_runs run,json_each(run.profile_ids_json) profile WHERE profile.value=?',(profile_id,))):return True
            if posting_ids and any(r[0] in posting_ids for r in c.execute(
                    'SELECT id FROM posting_jobs WHERE profile_id=?',(profile_id,))):return True
        return False
    studio.cleanup_profile_active = cleanup_profile_active
    accounts = AccountWorkspace(service, bitbrowser)
    account_interference_locks: dict[str, asyncio.Lock] = {}
    accounts.surface.manual_control_active = execution_manager.manual_control_active
    execution_manager.manual_control_revoke = accounts.surface.revoke_manual
    from .cloud_workspace import CloudWorkspace
    cloud = CloudWorkspace(service, settings.data_dir, enabled=settings.cloud_enabled,
                           project_url=settings.supabase_url, publishable_key=settings.supabase_publishable_key)
    # A renderer timeout must not start a second heavyweight snapshot while the
    # first request is still finishing synchronous SQLite/BitBrowser work.  Locks
    # are owner-scoped because every snapshot is owner-scoped as well.
    workbench_snapshot_locks: dict[str, asyncio.Lock] = {}
    account_snapshot_locks: dict[str, asyncio.Lock] = {}
    snapshot_inventory = SnapshotInventoryReader(lambda: bitbrowser.list_all_windows())
    owned_requests = OwnedRequests()
    maintenance_stop = asyncio.Event()
    maintenance_task: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal maintenance_task
        owned_requests.start()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        database.acquire_instance_lock()
        try:
            database.initialize()
            service.recover_interrupted_operations()
            posting.recover()
            follow_monitor.recover_interrupted()
            studio.recover()
            accounts.recover()
            studio.start_scheduler()
            posting.start_scheduler()
            cloud.start()
            start_bitbrowser = getattr(bitbrowser, "start", None)
            if callable(start_bitbrowser):
                start_bitbrowser()
            maintenance_task = asyncio.create_task(
                run_transient_maintenance(database, maintenance_stop),
                name="transient-data-maintenance",
            )
            yield
        finally:
            # Close admission synchronously, then shield the whole drain and
            # cleanup sequence so repeated lifespan cancellation cannot skip it.
            owned_requests.draining = True
            async def finish_shutdown():
                # HTTP cancellation does not detach admitted domain work.
                await owned_requests.drain()
                # Notify every manager after admitted commands have settled.
                # Each manager retains ownership until its own cleanup ends.
                maintenance_stop.set()
                cloud.stop_event.set()
                cloud.wake.set()
                await asyncio.to_thread(accounts.restore.capture_before_shutdown)
                outcomes = await asyncio.gather(
                    studio.shutdown(), posting.shutdown(), follow_monitor.shutdown(),
                    execution_manager.shutdown(), action_manager.shutdown(),
                    asyncio.to_thread(cloud.shutdown), snapshot_inventory.close(),
                    *([maintenance_task] if maintenance_task is not None else []),
                    return_exceptions=True,
                )
                import logging
                for result in outcomes:
                    if isinstance(result, BaseException):
                        logging.getLogger(__name__).error("Shutdown stage failed: %s", type(result).__name__)
                shutdown_bitbrowser = getattr(bitbrowser, "shutdown", None)
                if callable(shutdown_bitbrowser):
                    await asyncio.to_thread(shutdown_bitbrowser)
                database.release_instance_lock()
            from .async_cleanup import finish_owned
            await finish_owned(finish_shutdown())

    app = FastAPI(
        title="IG Audience Collector Local Core",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.router.route_class = owned_requests.route_class()
    app.state.owned_requests = owned_requests
    app.state.settings = settings
    app.state.database = database
    app.state.service = service
    app.state.bitbrowser = bitbrowser
    app.state.snapshot_inventory = snapshot_inventory
    app.state.execution_manager = execution_manager
    app.state.action_manager = action_manager
    app.state.studio = studio
    app.state.posting = posting
    app.state.accounts = accounts
    app.state.cloud = cloud

    @app.middleware("http")
    async def require_startup_token(request: Request, call_next):
        supplied = request.headers.get(STARTUP_HEADER, "")
        if not supplied or not secrets.compare_digest(supplied, settings.startup_token):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Invalid startup token",
                    "code": "invalid_startup_token",
                    "error": {"code": "invalid_startup_token", "message": "Invalid startup token"},
                },
            )
        if getattr(app.state, "shutting_down", False) and request.method not in {"GET", "HEAD"} and request.url.path != "/api/internal/shutdown":
            return JSONResponse(status_code=503, content={"detail":"应用正在退出，请等待当前任务保存完成", "code":"core_shutting_down"})
        return await call_next(request)

    @app.post("/api/internal/integrations/pexels")
    async def configure_runtime_pexels(request: Request) -> dict[str, bool]:
        # Startup-token middleware is mandatory; this route is deliberately NOT
        # permitted through the generic renderer HTTP bridge. Manual validation
        # avoids validation libraries echoing a credential in error details.
        import json
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 8192:
                raise ValidationError('配置内容过大')
            chunks.append(chunk)
        try:
            payload = json.loads(b''.join(chunks))
        except (ValueError, UnicodeDecodeError):
            raise ValidationError('配置格式无效') from None
        if not isinstance(payload, dict) or set(payload) != {'pexels_api_key'} or type(payload['pexels_api_key']) is not str:
            raise ValidationError('配置格式无效')
        value = payload['pexels_api_key'].strip()
        if len(value) > 2048 or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise ValidationError('配置格式无效')
        runtime_pexels['key'] = value
        return {'configured': bool(value), 'activated': True}

    @app.post("/api/internal/integrations/cloud")
    async def configure_runtime_cloud(request: Request) -> dict[str, Any]:
        # The main process owns secure persistence. This startup-token-only
        # route updates memory and is excluded from the renderer HTTP bridge.
        import json
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 8192:
                raise ValidationError('配置内容过大')
            chunks.append(chunk)
        try:
            payload = json.loads(b''.join(chunks))
        except (ValueError, UnicodeDecodeError):
            raise ValidationError('配置格式无效') from None
        if (not isinstance(payload, dict) or set(payload) != {'enabled', 'project_url', 'publishable_key'}
                or type(payload['enabled']) is not bool or type(payload['project_url']) is not str
                or type(payload['publishable_key']) is not str):
            raise ValidationError('配置格式无效')
        return await asyncio.to_thread(cloud.configure, **payload)

    @app.post("/api/internal/shutdown")
    async def request_core_shutdown():
        callback = getattr(app.state, "request_shutdown", None)
        if not callable(callback):
            return JSONResponse(status_code=503, content={"accepted":False})
        app.state.shutting_down = True
        asyncio.get_running_loop().call_soon(callback)
        return {"accepted":True}

    @app.exception_handler(DomainError)
    async def handle_domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": exc.message,
                "code": exc.code,
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
            },
        )

    @app.exception_handler(sqlite3.Error)
    async def handle_database_error(_: Request, exc: sqlite3.Error) -> JSONResponse:
        # Real SQLite result codes distinguish a full volume/page limit from
        # contention or damaged storage. Do not guess from timeout text.
        code = getattr(exc, 'sqlite_errorcode', 0) & 0xFF
        if code == sqlite3.SQLITE_FULL:
            status, kind, message = 507, 'storage_full', '磁盘空间或数据库容量不足；本次操作未确认，请释放数据所在磁盘的空间后检查任务状态'
        elif code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            status, kind, message = 503, 'database_busy', '数据库暂时繁忙；本次操作未确认，请等待刷新后检查任务状态'
        elif code == sqlite3.SQLITE_READONLY:
            status, kind, message = 503, 'database_readonly', '数据库无法写入，请检查数据目录的访问权限；本次操作未确认'
        else:
            status, kind, message = 500, 'database_error', '数据库读取或保存失败，请检查磁盘状态；本次操作未确认'
        import logging
        logging.getLogger(__name__).error('Core database error: %s (SQLite %s)', kind, code)
        return JSONResponse(status_code=status, content={
            'detail': message, 'code': kind,
            'error': {'code': kind, 'message': message, 'details': {'sqlite_code': code}},
        })

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ready",
            "source_revision": __source_revision__,
            # This endpoint is the desktop supervisor heartbeat.  Keep it
            # constant-time: a deep PRAGMA quick_check on every heartbeat can
            # stall a large live database and cause a healthy Core to be killed
            # after three false timeouts.  integrity_check remains available to
            # explicit startup/release diagnostics.
            "database": database.liveness_check(),
            "instagram_worker": "configured",
            "collection_platforms": ["instagram"],
            "collection_scheduler": "ready",
            "action_campaigns": "ready",
        }

    @app.post("/v1/auth/register", status_code=201)
    def register(body: RegisterRequest) -> dict[str, Any]:
        return service.register_user(body.username, body.password)

    @app.post("/v1/auth/login")
    def login(body: LoginRequest) -> dict[str, Any]:
        return service.login(
            body.username,
            body.password,
            remember_login=body.remember_login,
            auto_login=body.auto_login,
        )

    @app.post("/v1/auth/logout", status_code=204)
    def logout(session: CurrentSession) -> None:
        _, token = session
        service.logout(token)
        cloud.command(session[0]['id'], {'action':'logout'})

    @app.get("/v1/auth/me")
    def me(session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        return {key: value for key, value in user.items() if key != "session_id"}

    @app.post("/v1/tasks", status_code=201)
    def create_task(body: TaskCreateRequest, session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        return service.create_task(
            user["id"],
            name=body.name,
            modes=list(body.modes),
            targets=body.targets,
            window_ids=body.window_ids,
            settings=body.settings.model_dump(),
            assignment_mode=body.assignment_mode,
        )

    @app.get("/v1/tasks")
    def list_tasks(session: CurrentSession) -> list[dict[str, Any]]:
        user, _ = session
        return service.list_tasks(user["id"])

    @app.get("/v1/tasks/{task_id}")
    def get_task(task_id: str, session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        return service.get_task(user["id"], task_id)

    @app.post("/v1/tasks/{task_id}/targets")
    async def add_targets(task_id: str, body: TargetsAddRequest, session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        return await execution_manager.add_targets(user["id"], task_id, body.targets)

    @app.post("/v1/tasks/{task_id}/start")
    async def start_task(task_id: str, body: TaskControlRequest, session: CurrentSession) -> dict[str, Any]:
        del body
        user, _ = session
        return await execution_manager.start(user["id"], task_id)

    @app.post("/v1/tasks/{task_id}/pause")
    async def pause_task(task_id: str, body: TaskControlRequest, session: CurrentSession) -> dict[str, Any]:
        del body
        user, _ = session
        return await execution_manager.pause(user["id"], task_id)

    @app.post("/v1/tasks/{task_id}/resume")
    async def resume_task(task_id: str, body: TaskControlRequest, session: CurrentSession) -> dict[str, Any]:
        del body
        user, _ = session
        return await execution_manager.resume(user["id"], task_id)

    @app.post("/v1/tasks/{task_id}/stop")
    async def stop_task(task_id: str, body: TaskControlRequest, session: CurrentSession) -> dict[str, Any]:
        del body
        user, _ = session
        return await execution_manager.stop(user["id"], task_id)

    @app.post("/v1/tasks/{task_id}/restart")
    async def restart_task(task_id: str, body: TaskControlRequest, session: CurrentSession) -> dict[str, Any]:
        del body
        user, _ = session
        return await execution_manager.restart(user["id"], task_id)

    @app.put("/v1/tasks/{task_id}/checkpoint")
    def save_checkpoint(task_id: str, body: CheckpointRequest, session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        return service.upsert_checkpoint(
            user["id"],
            task_id,
            body.target_id,
            mode=body.mode,
            stage=body.stage,
            cursor=body.cursor,
            counters=body.counters,
            recoverable=body.recoverable,
        )

    @app.get("/v1/tasks/{task_id}/checkpoints")
    def list_checkpoints(task_id: str, session: CurrentSession) -> list[dict[str, Any]]:
        user, _ = session
        return service.list_checkpoints(user["id"], task_id)

    @app.post("/v1/tasks/{task_id}/results", status_code=201)
    def ingest_result(task_id: str, body: ResultRequest, session: CurrentSession) -> dict[str, Any]:
        """Persist a worker observation; this endpoint never performs an Instagram action."""
        user, _ = session
        return service.record_result(
            user["id"],
            task_id,
            body.target_id,
            username=body.username,
            instagram_user_id=body.instagram_user_id,
            source_mode=body.source_mode,
            visibility=body.visibility,
            profile=body.profile,
            screening=body.screening,
            qualified=body.qualified,
        )

    @app.get("/v1/tasks/{task_id}/results")
    def list_results(task_id: str, session: CurrentSession) -> list[dict[str, Any]]:
        user, _ = session
        return service.list_results(user["id"], task_id)

    @app.post("/v1/dedupe/check")
    def check_dedupe(body: DedupeCheckRequest, _: CurrentSession) -> dict[str, Any]:
        return service.check_global_dedupe(body.username)

    @app.get("/v1/events")
    def events(
        session: CurrentSession,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        user, _ = session
        return service.list_events(user["id"], after_seq=after_seq, limit=limit)

    @app.get("/v1/bitbrowser/health")
    def bitbrowser_health(_: CurrentSession) -> dict[str, Any]:
        return bitbrowser.health()

    @app.get("/v1/bitbrowser/windows")
    def bitbrowser_windows(
        session: CurrentSession,
        page: int = Query(default=0, ge=0),
        page_size: int = Query(default=100, ge=1, le=100),
        name: str = Query(default="", max_length=100),
    ) -> dict[str, Any]:
        listing=bitbrowser.list_all_windows(name=name)
        rows=[r for r in listing['windows'] if r.get('owner_user_id',session[0]['id'])==session[0]['id']]
        return {**listing,'windows':rows[page*page_size:(page+1)*page_size],'total':len(rows),'page':page,'page_size':page_size}

    @app.get("/v1/bitbrowser/windows/{profile_id}/ports")
    def bitbrowser_ports(profile_id: str, session: CurrentSession) -> dict[str, Any]:
        if profile_id.startswith('native:'):bitbrowser.native.assert_owner(session[0]['id'],profile_id)
        return bitbrowser.profile_ports(profile_id)

    @app.post("/v1/bitbrowser/windows/{profile_id}/open")
    def bitbrowser_open(profile_id: str, session: CurrentSession) -> dict[str, Any]:
        return accounts.control_profile(session[0]["id"],profile_id,"open")

    @app.post("/v1/bitbrowser/windows/{profile_id}/close")
    def bitbrowser_close(profile_id: str, session: CurrentSession) -> dict[str, Any]:
        return accounts.control_profile(session[0]["id"],profile_id,"close")

    # Electron renderer compatibility API -------------------------------
    @app.get("/api/health")
    def desktop_health() -> dict[str, Any]:
        return health()

    @app.post("/api/session/register", status_code=201)
    def desktop_register(body: LoginRequest) -> dict[str, Any]:
        service.register_user(body.username, body.password)
        login_result = service.login(
            body.username,
            body.password,
            remember_login=body.remember_login,
            auto_login=body.auto_login,
        )
        return {
            "session_token": login_result["token"],
            "expires_at": login_result["expires_at"],
            "user": login_result["user"],
        }

    @app.post("/api/session/login")
    def desktop_login(body: LoginRequest) -> dict[str, Any]:
        result = service.login(
            body.username,
            body.password,
            remember_login=body.remember_login,
            auto_login=body.auto_login,
        )
        return {
            "session_token": result["token"],
            "expires_at": result["expires_at"],
            "user": result["user"],
        }

    @app.post("/api/session/resume")
    def desktop_resume(body: SessionResumeRequest) -> dict[str, Any]:
        user = service.authenticate(body.session_token)
        return {
            "session_token": body.session_token,
            "user": {key: value for key, value in user.items() if key != "session_id"},
        }

    @app.post("/api/session/logout", status_code=204)
    def desktop_logout(session: CurrentSession) -> None:
        _, token = session
        service.logout(token)
        cloud.command(session[0]['id'], {'action':'logout'})

    def desktop_bitbrowser_profiles_payload(
        user_id: str,
        listing: dict[str, Any],
    ) -> dict[str, Any]:
        from .account_platforms import profile_platforms
        with service.database.read() as c:platform_by_profile=profile_platforms(c, owner_user_id=user_id)
        lease_by_profile = {
            item["profile_id"]: item
            for item in service.list_browser_lease_states(
                user_id,
                active_collection_entity_ids=execution_manager.active_task_ids(),
                active_action_entity_ids=action_manager.active_campaign_ids(),
                active_monitor_entity_ids=follow_monitor.active_run_ids(),
                active_studio_entity_ids=studio.active_ids(),
                active_posting_entity_ids=posting.active_ids(),
                # Both managers publish a startup fence before acquiring leases, so
                # a row absent from those registries is a true residual lock and can
                # be released on the first refresh instead of deselecting the window.
                inactive_grace_seconds=0,
            )
        }
        profiles = [
            {
                "id": item["id"],
                "platform": platform_by_profile.get(item["id"],"instagram"),
                "name": item["name"],
                "group": item.get("group"),
                "serial_number": item.get("serial_number"),
                "provider_order": item.get("provider_order"),
                "opened": item["is_open"],
                "window_state": item.get("window_state"),
                "generation": item.get("generation", 0),
                "ready": item.get("ready", False),
                "opening": item.get("opening", False),
                "locked": item["id"] in lease_by_profile,
                "lock_state": lease_by_profile.get(item["id"], {}).get("state"),
                "lock_operation": lease_by_profile.get(item["id"], {}).get("operation_type"),
                "lock_entity_id": lease_by_profile.get(item["id"], {}).get("entity_id"),
                "lock_owned": lease_by_profile.get(item["id"], {}).get("owned_by_current_login", False),
            }
            for item in listing["windows"]
            if item.get("owner_user_id",user_id)==user_id and platform_by_profile.get(item["id"], "instagram") != "unknown"
        ]
        return {
            "profiles": profiles,
            "total": len(profiles),
            "stale": bool(listing.get("stale", False)),
            # Additive metadata: older renderers safely ignore this field. It lets
            # current clients distinguish a fresh port check from cache/stale data
            # without starting a second high-frequency health probe.
            "connection": listing.get("connection", {}),
        }

    @app.get("/api/bitbrowser/profiles")
    def desktop_bitbrowser_profiles(session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        return desktop_bitbrowser_profiles_payload(user["id"], bitbrowser.list_all_windows())

    @app.post("/api/bitbrowser/reconnect")
    async def desktop_bitbrowser_reconnect(session: CurrentSession) -> dict[str, Any]:
        """Throttled confirmation after the user signs back into BitBrowser."""
        user, _ = session
        result = await asyncio.to_thread(lambda: desktop_bitbrowser_profiles_payload(user["id"], bitbrowser.confirm_login()))
        result['recovery_woken'] = await execution_manager.retry_waiting_windows(user['id'])
        return result

    @app.post("/api/bitbrowser/{profile_id}/open")
    def desktop_bitbrowser_open(profile_id: str, session: CurrentSession) -> dict[str, Any]:
        return accounts.control_profile(session[0]["id"],profile_id,"open")

    @app.post("/api/bitbrowser/{profile_id}/close")
    def desktop_bitbrowser_close(profile_id: str, session: CurrentSession) -> dict[str, Any]:
        return accounts.control_profile(session[0]["id"],profile_id,"close")

    @app.post("/api/bitbrowser/open-selected")
    def desktop_bitbrowser_open_profiles(
        body: BitBrowserProfilesRequest, session: CurrentSession
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for profile_id in dict.fromkeys(body.profile_ids):
            try:
                results.append(accounts.control_profile(session[0]["id"],profile_id,"open"))
            except Exception as exc:
                failures.append({"profile_id": profile_id, "error": str(exc)})
        return {
            "opened": len(results),
            "failed": len(failures),
            "results": results,
            "failures": failures,
        }

    @app.post("/api/tasks", status_code=201)
    def desktop_create_task(body: DesktopTaskCreateRequest, session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        # The retired liker aliases are deliberately not mapped.  This lets a
        # mixed old localStorage payload upgrade to its still-supported modes while
        # a liker-only payload fails below instead of silently running old behavior.
        mode_map = {"followers": "followers", "following": "following"}
        modes: list[str] = []
        for raw_mode in body.modes:
            mapped = mode_map.get(raw_mode)
            if mapped and mapped not in modes:
                modes.append(mapped)
        mode_limits: dict[str, dict[str, int]] = {}
        for raw_mode, values in body.source_limits.items():
            mapped = mode_map.get(raw_mode)
            if not mapped:
                continue
            translated: dict[str, int] = {}
            for source_key, destination_key in {
                "followers": "followers_max",
                "following": "following_max",
                "posts": "posts_max",
                "followersMin": "followers_min",
                "followersMax": "followers_max",
                "followingMin": "following_min",
                "followingMax": "following_max",
                "postsMin": "posts_min",
                "postsMax": "posts_max",
                "activityDays": "active_days_max",
                "perTargetLimit": "per_target_limit",
                # Accept snake_case as well for scripts using the Core API directly.
                "followers_min": "followers_min",
                "followers_max": "followers_max",
                "following_min": "following_min",
                "following_max": "following_max",
                "posts_min": "posts_min",
                "posts_max": "posts_max",
                "active_days_max": "active_days_max",
                "per_target_limit": "per_target_limit",
            }.items():
                if source_key in values:
                    value = values[source_key]
                    # Zero is a persisted first-use placeholder for optional filters,
                    # not an active maximum of zero.  A collection limit is checked
                    # separately because a selected mode cannot execute without one.
                    if destination_key == "per_target_limit" or value > 0:
                        translated[destination_key] = value
            mode_limits[mapped] = translated
        for mode in modes:
            # NewGen relation collection is intentionally unbounded: the source
            # dialog itself decides completion. Ignore stale desktop limits.
            mode_limits.setdefault(mode, {}).pop("per_target_limit", None)
        task = service.create_task(
            user["id"],
            name="采集任务",
            modes=modes,
            targets=body.targets,
            window_ids=body.window_ids,
            settings={
                # Global username dedupe is a database invariant. Keep the request
                # field for older desktop clients, but never allow it to be disabled.
                "dedupe_enabled": True,
                "auto_classify": body.auto_classify,
                # NewGen always performs location after visible count screening.
                # The request schema rejects false; this constant is a second
                # boundary so future callers cannot accidentally reintroduce a
                # bypass while translating the desktop payload.
                "location_enabled": True,
                "gpt_enabled": body.gpt_review,
                "local_person_recognition": body.local_person_recognition,
                "exclude_male_avatar": body.exclude_male_avatar,
                "exclude_verified": body.exclude_verified,
                "exclude_public_zero_posts": body.exclude_public_zero_posts,
                "discard_count_limits_enabled": body.discard_count_limits_enabled,
                "private_discard_followers_max": body.private_discard_followers_max,
                "private_discard_following_max": body.private_discard_following_max,
                "private_discard_posts_max": body.private_discard_posts_max,
                "public_discard_followers_max": body.public_discard_followers_max,
                "public_discard_following_max": body.public_discard_following_max,
                "public_discard_posts_max": body.public_discard_posts_max,
                "public_discard_active_days_max": body.public_discard_active_days_max,
                "parallel_screening_workers": body.parallel_screening_workers,
                "platform": body.platform,
                "live_queue_enabled": True,
                "unlimited_relation_collection": True,
                "mode_limits": mode_limits,
            },
            assignment_mode=body.assignment_mode,
            allow_completed_targets=body.allow_completed_targets,
        )
        if body.assignment_mode == "manual" and body.manual_assignments:
            task = service.set_manual_assignments(
                user["id"], task["id"], body.manual_assignments
            )
        return {"task_id": task["id"], "status": task["status"]}

    @app.post("/api/tasks/completed-targets/check")
    def desktop_check_completed_targets(
        body: CompletedTargetsCheckRequest, session: CurrentSession
    ) -> dict[str, Any]:
        user, _ = session
        return service.check_completed_targets(user["id"], body.targets)

    @app.get("/api/tasks")
    def desktop_tasks(
        session: CurrentSession,
        platform: Literal["instagram"] | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
        offset: int = Query(default=0, ge=0),
        detail_limit: int = Query(default=500, ge=1, le=5000),
    ) -> dict[str, Any]:
        user, _ = session
        tasks = service.list_tasks_with_details(
            user["id"], limit=limit, offset=offset, detail_limit=detail_limit, platform=platform
        )
        total = service.count_tasks(user["id"], platform=platform)
        return {
            "platform": platform,
            "tasks": tasks,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(tasks) < total,
        }

    @app.get("/api/tasks/{task_id}")
    def desktop_task(task_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return service.get_task(user["id"], task_id)

    @app.get("/api/tasks/{task_id}/runtime")
    async def desktop_task_runtime(
        task_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None
    ) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.runtime_diagnostics(user["id"], task_id)

    @app.post("/api/tasks/{task_id}/retry-network-now")
    async def desktop_retry_task_network_now(
        task_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None
    ) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.retry_network_now(user["id"], task_id)

    @app.post("/api/tasks/{task_id}/targets")
    async def desktop_add_targets(
        task_id: str, body: TargetsAddRequest, session: CurrentSession, platform: Literal["instagram"] | None = None
    ) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.add_targets(user["id"], task_id, body.targets)

    @app.post("/api/tasks/{task_id}/windows")
    async def desktop_add_task_windows(
        task_id: str, body: TaskWindowsAddRequest, session: CurrentSession, platform: Literal["instagram"] | None = None
    ) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.add_windows(user["id"], task_id, body.window_ids)

    @app.post("/api/tasks/{task_id}/targets/{target_id}/retry")
    async def desktop_retry_target(
        task_id: str, target_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None
    ) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.retry_target(user["id"], task_id, target_id)

    @app.delete("/api/tasks/{task_id}/targets/{target_id}", status_code=204)
    async def desktop_delete_target(
        task_id: str, target_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None
    ) -> None:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        await execution_manager.delete_target(user["id"], task_id, target_id)

    @app.post("/api/tasks/{task_id}/start")
    async def desktop_start_task(task_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        task = await execution_manager.start(user["id"], task_id)
        return {"task_id": task["id"], "status": task["status"]}

    @app.post("/api/tasks/{task_id}/pause")
    async def desktop_pause_task(task_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.pause(user["id"], task_id)

    @app.post("/api/tasks/{task_id}/resume")
    async def desktop_resume_task(task_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.resume(user["id"], task_id)

    @app.post("/api/tasks/{task_id}/stop")
    async def desktop_stop_task(task_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.stop(user["id"], task_id)

    @app.post("/api/tasks/{task_id}/close")
    async def desktop_close_task(task_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.close_task(user["id"], task_id)

    @app.post("/api/tasks/{task_id}/restart")
    async def desktop_restart_task(task_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.restart(user["id"], task_id)

    @app.post("/api/tasks/{task_id}/windows/{profile_id}/pause")
    async def desktop_pause_task_window(
        task_id: str, profile_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None
    ) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.pause_window(user["id"], task_id, profile_id)

    @app.post("/api/tasks/{task_id}/windows/{profile_id}/resume")
    async def desktop_resume_task_window(
        task_id: str, profile_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None
    ) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.resume_window(user["id"], task_id, profile_id)

    @app.post("/api/tasks/{task_id}/windows/{profile_id}/stop")
    async def desktop_stop_task_window(
        task_id: str, profile_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None
    ) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.stop_window(user["id"], task_id, profile_id)

    @app.post("/api/tasks/{task_id}/windows/{profile_id}/restart")
    async def desktop_restart_task_window(
        task_id: str, profile_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None
    ) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.restart_window(user["id"], task_id, profile_id)

    @app.delete("/api/tasks/{task_id}/windows/{profile_id}")
    async def desktop_delete_task_window(
        task_id: str,
        profile_id: str,
        session: CurrentSession,
        platform: Literal["instagram"] | None = None,
        target_id: str | None = Query(default=None, max_length=128),
        requeue: bool = Query(default=True),
    ) -> dict[str, Any]:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        return await execution_manager.delete_window(
            user["id"], task_id, profile_id, target_id=target_id, requeue=requeue
        )

    @app.delete("/api/tasks/{task_id}", status_code=204)
    async def desktop_delete_task(task_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None) -> None:
        user, _ = session
        service.guard_platform_command(user["id"], "task_control", {"task_id": task_id}, platform)
        task = service.get_task(user["id"], task_id)
        if task["status"] in {"running", "waiting_network", "paused", "recoverable", "queued"}:
            await execution_manager.stop(user["id"], task_id)
        service.delete_task(user["id"], task_id)

    @app.post("/api/actions/manual")
    async def desktop_manual_action(body: ManualActionRequest, session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        return await action_manager.manual_action(
            user["id"],
            operation=body.operation,
            profile_id=body.profile_id,
            target=body.target,
            source_target=body.source_target,
            message=body.message,
        )

    @app.post("/api/actions/campaigns")
    async def desktop_campaign(body: CampaignActionRequest, session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        campaign = await action_manager.start_campaign(
            user["id"],
            operation=body.operation,
            profile_id=body.profile_id,
            targets=body.targets,
            target_sources=body.target_sources,
            message=body.message,
            messages=body.messages,
            interval=body.interval,
            limit=body.limit,
        )
        return {"campaign_id": campaign["id"], "status": campaign["status"]}

    @app.get("/api/actions/campaigns")
    def desktop_action_campaigns(
        session: CurrentSession,
        limit: int = Query(default=500, ge=1, le=5000),
        offset: int = Query(default=0, ge=0),
        detail_limit: int = Query(default=500, ge=1, le=5000),
    ) -> dict[str, Any]:
        user, _ = session
        campaigns = service.list_action_campaigns(
            user["id"], limit=limit, offset=offset, detail_limit=detail_limit
        )
        total = service.count_action_campaigns(user["id"])
        return {
            "campaigns": campaigns,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(campaigns) < total,
        }

    @app.get("/api/actions/counter-resets")
    def desktop_action_counter_resets(
        session: CurrentSession,
        operation: str = Query(default="follow"),
    ) -> dict[str, Any]:
        user, _ = session
        rows = service.list_action_counter_resets(user["id"], operation=operation)
        return {"resets": rows}

    @app.post("/api/actions/counter-resets/{operation}/{profile_id}")
    def desktop_reset_action_counter(
        operation: str,
        profile_id: str,
        session: CurrentSession,
    ) -> dict[str, Any]:
        user, _ = session
        return service.reset_action_counter(user["id"], operation=operation, profile_id=profile_id)

    @app.post("/api/actions/active/pause")
    async def desktop_pause_campaign(body: PauseActionRequest, session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        campaign = await action_manager.pause_active(
            user["id"], profile_id=body.profile_id, operation=body.operation
        )
        return {"campaign_id": campaign["id"], "status": campaign["status"]}

    @app.post("/api/actions/campaigns/{campaign_id}/pause")
    async def desktop_pause_campaign_by_id(campaign_id: str, session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        campaign = await action_manager.pause(user["id"], campaign_id)
        return {"campaign_id": campaign["id"], "status": campaign["status"]}

    @app.post("/api/actions/campaigns/{campaign_id}/resume")
    async def desktop_resume_campaign(campaign_id: str, session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        campaign = await action_manager.resume(user["id"], campaign_id)
        return {"campaign_id": campaign["id"], "status": campaign["status"]}

    @app.post("/api/actions/campaigns/{campaign_id}/stop")
    async def desktop_stop_campaign(campaign_id: str, session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        campaign = await action_manager.stop(user["id"], campaign_id)
        return {"campaign_id": campaign["id"], "status": campaign["status"]}

    @app.get("/api/split-candidates")
    def desktop_split_candidates(session: CurrentSession, platform: Literal["instagram"] | None = None) -> dict[str, Any]:
        user, _ = session
        rows = service.list_split_candidates(user["id"], platform=platform)
        return {"candidates": rows, "total": len(rows)}

    @app.post("/api/split-candidates")
    async def desktop_save_split_candidates(
        body: SplitCandidatesUpsertRequest, session: CurrentSession,
        platform: Literal["instagram"] = "instagram",
    ) -> dict[str, Any]:
        user, _ = session
        outcome = service.upsert_manual_split_candidates(
            user["id"],
            [item.model_dump(exclude_unset=True) for item in body.candidates],
            include_outcome=True,
            platform=platform,
        )
        if outcome["accepted_count"] and any(item.queued for item in body.candidates):
            execution_manager.notify_split_queue(user["id"])
        return outcome

    @app.post("/api/split-candidates/queue")
    async def desktop_queue_split_candidates(
        body: SplitCandidatesQueueRequest, session: CurrentSession,
        platform: Literal["instagram"] | None = None,
    ) -> dict[str, Any]:
        service.guard_platform_command(session[0]["id"], "split_queue", {"candidate_ids": body.candidate_ids}, platform)
        user, _ = session
        outcome = service.mark_split_candidates_queued(
            user["id"], body.candidate_ids, include_outcome=True
        )
        if outcome["accepted_count"]:
            execution_manager.notify_split_queue(user["id"])
        return outcome

    @app.patch("/api/split-candidates/category")
    def desktop_set_split_candidate_category(
        body: SplitCandidatesCategoryRequest, session: CurrentSession,
        platform: Literal["instagram"] | None = None,
    ) -> dict[str, Any]:
        service.guard_platform_command(session[0]["id"], "split_category", {"candidate_ids": body.candidate_ids}, platform)
        user, _ = session
        rows = service.set_split_candidate_manual_category(
            user["id"],
            body.candidate_ids,
            body.category,
            affected_only=True,
        )
        return {"candidates": rows, "total": len(rows)}

    @app.patch("/api/split-candidates/{candidate_id}/windows")
    async def desktop_set_split_candidate_windows(
        candidate_id: str,
        body: SplitCandidateWindowsRequest,
        session: CurrentSession,
        platform: Literal["instagram"] | None = None,
    ) -> dict[str, Any]:
        service.guard_platform_command(session[0]["id"], "split_waiting_assign_windows", {"candidate_id": candidate_id}, platform)
        user, _ = session
        candidate = service.set_split_candidate_allowed_windows(
            user["id"], candidate_id, body.allowed_window_ids
        )
        execution_manager.notify_split_queue(user["id"])
        return {"candidate": candidate}

    @app.patch("/api/split-candidates/{candidate_id}/lock")
    async def desktop_split_candidate_lock(candidate_id: str, body: SplitClaimLockRequest, session: CurrentSession,
        platform: Literal["instagram"] | None = None,
    ):
        service.guard_platform_command(session[0]["id"], "split_waiting_lock", {"candidate_id": candidate_id}, platform)
        owner = session[0]["id"]
        candidate = await asyncio.to_thread(service.set_split_candidate_locked, owner, candidate_id, body.locked)
        execution_manager.notify_split_queue(owner)
        return {"candidate": candidate}

    @app.put("/api/split-candidates/claim-lock")
    async def desktop_split_claim_lock(body: SplitClaimLockRequest, session: CurrentSession):
        owner = session[0]["id"]
        result = await asyncio.to_thread(service.set_split_claim_locked, owner, body.locked)
        execution_manager.notify_split_queue(owner)
        return result

    @app.post("/api/split-candidates/{candidate_id}/requeue")
    async def desktop_requeue_split_candidate(
        candidate_id: str,
        session: CurrentSession,
        body: SplitCandidateRequeueRequest | None = None,
        platform: Literal["instagram"] | None = None,
    ) -> dict[str, Any]:
        service.guard_platform_command(session[0]["id"], "split_failure_requeue", {"candidate_id": candidate_id}, platform)
        user, _ = session
        candidate = service.requeue_split_candidate(
            user["id"],
            candidate_id,
            allowed_window_ids=(body.allowed_window_ids if body else None),
        )
        execution_manager.notify_split_queue(user["id"], returned_candidate=candidate)
        return {"candidate": candidate}

    @app.delete("/api/split-candidates/{candidate_id}")
    def desktop_delete_failed_split_candidate(
        candidate_id: str, session: CurrentSession,
        platform: Literal["instagram"] | None = None,
    ) -> dict[str, Any]:
        service.guard_platform_command(session[0]["id"], "split_failure_delete", {"candidate_id": candidate_id}, platform)
        user, _ = session
        return service.delete_failed_split_candidate(user["id"], candidate_id)

    @app.delete("/api/split-candidates/{candidate_id}/waiting")
    def desktop_delete_waiting_split_candidate(
        candidate_id: str, session: CurrentSession,
        platform: Literal["instagram"] | None = None,
    ) -> dict[str, Any]:
        service.guard_platform_command(session[0]["id"], "split_waiting_delete", {"candidate_id": candidate_id}, platform)
        user, _ = session
        return service.delete_waiting_split_candidate(user["id"], candidate_id)

    @app.get("/api/results")
    def desktop_results(
        session: CurrentSession,
        platform: Literal["instagram"] | None = None,
        limit: int = Query(default=500, ge=1, le=5000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        user, _ = session
        rows = service.list_all_results(user["id"], limit=limit, offset=offset, platform=platform)
        total = service.count_all_results(user["id"], platform=platform)
        return {
            "platform": platform,
            "results": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }

    @app.get("/api/history")
    def desktop_history(
        session: CurrentSession,
        platform: Literal["instagram"] | None = None,
        task_id: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=500, ge=1, le=5000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        user, _ = session
        rows = service.list_history(
            user["id"], task_id=task_id, limit=limit, offset=offset, platform=platform
        )
        total = service.count_history(user["id"], task_id=task_id, platform=platform)
        return {
            "platform": platform,
            "history": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }

    @app.delete("/api/history/{target_id}", status_code=204)
    async def desktop_delete_history_target(target_id: str, session: CurrentSession, platform: Literal["instagram"] | None = None) -> None:
        user, _ = session
        row = service.get_history_target_state(user["id"], target_id, platform=platform)
        if row["task_status"] in {"running", "waiting_network", "paused", "recoverable", "queued"}:
            await execution_manager.stop(user["id"], row["task_id"])
        service.delete_history_target(user["id"], target_id, platform=platform)

    def parse_workbench_payload(model: Any, payload: dict[str, Any]) -> Any:
        try:
            return model.model_validate(payload)
        except PydanticValidationError as exc:
            raise ValidationError(
                "Invalid workbench command payload",
                details={
                    "errors": exc.errors(
                        include_url=False,
                        include_context=False,
                        include_input=False,
                    )
                },
            ) from exc

    def workbench_response(command: str, result: Any) -> dict[str, Any]:
        result_revision = result.get("snapshot_seq") if isinstance(result, dict) else None
        revision = (
            int(result_revision)
            if isinstance(result_revision, int) and result_revision >= 0
            else service.get_workbench_revision()
        )
        return {"command": command, "result": result, "snapshot_seq": revision}

    async def _build_workbench_snapshot(
        request: Request,
        user: dict[str, Any],
        *,
        limit: int,
        history_limit: int,
        platform: str | None = None,
    ) -> dict[str, Any]:
        async def abandon_if_client_left() -> None:
            # Electron aborts local requests after 30 seconds. Polling the ASGI
            # disconnect signal between bounded stages prevents that abandoned
            # request from continuing through every later snapshot query.
            if await request.is_disconnected():
                raise UpstreamUnavailableError(
                    "工作台快照请求已结束，请等待下一次自动刷新",
                    details={"reason": "workbench_snapshot_client_disconnected"},
                )

        # Observe and check disconnects between expensive reads. A renderer that
        # already timed out must not start the remaining history scans.
        snapshot = await observe_snapshot_stage('business_rows', asyncio.to_thread(
            service.get_workbench_snapshot, user["id"], limit=limit,
            history_limit=history_limit, maintain=False, platform=platform))
        await abandon_if_client_left()
        tasks = await observe_snapshot_stage('task_rows', asyncio.to_thread(
            service.list_tasks_with_details, user["id"], limit=limit + 1,
            detail_limit=limit + 1, platform=platform))
        await abandon_if_client_left()
        campaigns = await observe_snapshot_stage('action_rows', asyncio.to_thread(
            service.list_action_campaigns, user["id"], limit=history_limit + 1,
            detail_limit=history_limit + 1))
        await abandon_if_client_left()
        active_tasks = [
            task for task in tasks
            if task.get("status") in {"queued", "running", "waiting_network", "paused", "recoverable"}
        ]
        if active_tasks:
            # A long-running installation can retain thousands of operational rows.
            # Build diagnostics in bounded groups instead of creating thousands of
            # queued coroutines at once.  Each task's durable status is already in
            # this snapshot, so runtime diagnostics never repeats the full SQLite
            # task/detail query.  Disconnect checks between groups promptly abandon
            # an Electron request whose local timeout has already fired.
            for offset in range(0, len(active_tasks), 8):
                await abandon_if_client_left()
                batch = active_tasks[offset : offset + 8]
                diagnostics = await asyncio.gather(
                    *(
                        execution_manager.runtime_diagnostics(
                            user["id"],
                            task["id"],
                            known_status=str(task["status"]),
                        )
                        for task in batch
                    ),
                    return_exceptions=True,
                )
                for task, diagnostic in zip(batch, diagnostics):
                    if isinstance(diagnostic, dict):
                        task["runtime"] = diagnostic
                await abandon_if_client_left()
        sources: list[dict[str, Any]] = []
        for task in tasks:
            for target in task.get("targets", []):
                mode_progress = target.get("mode_progress") or {}
                processed = sum(
                    int(progress.get("processed") or 0)
                    for progress in mode_progress.values()
                    if isinstance(progress, dict)
                )
                totals = [
                    int(progress["source_total"])
                    for progress in mode_progress.values()
                    if isinstance(progress, dict)
                    and isinstance(progress.get("source_total"), int)
                ]
                sources.append(
                    {
                        "id": target["id"],
                        "task_id": task["id"],
                        "username": target["username"],
                        "modes": list(task.get("modes", [])),
                        "status": target["status"],
                        "current_window_id": target.get("current_window_id"),
                        "processed": processed,
                        "total": sum(totals) if totals else None,
                        "mode_progress": mode_progress,
                        "mode_coverage": target.get("mode_coverage") or {},
                        "created_at": target.get("created_at"),
                        "updated_at": target.get("updated_at"),
                    }
                )

        try:
            listing = await snapshot_inventory.read()
            profile_payload = await observe_snapshot_stage('window_ownership', asyncio.to_thread(
                desktop_bitbrowser_profiles_payload, user["id"], listing
            ))
            windows = profile_payload["profiles"]
            connection_state = profile_payload.get("connection", {})
        except DomainError as exc:
            # The workbench and its SQLite history remain usable while BitBrowser
            # is signed out/offline.  This is a real unavailable state, not a demo
            # window list or a fabricated connected flag.
            windows = []
            connection_state = {
                "state": "unavailable",
                "connected": False,
                "error_code": exc.code,
                "message": exc.message,
            }
        await abandon_if_client_left()

        window_action_counts = await observe_snapshot_stage('window_counts', asyncio.to_thread(
            service.get_window_action_counts,
            user["id"],
            tuple(window.get("id", "") for window in windows),
        ))
        await abandon_if_client_left()
        action_counts_by_window = {
            item["profile_id"]: item for item in window_action_counts
        }
        for window in windows:
            window["action_counts"] = action_counts_by_window.get(
                window.get("id"),
                {
                    "profile_id": window.get("id"),
                    "greet_successes": 0,
                    "follow_successes": 0,
                    "total_successes": 0,
                    "greet_current": 0,
                    "follow_current": 0,
                    "greet_reset_at": None,
                    "follow_reset_at": None,
                },
            )
        snapshot.setdefault("dedupe", {})["window_action_counts"] = (
            window_action_counts
        )

        # Fetch one sentinel from each split-candidate priority bucket.  Failures
        # and live/manual work sort ahead of immutable completion history, then
        # the combined page is bounded to the operational snapshot limit.
        split_candidate_page = await observe_snapshot_stage('split_rows', asyncio.to_thread(
            service.list_split_candidates,
            user["id"],
            row_limit=limit + 1,
            completed_limit=history_limit + 1,
            platform=platform,
        ))
        await abandon_if_client_left()
        completed_split_candidates = [
            candidate
            for candidate in split_candidate_page
            if candidate.get("real_lifecycle_state") == "completed"
        ]
        visible_completed_split_candidates = completed_split_candidates[
            :history_limit
        ]
        visible_split_candidates = [
            candidate
            for candidate in split_candidate_page
            if candidate.get("real_lifecycle_state") != "completed"
        ] + visible_completed_split_candidates
        visible_split_candidates = visible_split_candidates[:limit]
        split_candidate_total = int(
            snapshot.get("counts", {}).get("total_split", 0)
        )
        split_candidates_have_more = bool(
            len(split_candidate_page) > limit
            or len(completed_split_candidates) > history_limit
            or split_candidate_total > len(visible_completed_split_candidates)
        )

        has_more = snapshot.get("has_more", {})
        task_details_truncated = any(
            bool(task.get("targets_truncated") or task.get("windows_truncated"))
            for task in tasks
        )
        campaign_details_truncated = any(
            bool(
                campaign.get("targets_truncated")
                or campaign.get("attempts_truncated")
            )
            for campaign in campaigns
        )
        campaign_counts_by_operation = await asyncio.to_thread(
            service.count_action_campaigns_by_operation, user["id"]
        )
        await abandon_if_client_left()
        visible_campaigns = campaigns[:history_limit]

        def operation_campaigns_truncated(operation: str) -> bool:
            visible = [
                campaign
                for campaign in visible_campaigns
                if campaign.get("operation") == operation
            ]
            return bool(
                campaign_counts_by_operation.get(operation, 0) > len(visible)
                or any(
                    campaign.get("targets_truncated")
                    or campaign.get("attempts_truncated")
                    for campaign in visible
                )
            )

        visible_action_successes = snapshot.get("action_success_history", [])

        def operation_successes_truncated(operation: str) -> bool:
            total_key = (
                "greet_successes" if operation == "greet" else "follow_successes"
            )
            visible_count = sum(
                1
                for success in visible_action_successes
                if success.get("operation") == operation
            )
            return int(snapshot.get("dedupe", {}).get(total_key, 0)) > visible_count

        operational_truncated = bool(
            len(tasks) > limit
            or len(campaigns) > history_limit
            or task_details_truncated
            or campaign_details_truncated
        )
        has_more.update(
            {
                "tasks": len(tasks) > limit or task_details_truncated,
                "campaigns": (
                    len(campaigns) > history_limit or campaign_details_truncated
                ),
                "greet_campaigns": operation_campaigns_truncated("greet"),
                "follow_campaigns": operation_campaigns_truncated("follow"),
                "greet_action_success_history": operation_successes_truncated(
                    "greet"
                ),
                "follow_action_success_history": operation_successes_truncated(
                    "follow"
                ),
                "actions": bool(
                    has_more.get("action_success_history")
                    or len(campaigns) > history_limit
                    or campaign_details_truncated
                ),
                "sources": len(sources) > limit,
                "split_candidates": split_candidates_have_more,
            }
        )
        snapshot.update(
            {
                "revision": snapshot["snapshot_seq"],
                "pending": {
                    "public": snapshot["pending_public_accounts"],
                    "private": snapshot["pending_private_accounts"],
                },
                "approved": {
                    "public": snapshot["approved_public_accounts"],
                    "private": snapshot["approved_private_accounts"],
                },
                "history": {
                    "manual_rejections": snapshot["manual_rejection_history"],
                    "collection_exclusions": snapshot["collection_exclusion_history"],
                    "approvals": snapshot.get("approval_history", []),
                    # Immutable successes are ledger-backed synthetic action
                    # rows produced by CoreService. Terminal campaigns remain
                    # for failure/cancellation audit, but their bounded target
                    # details are never the source of successful history.
                    "actions": snapshot.get("action_success_history", []) + [
                        campaign
                        for campaign in campaigns[:history_limit]
                        if campaign.get("status") in {"completed", "failed", "stopped"}
                    ],
                    "tasks": [
                        task
                        for task in tasks[:history_limit]
                        if task.get("status") in {"completed", "failed", "stopped"}
                    ],
                },
                "windows": windows,
                "sources": sources[:limit],
                "split_candidates": visible_split_candidates,
                "split_claim_locked": await asyncio.to_thread(service.get_split_claim_locked, user["id"]),
                "tasks": tasks[:limit],
                "campaigns": campaigns[:history_limit],
                "connection": connection_state,
                "has_more": has_more,
                "truncated": bool(
                    operational_truncated
                    or len(sources) > limit
                    or any(bool(value) for value in has_more.values())
                ),
            }
        )
        return snapshot

    @app.get("/api/workbench/snapshot")
    async def workbench_snapshot(
        request: Request,
        session: CurrentSession,
        limit: int = Query(default=500, ge=1, le=5000),
        history_limit: int = Query(default=500, ge=1, le=5000),
        platform: Literal["instagram"] | None = None,
        compact: Literal["1"] | None = None,
    ) -> JSONResponse:
        user, _ = session
        owner_id = str(user["id"])
        snapshot_lock = workbench_snapshot_locks.setdefault(
            owner_id, asyncio.Lock()
        )
        async with snapshot_lock:
            # Requests which timed out while waiting for a prior snapshot must not
            # start another build. This keeps the default executor and BitBrowser
            # inventory path bounded to one snapshot generation per owner.
            if await request.is_disconnected():
                raise UpstreamUnavailableError(
                    "工作台快照请求已结束，请等待下一次自动刷新",
                    details={"reason": "workbench_snapshot_client_disconnected"},
                )
            # Cancellation cannot stop SQLite threads. Retain the owner fence
            # until the build truly settles, so a renderer retry cannot overlap it.
            snapshot = await finish_owned(_build_workbench_snapshot(
                request,
                user,
                limit=limit,
                history_limit=history_limit,
                platform=platform,
            ))
            if compact == "1":
                snapshot = compact_workbench_snapshot(snapshot)
            # Returning a dict makes FastAPI traverse/encode the complete history
            # on the collection event loop. Build the actual response in a worker
            # and keep the owner fence until that worker has really finished.
            return await finish_owned(observe_snapshot_stage('response_encoding',
                asyncio.to_thread(SnapshotJSONResponse, content=snapshot)))

    @app.get("/api/cloud/status")
    async def cloud_status(session: CurrentSession):
        return await asyncio.to_thread(cloud.status,session[0]["id"])

    @app.post("/api/cloud/command")
    async def cloud_command(payload: dict[str,Any], session: CurrentSession):
        return await asyncio.to_thread(cloud.command,session[0]["id"],payload)

    @app.get("/api/accounts/snapshot")
    async def account_workspace_snapshot(session: CurrentSession, request: Request) -> JSONResponse:
        owner_id = str(session[0]["id"])
        # A remount/retry must not add another slow account read to the shared
        # executor. Keep this separate from the full-workbench fence so that
        # account controls remain independent of slow history snapshots.
        async with account_snapshot_locks.setdefault(owner_id, asyncio.Lock()):
            async def check_connected():
                if await request.is_disconnected():
                    raise UpstreamUnavailableError(
                        "账号窗口读取请求已结束，请等待下一次自动刷新",
                        details={"reason": "account_snapshot_client_disconnected"},
                    )

            await check_connected()
            accounts.restore.start(owner_id)
            listing = await snapshot_inventory.read()
            await check_connected()
            # Cancellation cannot stop SQLite/encoding threads. Retain the
            # fence until the real worker settles; never cache durable leases.
            snapshot = await finish_owned(observe_snapshot_stage('account_rows',
                asyncio.to_thread(accounts.snapshot, owner_id, window_listing=listing)))
            await check_connected()
            return await finish_owned(observe_snapshot_stage('account_response_encoding',
                asyncio.to_thread(SnapshotJSONResponse, content=snapshot)))

    @app.get("/api/accounts/unread")
    async def account_unread_snapshot(session: CurrentSession) -> dict[str, Any]:
        accounts.restore.start(session[0]["id"])
        return await asyncio.to_thread(accounts.unread_snapshot,session[0]["id"])

    @app.post("/api/accounts/watch")
    async def account_watch(payload: dict[str,Any], session: CurrentSession):
        return await asyncio.to_thread(accounts.watch_task,session[0]["id"],str(payload.get('id','')),payload.get('target'))

    @app.post("/api/accounts/close-task-page")
    async def account_close_task_page(payload: dict[str,Any], session: CurrentSession):
        owner=session[0]['id']
        info=await asyncio.to_thread(accounts.task_page_close_info,owner,str(payload.get('id','')),payload.get('task_key'),payload.get('target'))
        await execution_manager.pause_for_page_close(owner,info['row']['profile_id'],info['lease_token'])
        return await asyncio.to_thread(accounts.close_task_page,owner,info)

    @app.post("/api/accounts/interference")
    async def account_interference(payload: dict[str,Any], session: CurrentSession):
        owner=session[0]["id"];ident=str(payload.get('id',''))
        row=await asyncio.to_thread(accounts.get,owner,ident)
        async with account_interference_locks.setdefault(row['profile_id'],asyncio.Lock()):
            info=await asyncio.to_thread(accounts.task_page_close_info,owner,ident,payload.get('task_key'),payload.get('target'),operation_label='干扰')
            await execution_manager.begin_manual_control(owner,info['row']['profile_id'],info['lease_token'])
            # A recovery may retire the selected page while the task stops.
            current=await asyncio.to_thread(accounts.task_page_close_info,owner,ident,payload.get('task_key'),payload.get('target'),operation_label='干扰')
            if current['generation']!=info['generation'] or current['lease_token']!=info['lease_token']:
                raise ConflictError('任务页面已变化，请重新选择画面后操作')
            return await asyncio.to_thread(accounts.surface.permit_interference,owner,current['row'],payload.get('task_key'),payload.get('target'))

    @app.post("/api/accounts/interference/resume")
    async def account_interference_resume(payload: dict[str,Any], session: CurrentSession):
        owner=session[0]["id"];row=await asyncio.to_thread(accounts.get,owner,str(payload.get('id','')))
        async with account_interference_locks.setdefault(row['profile_id'],asyncio.Lock()):
            token=await asyncio.to_thread(accounts.surface.revoke_manual_grant,owner,row,payload.get('task_key'),payload.get('grant'))
            result=await execution_manager.end_manual_control(owner,row['profile_id'],token)
            return {**result,'resumed':True,'message':'已结束手动干扰，按原任务进度继续。'}

    @app.post("/api/accounts/surface")
    async def account_surface(payload: dict[str, Any],session: CurrentSession) -> dict[str, Any]:
        owner=session[0]["id"]
        row=await asyncio.to_thread(accounts.get,owner,str(payload.get('id',''))) if payload.get('visible') else None
        return await asyncio.to_thread(accounts.surface.update,owner,row,payload)

    @app.post("/api/accounts/command")
    async def account_workspace_command(payload: dict[str, Any],session: CurrentSession) -> dict[str, Any]:
        return await asyncio.to_thread(accounts.command,session[0]["id"],payload)

    @app.post("/api/reports/query")
    async def query_work_report(payload: dict[str, Any], session: CurrentSession) -> dict[str, Any]:
        from .work_reports import work_report, history_totals
        if payload.get("kind") not in {"activity", "history"}:
            raise ValidationError("无效报表类型")
        method = history_totals if payload["kind"] == "history" else work_report
        summary_only = payload.get("summary_only", False)
        if type(summary_only) is not bool or (payload["kind"] == "history" and summary_only):
            raise ValidationError("无效报表汇总选项")
        options = {"summary_only": summary_only} if payload["kind"] == "activity" else {}
        return await asyncio.to_thread(method, service.database, str(session[0]["id"]), payload.get("start"), payload.get("end"), platform=validate_platform(payload.get("platform")), **options)

    @app.get("/api/posting/snapshot")
    async def posting_snapshot(session: CurrentSession, timezone: str = "UTC", cursor: str | None = Query(default=None, max_length=256), limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        return await asyncio.to_thread(posting.snapshot, str(session[0]['id']), timezone, cursor=cursor, limit=limit)

    @app.post("/api/posting/command")
    async def posting_command(payload: dict[str, Any], session: CurrentSession) -> dict[str, Any]:
        validate_platform(payload.get('platform'))
        return await posting.command(str(session[0]['id']), payload)

    @app.get("/api/studio/snapshot")
    async def studio_snapshot(session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        return await asyncio.to_thread(studio.snapshot, str(user["id"]))

    @app.post("/api/studio/command")
    async def studio_command(payload: dict[str, Any], session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        if payload.get('action') == 'locate_cleanup_collection':
            from .nurture_collection_blocker import locate_collection_blocker
            return await asyncio.to_thread(locate_collection_blocker, studio, execution_manager, str(user['id']), str(payload.get('job_id', '')))
        if payload.get('action') == 'stop_cleanup_collection':
            return await execution_manager.stop_nurture_collection_blocker(
                studio, str(user['id']), str(payload.get('job_id', '')),
                payload.get('task_id'), payload.get('version'))
        return await studio.command(str(user["id"]), payload)

    @app.get("/api/follow-monitor/snapshot")
    async def follow_monitor_snapshot(session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        return await asyncio.to_thread(follow_monitor.snapshot, str(user["id"]))

    @app.post("/api/follow-monitor/runs")
    async def start_follow_monitor_run(
        payload: FollowMonitorStartRequest,
        session: CurrentSession,
    ) -> dict[str, Any]:
        user, _ = session
        return await follow_monitor.start(
            str(user["id"]), payload.profile_ids, concurrency=payload.concurrency, check_kind=payload.check_kind
        )

    @app.post("/api/follow-monitor/control")
    async def control_follow_monitor_run(
        payload: FollowMonitorControlRequest,
        session: CurrentSession,
    ) -> dict[str, Any]:
        user, _ = session
        return await follow_monitor.control(str(user["id"]), payload.run_id, payload.action)

    @app.get("/api/follow-monitor/diagnostics")
    async def export_follow_monitor_diagnostics(session: CurrentSession) -> dict[str, Any]:
        user, _ = session
        return await asyncio.to_thread(follow_monitor.diagnostics.export, str(user["id"]))

    @app.get("/api/workbench/live-status")
    async def workbench_live_status(session: CurrentSession) -> dict[str, Any]:
        """Return a lightweight active-task heartbeat for renderer reconciliation.

        This endpoint deliberately avoids BitBrowser inventory refreshes and all
        history/spool scans.  Only tasks that currently have an execution-manager
        control are read, so the UI can follow progress every few seconds without
        competing with collection workers for browser or database resources.
        """
        user, _ = session
        owner_user_id = str(user["id"])
        # Stamp the observation before reading task rows. A pause/stop may commit
        # while a slow read is in flight; its newer revision must not be attached
        # to the older captured status and accepted as a post-command heartbeat.
        revision = await asyncio.to_thread(service.get_workbench_revision)
        observed_at = datetime.now(timezone.utc).isoformat()
        active_ids = tuple(execution_manager.active_task_ids())

        async def load_one(task_id: str) -> dict[str, Any] | None:
            try:
                status = await asyncio.to_thread(
                    service.get_task_status, owner_user_id, task_id
                )
                runtime = await execution_manager.runtime_diagnostics(
                    owner_user_id, task_id, known_status=status
                )
                # Completed history keeps its last window as evidence. Only the
                # live runtime can identify terminal targets whose cleanup still
                # owns a window; preserve those alongside newly bound targets.
                current_target_ids = tuple(dict.fromkeys(
                    state["current_target_id"]
                    for state in runtime.get("profile_states", [])
                    if isinstance(state.get("current_target_id"), str)
                    and state["current_target_id"]
                ))
                task = await asyncio.to_thread(
                    service.get_task_live_status, owner_user_id, task_id,
                    current_target_ids=current_target_ids,
                )
            except DomainError:
                return None
            return {"task": task, "runtime": runtime}

        rows = await asyncio.gather(*(load_one(task_id) for task_id in active_ids))
        return {
            "generated_at": observed_at,
            "revision": revision,
            "tasks": [row for row in rows if row is not None],
        }

    @app.get("/api/workbench/dedupe/stats")
    def workbench_dedupe_stats(session: CurrentSession, platform: Literal["instagram"] | None = None) -> dict[str, int]:
        user, _ = session
        return service.get_workbench_dedupe_stats(user["id"], platform=platform)

    @app.get("/api/workbench/storage")
    def workbench_storage_status(_: CurrentSession) -> dict[str, Any]:
        # Independent, read-only diagnosis remains available if full history
        # projection fails. This performs no cleanup, inventory RPC or task work.
        return service.get_storage_status()

    @app.post("/api/workbench/accounts/export")
    async def workbench_account_export(
        body: WorkbenchAccountExportRequest, session: CurrentSession,
    ) -> dict[str, Any]:
        from .account_exports import export_accounts
        return await asyncio.to_thread(
            export_accounts, service.database, session[0]["id"], **body.model_dump()
        )

    @app.post("/api/workbench/review/query")
    async def workbench_review_query(
        body: WorkbenchReviewQueueRequest, session: CurrentSession,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            service.query_review_queue, session[0]["id"], **body.model_dump()
        )

    @app.post("/api/reports/split-review")
    async def query_split_review_report(
        body: SplitReviewReportRequest, session: CurrentSession,
    ) -> dict[str, Any]:
        from .work_reports import split_review_report
        return await asyncio.to_thread(
            split_review_report, service.database, session[0]["id"], **body.model_dump()
        )

    @app.post("/api/reports/private-follow-review")
    async def query_private_follow_review_report(
        body: PrivateFollowReviewReportRequest, session: CurrentSession,
    ) -> dict[str, Any]:
        from .work_reports import private_follow_review_report
        return await asyncio.to_thread(
            private_follow_review_report, service.database, session[0]["id"], **body.model_dump()
        )

    @app.post("/api/reports/review-decision")
    async def save_report_review_decision(
        body: ReportReviewDecisionRequest, session: CurrentSession,
    ) -> dict[str, Any]:
        from .work_reports import set_report_review_decision
        return await asyncio.to_thread(
            set_report_review_decision, service.database, session[0]["id"], **body.model_dump()
        )

    @app.post("/api/workbench/commands")
    async def workbench_command(
        body: WorkbenchCommandRequest,
        session: CurrentSession,
    ) -> dict[str, Any]:
        async def execute_owned_command():
            # Synchronous database/browser operations run outside the collection
            # event loop. Keep the complete command owned until its mutation,
            # queue notification and response revision settle, even if its HTTP
            # caller disconnects or cancellation arrives repeatedly.
            user, _ = session
            command = body.command
            command_payload = dict(body.payload)
            platform = command_payload.get("platform")
            platform = validate_platform(platform)
            await asyncio.to_thread(service.guard_platform_command, user["id"], command, command_payload, platform)
            platform_options = {"platform": platform} if platform is not None else {}
            if command != "task_create":
                command_payload.pop("platform", None)

            if command == "dedupe_claim":
                payload = parse_workbench_payload(WorkbenchDedupeClaimPayload, command_payload)
                result = await asyncio.to_thread(service.claim_workbench_identity,
                    user["id"],
                    username=payload.username,
                    instagram_user_id=payload.instagram_user_id,
                    source=payload.source,
                    source_target=payload.source_target,
                )
            elif command == "create_candidate":
                payload = parse_workbench_payload(WorkbenchCreateCandidatePayload, command_payload)
                result = await asyncio.to_thread(service.create_workbench_candidate,
                    user["id"],
                    claim_id=payload.claim_id,
                    username=payload.username,
                    visibility=payload.visibility,
                    profile=payload.profile,
                    screening=payload.screening,
                    review_cache=payload.review_cache,
                    source_mode=payload.source_mode,
                    source_target=payload.source_target,
                )
            elif command == "record_exclusion":
                payload = parse_workbench_payload(WorkbenchRecordExclusionPayload, command_payload)
                result = await asyncio.to_thread(service.record_workbench_exclusion,
                    user["id"],
                    claim_id=payload.claim_id,
                    username=payload.username,
                    reason_code=payload.reason_code,
                    reason=payload.reason,
                    location_country=payload.location_country,
                    profile=payload.profile,
                )
            elif command == "review_decision":
                payload = parse_workbench_payload(WorkbenchReviewDecisionPayload, command_payload)
                result = await asyncio.to_thread(service.decide_workbench_candidate,
                    user["id"],
                    candidate_id=payload.candidate_id,
                    decision=payload.decision,
                    **platform_options,
                )
            elif command == "review_stage_move":
                payload = parse_workbench_payload(WorkbenchReviewStageMovePayload, command_payload)
                result = await asyncio.to_thread(
                    service.move_review_stage, user["id"], **payload.model_dump(), **platform_options
                )
            elif command == "bitbrowser_refresh":
                if command_payload:
                    raise ValidationError("bitbrowser_refresh payload must be empty")
                profile_result = await asyncio.to_thread(
                    lambda: desktop_bitbrowser_profiles_payload(
                        user["id"], bitbrowser.list_all_windows()
                    )
                )
                result = {
                    "windows": profile_result["profiles"],
                    "connection": profile_result.get("connection", {}),
                    "stale": profile_result.get("stale", False),
                }
            elif command == "bitbrowser_reconnect":
                if command_payload:
                    raise ValidationError("bitbrowser_reconnect payload must be empty")
                profile_result = await desktop_bitbrowser_reconnect(session)
                result = {
                    "windows": profile_result["profiles"],
                    "connection": profile_result.get("connection", {}),
                    "stale": profile_result.get("stale", False),
                    "recovery_woken": profile_result.get("recovery_woken", 0),
                }
            elif command in {"bitbrowser_open", "bitbrowser_close"}:
                payload = parse_workbench_payload(WorkbenchProfileCommandPayload, command_payload)
                result = await asyncio.to_thread(accounts.control_profile,user["id"],payload.profile_id,"open" if command=="bitbrowser_open" else "close")
            elif command == "bitbrowser_open_selected":
                payload = parse_workbench_payload(WorkbenchProfilesCommandPayload, command_payload)
                result = await asyncio.to_thread(desktop_bitbrowser_open_profiles,
                    BitBrowserProfilesRequest(profile_ids=payload.profile_ids), session
                )
            elif command == "task_create":
                payload = parse_workbench_payload(DesktopTaskCreateRequest, command_payload)
                result = await asyncio.to_thread(desktop_create_task, payload, session)
            elif command == "task_completed_targets_check":
                payload = parse_workbench_payload(
                    WorkbenchCompletedTargetsCheckPayload, command_payload
                )
                result = await asyncio.to_thread(service.check_completed_targets, user["id"], payload.targets)
            elif command == "split_waiting_add":
                payload = parse_workbench_payload(
                    WorkbenchSplitWaitingAddPayload, command_payload
                )
                result = await asyncio.to_thread(service.upsert_manual_split_candidates,
                    user["id"],
                    [
                        {
                            "username": target,
                            "queued": True,
                            "allowed_window_ids": payload.allowed_window_ids,
                        }
                        for target in payload.targets
                    ],
                    include_outcome=True,
                    allow_completed=payload.allow_completed_targets,
                    platform=platform or "instagram",
                )
                execution_manager.notify_split_queue(user["id"])
            elif command == "split_waiting_assign_windows":
                payload = parse_workbench_payload(
                    WorkbenchSplitWaitingAssignWindowsPayload, command_payload
                )
                result = await asyncio.to_thread(service.set_split_candidate_allowed_windows,
                    user["id"], payload.candidate_id, payload.allowed_window_ids
                )
                execution_manager.notify_split_queue(user["id"])
            elif command == "split_waiting_lock":
                payload = parse_workbench_payload(WorkbenchSplitWaitingLockPayload, command_payload)
                candidate = await asyncio.to_thread(service.set_split_candidate_locked, user["id"], payload.candidate_id, payload.locked)
                execution_manager.notify_split_queue(user["id"])
                result = {"candidate": candidate}
            elif command == "split_claim_lock":
                payload = parse_workbench_payload(SplitClaimLockRequest, command_payload)
                result = await asyncio.to_thread(service.set_split_claim_locked, user["id"], payload.locked)
                execution_manager.notify_split_queue(user["id"])
            elif command == "split_waiting_delete":
                payload = parse_workbench_payload(
                    WorkbenchSplitWaitingDeletePayload, command_payload
                )
                result = await asyncio.to_thread(service.delete_waiting_split_candidate,
                    user["id"], payload.candidate_id
                )
            elif command == "split_failure_requeue":
                payload = parse_workbench_payload(
                    WorkbenchSplitFailureRequeuePayload, command_payload
                )
                result = await asyncio.to_thread(service.requeue_split_candidate,
                    user["id"],
                    payload.candidate_id,
                    allowed_window_ids=payload.allowed_window_ids,
                )
                execution_manager.notify_split_queue(user["id"], returned_candidate=result)
            elif command == "split_failure_delete":
                payload = parse_workbench_payload(
                    WorkbenchSplitWaitingDeletePayload, command_payload
                )
                result = await asyncio.to_thread(service.delete_failed_split_candidate,
                    user["id"], payload.candidate_id
                )
            elif command == "task_control":
                payload = parse_workbench_payload(WorkbenchTaskControlPayload, command_payload)
                controls = {
                    "start": desktop_start_task,
                    "pause": desktop_pause_task,
                    "resume": desktop_resume_task,
                    "stop": desktop_stop_task,
                    "restart": desktop_restart_task,
                    "close": desktop_close_task,
                }
                result = await controls[payload.action](payload.task_id, session)
            elif command == "task_delete":
                payload = parse_workbench_payload(WorkbenchTaskDeletePayload, command_payload)
                task = await asyncio.to_thread(service.get_task, user["id"], payload.task_id)
                if task["status"] in {"running", "waiting_network", "paused", "recoverable", "queued"}:
                    await execution_manager.stop(user["id"], payload.task_id)
                await asyncio.to_thread(service.dismiss_task_from_list, user["id"], payload.task_id)
                result = {
                    "task_id": payload.task_id,
                    "status": "deleted",
                    "global_dedupe_retained": True,
                }
            elif command == "task_retry_network":
                payload = parse_workbench_payload(WorkbenchTaskDeletePayload, command_payload)
                result = await execution_manager.retry_network_now(user["id"], payload.task_id)
            elif command == "task_add_targets":
                payload = parse_workbench_payload(WorkbenchTaskTargetsPayload, command_payload)
                result = await desktop_add_targets(
                    payload.task_id, TargetsAddRequest(targets=payload.targets), session
                )
            elif command == "task_add_windows":
                payload = parse_workbench_payload(WorkbenchTaskWindowsPayload, command_payload)
                result = await desktop_add_task_windows(
                    payload.task_id,
                    TaskWindowsAddRequest(window_ids=payload.window_ids),
                    session,
                )
            elif command == "task_window_control":
                payload = parse_workbench_payload(WorkbenchTaskWindowControlPayload, command_payload)
                if payload.action in {"delete", "delete_only"}:
                    result = await execution_manager.delete_window(
                        user["id"],
                        payload.task_id,
                        payload.profile_id,
                        target_id=payload.target_id,
                        requeue=payload.action != "delete_only",
                    )
                elif payload.action in {"pause", "stop"}:
                    window_controls = {
                        "pause": execution_manager.pause_window,
                        "stop": execution_manager.stop_window,
                    }
                    result = await window_controls[payload.action](
                        user["id"], payload.task_id, payload.profile_id,
                        expected_target_id=payload.target_id,
                    )
                else:
                    window_controls = {
                        "resume": execution_manager.resume_window,
                        "restart": execution_manager.restart_window,
                    }
                    result = await window_controls[payload.action](
                        user["id"], payload.task_id, payload.profile_id,
                        expected_target_id=payload.target_id,
                    )
            elif command == "task_source_recheck":
                payload = parse_workbench_payload(WorkbenchTaskSourceRecheckPayload, command_payload)
                result = await execution_manager.recheck_source(
                    user["id"], payload.task_id, payload.target_id, payload.mode
                )
            elif command == "task_target_control":
                payload = parse_workbench_payload(WorkbenchTaskTargetControlPayload, command_payload)
                if payload.action == "retry":
                    result = await execution_manager.retry_target(
                        user["id"], payload.task_id, payload.target_id
                    )
                elif payload.action == "dismiss_completed":
                    result = await execution_manager.dismiss_completed_target(
                        user["id"], payload.task_id, payload.target_id
                    )
                else:
                    await execution_manager.delete_target(
                        user["id"], payload.task_id, payload.target_id
                    )
                    result = {
                        "task_id": payload.task_id,
                        "target_id": payload.target_id,
                        "deleted": True,
                    }
            elif command == "action_manual":
                payload = parse_workbench_payload(ManualActionRequest, command_payload)
                campaign = await action_manager.submit_manual_action(
                    user["id"],
                    operation=payload.operation,
                    profile_id=payload.profile_id,
                    target=payload.target,
                    source_target=payload.source_target,
                    message=payload.message,
                )
                result = {"campaign_id": campaign["id"], "status": campaign["status"]}
            elif command == "action_campaign_start":
                payload = parse_workbench_payload(CampaignActionRequest, command_payload)
                campaign = await action_manager.start_campaign(
                    user["id"],
                    operation=payload.operation,
                    profile_id=payload.profile_id,
                    targets=payload.targets,
                    target_sources=payload.target_sources,
                    message=payload.message,
                    messages=payload.messages,
                    interval=payload.interval,
                    limit=payload.limit,
                )
                result = {"campaign_id": campaign["id"], "status": campaign["status"]}
            elif command == "action_campaign_control":
                payload = parse_workbench_payload(WorkbenchCampaignControlPayload, command_payload)
                campaign_controls = {
                    "pause": action_manager.pause,
                    "resume": action_manager.resume,
                    "stop": action_manager.stop,
                }
                campaign = await campaign_controls[payload.action](
                    user["id"], payload.campaign_id
                )
                result = {"campaign_id": campaign["id"], "status": campaign["status"]}
            elif command in {"action_pause_active", "action_reset_counter"}:
                payload = parse_workbench_payload(PauseActionRequest, command_payload)
                if command == "action_pause_active":
                    campaign = await action_manager.pause_active(
                        user["id"],
                        profile_id=payload.profile_id,
                        operation=payload.operation,
                    )
                    result = {"campaign_id": campaign["id"], "status": campaign["status"]}
                else:
                    result = await asyncio.to_thread(service.reset_action_counter,
                        user["id"],
                        operation=payload.operation,
                        profile_id=payload.profile_id,
                    )
            elif command == "action_failure_dismiss":
                payload = parse_workbench_payload(
                    WorkbenchActionFailureDismissPayload, command_payload
                )
                result = await asyncio.to_thread(service.dismiss_action_failure,
                    user["id"], payload.campaign_id, payload.target_id
                )
            elif command == "action_unknown_resolve":
                payload = parse_workbench_payload(
                    WorkbenchActionUnknownResolvePayload, command_payload
                )
                result = await asyncio.to_thread(service.resolve_unknown_action,
                    user["id"],
                    payload.campaign_id,
                    payload.target_id,
                    outcome=payload.outcome,
                )
            elif command == "action_target_control":
                payload = parse_workbench_payload(
                    WorkbenchActionTargetControlPayload, command_payload
                )
                result = await action_manager.control_target(
                    user["id"],
                    payload.campaign_id,
                    payload.target_id,
                    action=payload.action,
                )
            elif command == "approved_candidate_dismiss":
                payload = parse_workbench_payload(
                    WorkbenchApprovedCandidateDismissPayload, command_payload
                )
                result = await asyncio.to_thread(service.dismiss_approved_candidate,
                    user["id"], payload.candidate_id, **platform_options
                )
            elif command == "storage_cache_clear":
                if command_payload:
                    raise ValidationError("storage_cache_clear payload must be empty")
                result = await asyncio.to_thread(service.clear_workbench_cache, user["id"])
            else:  # pragma: no cover - Literal validation makes this unreachable.
                raise ValidationError("Unknown workbench command")

            if command in {
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
            }:
                await asyncio.to_thread(service.advance_workbench_revision)
            return await asyncio.to_thread(workbench_response, command, result)

        return await finish_owned(execute_owned_command())

    return app
