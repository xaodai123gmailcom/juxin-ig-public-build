from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .async_cleanup import finish_owned
from .browser_cleanup import disconnect_worker, close_profile_and_wait
from .errors import ConflictError, NotFoundError, ValidationError
from .playwright_worker import PlaywrightWorker, WorkerExecutionError
from .relationship_diagnostics import RelationshipDiagnostics, RelationshipDiagnosticStore
from .service import CoreService, isoformat


class CombinedCheckError(Exception):
    """Keep committed stage results when another stage fails."""

    def __init__(self, failures: dict[str, str], counts: tuple[int, int, int, int]) -> None:
        self.failures = failures
        self.counts = counts
        self.partial = len(failures) == 1
        labels = {"following": "关注检查", "dm": "私信检查"}
        super().__init__("；".join(f"{labels[kind]}失败：{message}" for kind, message in failures.items()))


@dataclass
class _MonitorControl:
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    started: bool = False
    cancel_requested: bool = False
    stop_requested: bool = False
    active: set[str] = field(default_factory=set)
    finished: set[str] = field(default_factory=set)


class FollowMonitorManager:
    """Combined checks with separately committed following and DM results."""

    def __init__(self, service: CoreService, bitbrowser: Any) -> None:
        self.service = service
        self.bitbrowser = bitbrowser
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._owner_runs: dict[tuple[str, str], str] = {}
        self._controls: dict[str, _MonitorControl] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self.diagnostics = RelationshipDiagnosticStore(service.database.path.parent / "follow-monitor-diagnostics")

    def recover_interrupted(self) -> None:
        with self.service.database.write() as connection:
            connection.execute("UPDATE follow_monitor_runs SET status='interrupted', finished_at=? WHERE status IN ('running','paused','cancelling')", (isoformat(),))

    def active_run_ids(self) -> set[str]:
        # Inventory requests run in a thread while the loop registers/removes
        # tasks. Snapshot the registry before consulting individual task states.
        return {run_id for run_id, task in tuple(self._tasks.items()) if not task.done()}

    async def start(self, owner_user_id: str, profile_ids: list[str], *, concurrency: int = 2, check_kind: str = "combined") -> dict[str, Any]:
        if self._closing:
            raise ConflictError("检查服务正在关闭，不能启动新的检查")
        if check_kind not in {"combined", "following", "dm"}:
            raise ValidationError("无效的检查类型")
        ids = list(dict.fromkeys(str(value).strip() for value in profile_ids if str(value).strip()))
        if not ids:
            raise ValidationError("请至少选择一个 Instagram 窗口")
        if type(concurrency) is not int or concurrency < 1:
            raise ValidationError("同时检查窗口数必须为正整数")
        key = (owner_user_id, check_kind)
        async with self._lock:
            if self._closing:
                raise ConflictError("检查服务正在关闭，不能启动新的检查")
            for (owner, kind), active_id in self._owner_runs.items():
                active = self._tasks.get(active_id)
                if owner == owner_user_id and (kind == check_kind or "combined" in {kind, check_kind}) and active and not active.done():
                    raise ConflictError("检查正在运行，请等待本轮完成")
            run_id = str(uuid.uuid4())
            with self.service.database.write() as connection:
                tables = ("follow_monitor_latest_dm",) if check_kind == "dm" else ("follow_monitor_latest_follow", "follow_monitor_latest_unfollow")
                if check_kind == "combined":
                    tables += ("follow_monitor_latest_dm",)
                for table in tables:
                    connection.execute(f"DELETE FROM {table} WHERE owner_user_id=?", (owner_user_id,))
                connection.execute(
                    """INSERT INTO follow_monitor_runs(id, owner_user_id, profile_ids_json, check_kind, status, started_at)
                    VALUES(?, ?, ?, ?, 'running', ?)""",
                    (run_id, owner_user_id, json.dumps(ids, ensure_ascii=False), check_kind, isoformat()),
                )
            control = _MonitorControl()
            control.gate.set()
            self._controls[run_id] = control
            task = asyncio.create_task(self._run(owner_user_id, run_id, ids, concurrency, check_kind))
            self._tasks[run_id] = task
            self._owner_runs[key] = run_id
            task.add_done_callback(lambda _task: self._tasks.pop(run_id, None))
            return {"run_id": run_id, "check_kind": check_kind, "status": "running", "total": len(ids)}

    async def control(self, owner_user_id: str, run_id: str, action: str) -> dict[str, Any]:
        if action not in {"pause", "resume", "cancel"}:
            raise ValidationError("无效的检查控制操作")
        async with self._lock:
            if self._closing:
                raise ConflictError("检查服务正在关闭，请等待窗口释放")
            with self.service.database.read() as connection:
                row = connection.execute("SELECT status FROM follow_monitor_runs WHERE id=? AND owner_user_id=?", (run_id, owner_user_id)).fetchone()
            if row is None:
                raise NotFoundError("检查任务不存在")
            state = self._controls.get(run_id)
            task = self._tasks.get(run_id)
            if state is None or task is None or task.done() or row["status"] not in {"running", "paused", "cancelling"}:
                raise ConflictError("检查任务已经结束")
            if state.cancel_requested:
                if action != "cancel":
                    raise ConflictError("任务正在取消并释放窗口，请稍候")
                return {"run_id": run_id, "status": "cancelling"}
            status = {"pause": "paused", "resume": "running", "cancel": "cancelling"}[action]
            with self.service.database.write() as connection:
                connection.execute("UPDATE follow_monitor_runs SET status=? WHERE id=?", (status, run_id))
            if action == "pause":
                state.gate.clear()
            elif action == "resume":
                state.gate.set()
            else:
                state.cancel_requested = True
                state.gate.set()
                # If _run has not entered yet, its first checkpoint observes the
                # flag. Cancelling an unstarted coroutine would skip its cleanup.
                if state.started:
                    task.cancel()
            return {"run_id": run_id, "status": status}

    async def _checkpoint(self, run_id: str) -> None:
        if self._closing:
            raise asyncio.CancelledError()
        state = self._controls.get(run_id)
        if state is not None:
            if state.cancel_requested or state.stop_requested:
                raise asyncio.CancelledError()
            await state.gate.wait()
            if state.cancel_requested or state.stop_requested:
                raise asyncio.CancelledError()

    async def _run(self, owner_user_id: str, run_id: str, profile_ids: list[str], concurrency: int, check_kind: str = "following") -> None:
        semaphore = asyncio.Semaphore(concurrency)
        control = self._controls.get(run_id)
        if control:
            control.started = True

        async def one(profile_id: str) -> None:
            async with semaphore:
                try:
                    await self._checkpoint(run_id)
                    if control:
                        control.active.add(profile_id)
                    added, unfollowed, dm_count, repeated = await self._scan_profile(owner_user_id, run_id, profile_id, check_kind)
                except asyncio.CancelledError:
                    raise
                except CombinedCheckError as exc:
                    added, unfollowed, dm_count, repeated = exc.counts
                    self._record_result(run_id, added=added, unfollowed=unfollowed, dm_count=dm_count, repeated=repeated, failed=True,
                        error={"profile_id": profile_id, "message": str(exc)[:500], "partial": exc.partial, "stages": exc.failures})
                except Exception as exc:
                    self._mark_profile_failed(owner_user_id, profile_id, str(exc), check_kind)
                    self._record_result(run_id, failed=True, error={"profile_id": profile_id, "message": str(exc)[:500]})
                else:
                    self._record_result(run_id, added=added, unfollowed=unfollowed, dm_count=dm_count, repeated=repeated)
                finally:
                    if control:
                        control.active.discard(profile_id)
                        control.finished.add(profile_id)
        children: list[asyncio.Task[None]] = []
        try:
            await self._checkpoint(run_id)
            children = [asyncio.create_task(one(profile_id)) for profile_id in profile_ids]
            await asyncio.gather(*children)
            with self.service.database.write() as connection:
                row = connection.execute("SELECT failed,succeeded,error_json FROM follow_monitor_runs WHERE id=?", (run_id,)).fetchone()
                partial = any(error.get("partial") for error in json.loads(row["error_json"] or "[]"))
                status = "completed" if row["failed"] == 0 else "partial" if row["succeeded"] or partial else "failed"
                connection.execute("UPDATE follow_monitor_runs SET status=?, finished_at=? WHERE id=?", (status, isoformat(), run_id))
        except asyncio.CancelledError:
            async def settle_cancelled_run() -> None:
                for child in children:
                    if not child.done() and not child.cancelling():
                        child.cancel()
                await asyncio.gather(*children, return_exceptions=True)
                with self.service.database.write() as connection:
                    # A stage may already be saved while the next stage is cancelled.
                    # Preserve those results and make the run totals match them.
                    counts = connection.execute("SELECT COALESCE(SUM(added_count),0),COALESCE(SUM(unfollow_count),0),COALESCE(SUM(repeat_count),0) FROM follow_monitor_rounds WHERE batch_id=?", (run_id,)).fetchone()
                    dms = connection.execute("SELECT COUNT(*) FROM follow_monitor_latest_dm WHERE batch_id=?", (run_id,)).fetchone()[0]
                    connection.execute("UPDATE follow_monitor_runs SET status=?, added_count=?, unfollow_count=?, repeat_count=?, dm_count=?, finished_at=? WHERE id=?", ("cancelled" if control and control.cancel_requested else "stopped", *counts, dms, isoformat(), run_id))
            await finish_owned(settle_cancelled_run())
            raise
        finally:
            key = (owner_user_id, check_kind)
            if self._owner_runs.get(key) == run_id:
                self._owner_runs.pop(key, None)
            self._controls.pop(run_id, None)

    def _record_result(self, run_id: str, *, added: int = 0, unfollowed: int = 0, dm_count: int = 0, repeated: int = 0, failed: bool = False, error: dict[str, Any] | None = None) -> None:
        with self.service.database.write() as connection:
            row = connection.execute("SELECT error_json FROM follow_monitor_runs WHERE id=?", (run_id,)).fetchone()
            errors = json.loads(row["error_json"] or "[]") if row else []
            if error:
                errors.append(error)
            connection.execute("""UPDATE follow_monitor_runs SET processed=processed+1,
                succeeded=succeeded+?, failed=failed+?, added_count=added_count+?,
                unfollow_count=unfollow_count+?, dm_count=dm_count+?, repeat_count=repeat_count+?, error_json=? WHERE id=?""",
                (0 if failed else 1, int(failed), added, unfollowed, dm_count, repeated, json.dumps(errors, ensure_ascii=False), run_id))

    def _mark_profile_failed(self, owner_user_id: str, profile_id: str, message: str, check_kind: str = "following") -> None:
        if check_kind == "combined":
            for kind in ("following", "dm"):
                self._mark_profile_failed(owner_user_id, profile_id, message, kind)
            return
        table = "follow_monitor_dm_accounts" if check_kind == "dm" else "follow_monitor_accounts"
        with self.service.database.write() as connection:
            # Keep the last saved observation and counters on actual execution errors.
            connection.execute(f"UPDATE {table} SET last_status='failed', last_error=? WHERE owner_user_id=? AND profile_id=?", (message[:500], owner_user_id, profile_id))

    async def _scan_profile(self, owner_user_id: str, run_id: str, profile_id: str, check_kind: str = "following") -> tuple[int, int, int, int]:
        # Construction can fail before any browser work exists. Do not leave a
        # durable lease behind when there is no worker to perform its cleanup.
        worker = PlaywrightWorker(self.bitbrowser)
        lease = await self.service.acquire_browser_lease_async(owner_user_id, profile_id, operation_type="monitor", entity_id=run_id, ttl_seconds=600)
        parent_task = asyncio.current_task()
        lease_error: Exception | None = None
        async def checkpoint() -> None:
            if lease_error is not None:
                raise ConflictError("检查窗口锁定已失效，本次读取已停止") from lease_error
            await self._checkpoint(run_id)
            if lease_error is not None:
                raise ConflictError("检查窗口锁定已失效，本次读取已停止") from lease_error
        worker.monitor_checkpoint = checkpoint
        renew_stop = asyncio.Event()
        async def renew() -> None:
            nonlocal lease_error
            try:
                while not renew_stop.is_set():
                    try:
                        await asyncio.wait_for(renew_stop.wait(), timeout=60)
                    except asyncio.TimeoutError:
                        for attempt in range(4):
                            try:
                                # Keep ownership until an in-flight SQLite thread
                                # settles, including cancellation during shutdown.
                                await finish_owned(asyncio.to_thread(self.service.renew_browser_lease, profile_id, lease, ttl_seconds=600))
                                break
                            except sqlite3.OperationalError:
                                if attempt == 3:
                                    raise
                                await asyncio.sleep(0.75 * (attempt + 1))
            except Exception as exc:
                # A dead heartbeat must stop its scanner, including a blocked
                # Playwright read; never continue on an unowned window.
                lease_error = exc
                if parent_task is not None and not parent_task.done():
                    parent_task.cancel()
        renew_task = asyncio.create_task(renew())
        close_on_success = False
        try:
            await checkpoint()
            await worker.connect(profile_id, open_if_needed=True)
            await checkpoint()
            instagram_user_id, username = await self._read_identity(worker)
            added = removed = repeated = dm_count = 0
            failures: dict[str, str] = {}
            stages = ("following", "dm") if check_kind == "combined" else (check_kind,)
            for stage in stages:
                try:
                    await checkpoint()
                    if stage == "following":
                        added, removed, repeated = await self._scan_following_stage(worker, owner_user_id, run_id, profile_id, instagram_user_id, username)
                    else:
                        dms = await self._read_pending_dms(worker)
                        await checkpoint()
                        dm_count = self._commit_dm_scan(owner_user_id, run_id, profile_id, instagram_user_id, username, dms)
                except Exception as exc:
                    if check_kind != "combined":
                        raise
                    failures[stage] = str(exc)[:240]
                    self._mark_profile_failed(owner_user_id, profile_id, str(exc), stage)
            counts = (added, removed, dm_count, repeated)
            if failures:
                raise CombinedCheckError(failures, counts)
            close_on_success = True
            return counts
        except asyncio.CancelledError:
            control = self._controls.get(run_id)
            if lease_error is not None and not (control and (control.cancel_requested or control.stop_requested)):
                raise ConflictError("检查窗口锁定已失效，本次读取已停止；已保留上次记录") from lease_error
            raise
        finally:
            async def cleanup():
                renew_stop.set()
                renew_task.cancel()
                await asyncio.gather(renew_task, return_exceptions=True)
                try:
                    await disconnect_worker(worker)
                finally:
                    try:
                        if close_on_success:
                            await close_profile_and_wait(self.service, self.bitbrowser, profile_id, lease)
                    except Exception:
                        pass
                    finally:
                        self.service.release_browser_lease(profile_id, lease)
            await finish_owned(cleanup())

    async def _scan_following_stage(self, worker: PlaywrightWorker, owner_user_id: str, run_id: str, profile_id: str, instagram_user_id: str, username: str) -> tuple[int, int, int]:
        diagnostic = RelationshipDiagnostics(lambda: worker.page)
        diagnostic.account_ref = diagnostic.fingerprint(instagram_user_id)
        diagnostic.username_ref = diagnostic.fingerprint(username)
        diagnostic.start(getattr(worker, "_context", None) or worker.page)
        try:
            following, metrics = await self._read_following_round(worker, username, diagnostic=diagnostic)
            # Positive observations are useful at every coverage level. Missing
            # rows are not unfollow evidence: only a confirmed complete snapshot
            # may remove members from the durable comparison baseline.
            diagnostic.outcome = "completed" if metrics.get("snapshot_complete") else "completed_partial"
        except BaseException as exc:
            diagnostic.outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            diagnostic.failure_type = type(exc).__name__
            raise
        finally:
            # Detach following diagnostics before opening the private inbox.
            await diagnostic.stop()
            await asyncio.to_thread(self.diagnostics.save, owner_user_id, profile_id, diagnostic.report())
        checkpoint = getattr(worker, "monitor_checkpoint", None)
        if checkpoint is not None:
            await checkpoint()
        else:
            await self._checkpoint(run_id)
        return self._commit_following_scan(owner_user_id, run_id, profile_id, instagram_user_id, username, following, **metrics)

    async def _read_following_round(self, worker: PlaywrightWorker, username: str, *, diagnostic: RelationshipDiagnostics | None = None) -> tuple[set[str], dict[str, Any]]:
        recoverable = {'instagram_following_list_not_rendered', 'instagram_following_list_incomplete', 'instagram_profile_not_ready'}

        async def read(attempt: int) -> tuple[set[str], int | None, WorkerExecutionError | None]:
            if diagnostic is not None:
                diagnostic.attempt = attempt
            error = None
            try:
                outcome = await worker.collect_following(username, limit=None, monitor_observation=True)
                raw_members, observed_total = outcome.usernames, outcome.source_total
            except WorkerExecutionError as exc:
                if exc.code not in recoverable:
                    raise
                error = exc
                raw_members = getattr(worker, 'last_relation_partial_usernames', ())
                observed_total = getattr(worker, 'last_relation_source_total', None)
            members = {str(value).strip().lower() for value in raw_members if str(value).strip()} if isinstance(raw_members, (list, tuple, set)) else set()
            members.discard(username.strip().lower())
            total = observed_total if type(observed_total) is int and observed_total >= 0 else None
            if diagnostic is not None:
                diagnostic.record_attempt(total, members, scroll=getattr(worker, 'last_relation_scroll', None))
            return members, total, error

        members, total, last_error = await read(1)
        metrics: dict[str, Any] = {"source_total": total, "first_read_count": len(members), "second_read_count": None, "second_homepage_count": None}
        final_members = members.copy()
        final_total = total
        # One supplemental attempt for a short read, without a percentage gate.
        # Login/network/challenge errors still propagate instead of looking saved.
        if last_error is not None or (total is not None and len(members) < total):
            replace_page = getattr(worker, '_replace_stuck_page_once', None)
            finish_page = getattr(worker, '_finish_page_recovery', None)
            replacement = callable(replace_page) and callable(finish_page)
            checkpoint = getattr(worker, 'monitor_checkpoint', None)
            if callable(checkpoint):
                await checkpoint()
            progressed = False
            try:
                if replacement:
                    await replace_page(username)
                second, second_total, last_error = await read(2)
                metrics.update(second_read_count=len(second), second_homepage_count=second_total)
                if metrics['source_total'] is None:
                    metrics['source_total'] = second_total
                progressed = bool(second - members)
                members |= second
                final_members, final_total = second, second_total
                if last_error is None and second_total is not None and len(second) == second_total:
                    progressed = True
            finally:
                if replacement:
                    await finish_owned(finish_page(progressed=progressed))
        totals = [value for value in (total, final_total) if value is not None]
        highest_total = max(totals) if totals else None
        # A union of two individually short lists is not a complete final frame;
        # users may follow/unfollow while a page is open. Unknown counts and failed
        # scans likewise cannot authorize negative evidence.
        metrics['snapshot_complete'] = bool(
            last_error is None and final_total is not None
            and len(final_members) == len(members) == highest_total == final_total
        )
        if not members and not metrics['snapshot_complete']:
            raise last_error or WorkerExecutionError(
                "本轮未读取到已关注账号；保留上轮记录，未确认取关",
                reason='instagram_following_list_not_rendered', pause_required=False,
            )
        return members, metrics

    async def _read_identity(self, worker: PlaywrightWorker) -> tuple[str, str]:
        page = worker.page
        await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(2_000)
        cookies = await worker._context.cookies("https://www.instagram.com/")
        instagram_user_id = next((str(item.get("value") or "") for item in cookies if item.get("name") == "ds_user_id"), "")
        if not instagram_user_id:
            raise ValidationError("窗口未登录 Instagram，无法识别账号")
        from .instagram_home import OWN_PROFILE, prepare_instagram_home
        await prepare_instagram_home(page, getattr(worker, "monitor_checkpoint", None))
        username = await page.evaluate(OWN_PROFILE)
        username = str(username or "").strip().lower()
        if not username:
            raise ValidationError("已登录但无法读取当前 Instagram 用户名，请打开个人主页后重试")
        return instagram_user_id, username

    async def _read_pending_dms(self, worker: PlaywrightWorker) -> list[dict[str, str]]:
        page = worker.page
        await page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(5_000)
        checkpoint = getattr(worker, "monitor_checkpoint", None)
        if checkpoint is not None:
            await checkpoint()
        rows = await page.evaluate(
            r"""() => {
              const unreadWords = /unread|new message|未读|新消息|未讀|não lida|non lu|ungelesen|未読|no leído|non letto/i;
              const mineWords = /^(you|你|您|sent by you|tú|vous|du|você)\s*[:：]/i;
              const visible = el => {
                const box = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return box.width > 120 && box.height >= 36 && box.height <= 150 &&
                  box.right > 0 && box.bottom > 0 && box.top < innerHeight &&
                  style.display !== 'none' && style.visibility !== 'hidden';
              };
              const linesOf = el => (el.innerText || '').split('\n').map(v => v.trim()).filter(Boolean);
              const leafText = el => Array.from(el.querySelectorAll('span,div')).filter(node =>
                Array.from(node.childNodes).some(child => child.nodeType === Node.TEXT_NODE && (child.textContent || '').trim())
              );
              const isBold = el => {
                const weight = getComputedStyle(el).fontWeight;
                return weight === 'bold' || Number.parseInt(weight, 10) >= 600;
              };
              const candidates = new Set(Array.from(document.querySelectorAll('a[href^="/direct/t/"], [role="button"], [tabindex="0"]')));
              const output = [];
              const seen = new Set();
              for (const row of candidates) {
                if (!visible(row) || !row.querySelector('img')) continue;
                const box = row.getBoundingClientRect();
                if (box.left > innerWidth * 0.55) continue;
                const text = linesOf(row);
                if (text.length < 2 || text.length > 8) continue;
                const hrefNode = row.matches('a[href]') ? row : row.querySelector('a[href^="/direct/t/"]');
                const href = hrefNode?.getAttribute('href') || '';
                const aria = `${row.getAttribute('aria-label') || ''} ${row.textContent || ''}`;
                const boldLines = leafText(row).filter(isBold).map(node => (node.textContent || '').trim()).filter(Boolean);
                const previewBold = boldLines.some(value => value !== text[0]);
                const labelledUnread = unreadWords.test(aria);
                const smallUnreadDot = Array.from(row.querySelectorAll('div,span')).some(node => {
                  const dot = node.getBoundingClientRect();
                  if (dot.width < 5 || dot.width > 18 || dot.height < 5 || dot.height > 18) return false;
                  const style = getComputedStyle(node);
                  const radius = Number.parseFloat(style.borderRadius || '0');
                  const channels = (style.backgroundColor.match(/[\d.]+/g) || []).map(Number);
                  const opaque = channels.length >= 3 && (channels.length < 4 || channels[3] >= 0.5);
                  const colored = opaque && Math.max(...channels.slice(0, 3)) - Math.min(...channels.slice(0, 3)) >= 30;
                  return !((node.textContent || '').trim()) && colored && radius >= Math.min(dot.width, dot.height) * 0.35;
                });
                const mine = text.slice(1).some(value => mineWords.test(value));
                if ((!previewBold && !labelledUnread && !smallUnreadDot) || mine) continue;
                const sender = text[0] || '';
                const key = href || sender.toLocaleLowerCase();
                if (!key || seen.has(key)) continue;
                seen.add(key);
                output.push({
                  thread_id: href || `visible:${sender}`,
                  sender,
                  preview: text.slice(1).join(' · ').slice(0, 300),
                  unread: true,
                  mine: false,
                });
                if (output.length >= 100) break;
              }
              return output;
            }"""
        )
        if "/direct/inbox" not in str(page.url).casefold():
            raise ValidationError("私信页面未正确打开，本窗口本轮不提交")
        return [dict(item) for item in rows if isinstance(item, dict)]

    def _commit_following_scan(self, owner_user_id: str, run_id: str, profile_id: str, instagram_user_id: str, username: str, following: set[str], *, source_total: int | None = None, first_read_count: int | None = None, second_read_count: int | None = None, second_homepage_count: int | None = None, snapshot_complete: bool | None = None) -> tuple[int, int, int]:
        now = isoformat()
        today = datetime.now(timezone.utc).date().isoformat()
        following = {str(value).strip().lower() for value in following if str(value).strip()}
        following.discard(username.strip().lower())
        if snapshot_complete is None:
            totals = [value for value in (source_total, second_homepage_count) if type(value) is int and value >= 0]
            snapshot_complete = bool(totals and len(following) == max(totals))
        observation_note = None if snapshot_complete else "已保存本轮实读账号；名单未确认完整，未判定取关"
        with self.service.database.write() as connection:
            saved = connection.execute("SELECT added_count, unfollow_count, repeat_count FROM follow_monitor_rounds WHERE owner_user_id=? AND batch_id=? AND profile_id=?", (owner_user_id, run_id, profile_id)).fetchone()
            if saved:
                return int(saved[0]), int(saved[1]), int(saved[2])
            account = connection.execute("SELECT * FROM follow_monitor_accounts WHERE owner_user_id=? AND profile_id=?", (owner_user_id, profile_id)).fetchone()
            same_identity = bool(account and account["instagram_user_id"] == instagram_user_id)
            old_generation = int(account["generation"] or 0) if same_identity else 0
            old_members = {row[0] for row in connection.execute("SELECT username FROM follow_monitor_members WHERE owner_user_id=? AND profile_id=? AND generation=?", (owner_user_id, profile_id, old_generation))} if old_generation else set()
            seen = {row[0] for row in connection.execute("SELECT username FROM follow_monitor_seen WHERE owner_user_id=? AND profile_id=? AND instagram_user_id=?", (owner_user_id, profile_id, instagram_user_id))}
            additions = following - old_members if old_generation else set()
            removals = old_members - following if old_generation and snapshot_complete else set()
            baseline_members = following if snapshot_complete else old_members | following
            repeats = additions & seen
            generation = old_generation + 1
            previous_homepage = account["homepage_count"] if old_generation else None
            previous_actual = int(account["following_count"]) if old_generation else None
            prior_added = int(account["total_added_count"] or 0) if same_identity else 0
            prior_removed = int(account["total_unfollow_count"] or 0) if same_identity else 0
            prior_repeat = int(account["total_repeat_count"] or 0) if same_identity else 0
            connection.execute("""INSERT INTO follow_monitor_accounts(
                owner_user_id, profile_id, instagram_user_id, username, generation, previous_generation,
                previous_following_count, following_count, homepage_count, previous_homepage_count,
                total_added_count, last_added_count, total_unfollow_count, last_unfollow_count,
                total_repeat_count, last_repeat_count, baseline_verified, last_status, last_error, checked_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'completed',?,?)
                ON CONFLICT(owner_user_id, profile_id) DO UPDATE SET
                instagram_user_id=excluded.instagram_user_id, username=excluded.username,
                generation=excluded.generation, previous_generation=excluded.previous_generation,
                previous_following_count=excluded.previous_following_count, following_count=excluded.following_count,
                homepage_count=excluded.homepage_count, previous_homepage_count=excluded.previous_homepage_count,
                total_added_count=excluded.total_added_count, last_added_count=excluded.last_added_count,
                total_unfollow_count=excluded.total_unfollow_count, last_unfollow_count=excluded.last_unfollow_count,
                total_repeat_count=excluded.total_repeat_count, last_repeat_count=excluded.last_repeat_count,
                baseline_verified=excluded.baseline_verified, last_status='completed', last_error=excluded.last_error, checked_at=excluded.checked_at""",
                (owner_user_id, profile_id, instagram_user_id, username, generation, old_generation or None,
                 previous_actual or 0, len(following), source_total, previous_homepage,
                 prior_added+len(additions), len(additions), prior_removed+len(removals), len(removals), prior_repeat+len(repeats), len(repeats), int(snapshot_complete), observation_note, now))
            if not same_identity:
                connection.execute("DELETE FROM follow_monitor_members WHERE owner_user_id=? AND profile_id=?", (owner_user_id, profile_id))
            connection.executemany("INSERT INTO follow_monitor_members(owner_user_id,profile_id,generation,username) VALUES(?,?,?,?)", [(owner_user_id, profile_id, generation, member) for member in baseline_members])
            connection.execute("DELETE FROM follow_monitor_members WHERE owner_user_id=? AND profile_id=? AND generation NOT IN (?,?)", (owner_user_id, profile_id, generation, old_generation))
            connection.executemany("""INSERT INTO follow_monitor_seen VALUES(?,?,?,?,?,?)
                ON CONFLICT(owner_user_id,profile_id,instagram_user_id,username) DO UPDATE SET last_seen_at=excluded.last_seen_at""", [(owner_user_id, profile_id, instagram_user_id, member, now, now) for member in following])
            connection.executemany("INSERT INTO follow_monitor_latest_follow VALUES(?,?,?,?,?,?,?)", [(owner_user_id, run_id, profile_id, username, member, "repeat_following" if member in repeats else "new_following", now) for member in sorted(additions)])
            connection.executemany("INSERT INTO follow_monitor_latest_unfollow VALUES(?,?,?,?,?,?)", [(owner_user_id, run_id, profile_id, username, member, now) for member in sorted(removals)])
            connection.execute("""INSERT INTO follow_monitor_rounds(
                owner_user_id,batch_id,profile_id,owner_username,homepage_count,previous_homepage_count,
                actual_count,previous_actual_count,first_read_count,second_read_count,second_homepage_count,
                added_count,repeat_count,unfollow_count,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner_user_id,run_id,profile_id,username,source_total,previous_homepage,len(following),previous_actual,
                 len(following) if first_read_count is None else first_read_count,second_read_count,second_homepage_count,len(additions),len(repeats),len(removals),now))
            if additions:
                connection.execute("""INSERT INTO follow_monitor_daily_counts(owner_user_id,day,added_count,repeat_count)
                    VALUES(?,?,?,?) ON CONFLICT(owner_user_id,day) DO UPDATE SET
                    added_count=added_count+excluded.added_count, repeat_count=repeat_count+excluded.repeat_count""", (owner_user_id,today,len(additions),len(repeats)))
        return len(additions), len(removals), len(repeats)

    def _commit_dm_scan(self, owner_user_id: str, run_id: str, profile_id: str, instagram_user_id: str, username: str, dms: list[dict[str, Any]]) -> int:
        now = isoformat()
        rows = {str(item.get("thread_id") or ""): item for item in dms if item.get("thread_id")}
        with self.service.database.write() as connection:
            old = connection.execute("SELECT * FROM follow_monitor_dm_accounts WHERE owner_user_id=? AND profile_id=?", (owner_user_id,profile_id)).fetchone()
            if old and old["last_run_id"] == run_id:
                return int(old["last_dm_count"])
            prior = int(old["total_dm_count"]) if old and old["instagram_user_id"] == instagram_user_id else 0
            connection.execute("""INSERT INTO follow_monitor_dm_accounts(
                owner_user_id,profile_id,instagram_user_id,username,total_dm_count,last_dm_count,last_run_id,last_status,last_error,checked_at)
                VALUES(?,?,?,?,?,?,?,'completed',NULL,?) ON CONFLICT(owner_user_id,profile_id) DO UPDATE SET
                instagram_user_id=excluded.instagram_user_id,username=excluded.username,
                total_dm_count=excluded.total_dm_count,last_dm_count=excluded.last_dm_count,last_run_id=excluded.last_run_id,
                last_status='completed',last_error=NULL,checked_at=excluded.checked_at""", (owner_user_id,profile_id,instagram_user_id,username,prior+len(rows),len(rows),run_id,now))
            connection.executemany("""INSERT INTO follow_monitor_latest_dm(
                owner_user_id,batch_id,profile_id,owner_username,thread_id,sender,preview,message_at,discovered_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", [(owner_user_id,run_id,profile_id,username,key,str(item.get("sender") or ""),str(item.get("preview") or ""),item.get("message_at"),now) for key,item in rows.items()])
        return len(rows)

    def snapshot(self, owner_user_id: str) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date()
        week_start = today - timedelta(days=today.weekday())
        month_start = today.replace(day=1)
        def normalized_log(row: Any) -> dict[str, Any] | None:
            if row is None:
                return None
            result = dict(row)
            result["profile_ids"] = json.loads(result.pop("profile_ids_json") or "[]")
            result["errors"] = json.loads(result.pop("error_json") or "[]")
            control = self._controls.get(result["id"])
            result["active_profile_ids"] = sorted(control.active.copy()) if control else []
            result["finished_profile_ids"] = sorted(control.finished.copy()) if control else (result["profile_ids"] if result["status"] not in {"running", "paused", "cancelling"} else [])
            return result
        with self.service.database.read() as connection:
            latest = normalized_log(connection.execute("SELECT * FROM follow_monitor_runs WHERE owner_user_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1", (owner_user_id,)).fetchone())
            runs = {kind: normalized_log(connection.execute("SELECT * FROM follow_monitor_runs WHERE owner_user_id=? AND check_kind IN (?, 'combined') ORDER BY started_at DESC,rowid DESC LIMIT 1", (owner_user_id,kind)).fetchone()) for kind in ("combined","following","dm")}
            follows = [dict(row) for row in connection.execute("SELECT * FROM follow_monitor_latest_follow WHERE owner_user_id=? ORDER BY discovered_at DESC,username", (owner_user_id,))]
            unfollows = [dict(row) for row in connection.execute("SELECT * FROM follow_monitor_latest_unfollow WHERE owner_user_id=? ORDER BY discovered_at DESC,username", (owner_user_id,))]
            dms = [dict(row) for row in connection.execute("SELECT * FROM follow_monitor_latest_dm WHERE owner_user_id=? ORDER BY discovered_at DESC", (owner_user_id,))]
            accounts = [dict(row) for row in connection.execute("SELECT * FROM follow_monitor_accounts WHERE owner_user_id=? ORDER BY checked_at DESC", (owner_user_id,))]
            dm_accounts = [dict(row) for row in connection.execute("SELECT * FROM follow_monitor_dm_accounts WHERE owner_user_id=? ORDER BY checked_at DESC", (owner_user_id,))]
            counts = [dict(row) for row in connection.execute("SELECT * FROM follow_monitor_daily_counts WHERE owner_user_id=?", (owner_user_id,))]
            logs = [normalized_log(row) for row in connection.execute("SELECT * FROM follow_monitor_runs WHERE owner_user_id=? ORDER BY started_at DESC,rowid DESC LIMIT 100", (owner_user_id,))]
            # Only this following run's details are current; failed/new rounds
            # must never present a previous run's counts as current results.
            rounds = [dict(row) for row in connection.execute("SELECT * FROM follow_monitor_rounds WHERE owner_user_id=? AND batch_id=? ORDER BY profile_id", (owner_user_id,runs['following']['id'] if runs['following'] else ''))]
        return {
            "run": latest, "runs": runs, "latest_follow": follows, "latest_unfollow": unfollows,
            "latest_dm": dms, "accounts": accounts, "dm_accounts": dm_accounts, "rounds": rounds,
            "counts": {
                "total": sum(int(row['added_count']) for row in counts),
                "month": sum(int(row['added_count']) for row in counts if row['day'] >= month_start.isoformat()),
                "week": sum(int(row['added_count']) for row in counts if row['day'] >= week_start.isoformat()),
                "today": sum(int(row['added_count']) for row in counts if row['day'] == today.isoformat()),
                "repeated": sum(int(row['repeat_count']) for row in counts),
            }, "logs": logs,
        }

    async def shutdown(self) -> None:
        self._closing = True
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown_owned_runs())
        # A cancelled shutdown caller must not abandon window cleanup or leave
        # persisted runs permanently running. Concurrent shutdowns share one drain.
        await finish_owned(self._shutdown_task)

    async def _shutdown_owned_runs(self) -> None:
        tasks = list(self._tasks.values())
        for run_id, task in list(self._tasks.items()):
            control = self._controls.get(run_id)
            if control is not None:
                control.stop_requested = True
                control.gate.set()
            # As with user cancellation, an unstarted coroutine must enter its
            # checkpoint/finally blocks to persist terminal state and clear owners.
            if (control is None or control.started) and not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
