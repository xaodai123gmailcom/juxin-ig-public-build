import type { ChildProcess } from "node:child_process";

export type CoreSupervisorOptions = {
  spawnChild: () => ChildProcess;
  healthCheck: (signal: AbortSignal) => Promise<boolean>;
  requestShutdown?: (signal: AbortSignal) => Promise<boolean>;
  log?: (message: string, error?: unknown) => void;
  restartDelaysMs?: readonly number[];
  readyTimeoutMs?: number;
  readyPollMs?: number;
  stableResetMs?: number;
  heartbeatIntervalMs?: number;
  healthTimeoutMs?: number;
  heartbeatFailureThreshold?: number;
  stopGraceMs?: number;
  stopForceMs?: number;
};

type ChildRecord = {
  child: ChildProcess;
  generation: number;
  expectedExit: boolean;
  finished: boolean;
  restartRequested: boolean;
  readySettled: boolean;
  readyPromise: Promise<void>;
  resolveReady: () => void;
  rejectReady: (error: Error) => void;
  exitPromise: Promise<void>;
  resolveExit: () => void;
  heartbeatTimer: ReturnType<typeof setTimeout> | null;
  stableTimer: ReturnType<typeof setTimeout> | null;
  heartbeatFailures: number;
  healthySince: number | null;
};

const DEFAULT_RESTART_DELAYS_MS = [1000, 2000, 5000, 10000, 30000] as const;

function delay(milliseconds: number) {
  return new Promise<void>((resolve) => setTimeout(resolve, milliseconds));
}

/**
 * Owns exactly one Core child process. All intentional stop/start operations are
 * serialized, while every readiness/heartbeat callback is bound to the child
 * generation that created it so a stale response cannot affect a replacement.
 */
export class CoreSupervisor {
  private readonly options: Required<Omit<CoreSupervisorOptions, "log" | "requestShutdown">> & Pick<CoreSupervisorOptions, "log" | "requestShutdown">;
  private current: ChildRecord | null = null;
  private generation = 0;
  private restartAttempt = 0;
  private restartTimer: ReturnType<typeof setTimeout> | null = null;
  private quitting = false;
  private operationTail: Promise<void> = Promise.resolve();
  private shutdownPromise: Promise<void> | null = null;
  private readonly probeControllers = new Set<AbortController>();

  constructor(options: CoreSupervisorOptions) {
    this.options = {
      ...options,
      restartDelaysMs: options.restartDelaysMs ?? DEFAULT_RESTART_DELAYS_MS,
      readyTimeoutMs: options.readyTimeoutMs ?? 180_000,
      readyPollMs: options.readyPollMs ?? 250,
      stableResetMs: options.stableResetMs ?? 30_000,
      heartbeatIntervalMs: options.heartbeatIntervalMs ?? 10_000,
      healthTimeoutMs: options.healthTimeoutMs ?? 3_000,
      heartbeatFailureThreshold: options.heartbeatFailureThreshold ?? 3,
      stopGraceMs: options.stopGraceMs ?? 3_000,
      stopForceMs: options.stopForceMs ?? 5_000,
    };
    if (this.options.restartDelaysMs.length === 0) {
      throw new Error("Core restart delays must not be empty");
    }
  }

  start(): Promise<void> {
    return this.runExclusive(async () => {
      if (this.quitting) throw new Error("应用正在退出，不能启动本机采集服务");
      this.cancelScheduledRestart();
      const record = this.current ?? this.launchChild();
      await record.readyPromise;
    });
  }

  restartForConfiguration(): Promise<void> {
    return this.runExclusive(async () => {
      if (this.quitting) throw new Error("应用正在退出，不能重启本机采集服务");
      this.cancelScheduledRestart();
      this.restartAttempt = 0;
      const previous = this.current;
      if (previous) {
        try {
          await this.stopRecord(previous, "配置已更新");
        } catch (error) {
          this.restoreMonitoringAfterFailedStop(previous);
          throw error;
        }
      }
      if (this.quitting) throw new Error("应用正在退出，不能重启本机采集服务");
      const replacement = this.launchChild();
      await replacement.readyPromise;
    });
  }

  shutdown(): Promise<void> {
    if (this.shutdownPromise) return this.shutdownPromise;
    this.quitting = true;
    this.cancelScheduledRestart();
    this.abortHealthProbes();
    if (this.current) {
      this.current.expectedExit = true;
      this.rejectReady(this.current, new Error("应用正在退出"));
      this.clearRecordTimers(this.current);
    }
    this.shutdownPromise = this.runExclusive(async () => {
      const record = this.current;
      if (record) await this.stopRecord(record, "应用正在退出");
    });
    return this.shutdownPromise;
  }

  private runExclusive<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.operationTail.then(operation, operation);
    this.operationTail = result.then(() => undefined, () => undefined);
    return result;
  }

  private launchChild(): ChildRecord {
    if (this.current) throw new Error("本机采集服务仍在运行，拒绝启动第二个实例");
    let child: ChildProcess;
    try {
      child = this.options.spawnChild();
    } catch (error) {
      // Configuration/credential I/O can throw before spawn returns a child.
      // There is then no exit event to schedule the next recovery attempt.
      this.log("本机采集服务启动准备失败", error);
      this.scheduleRestart("启动准备失败");
      throw error;
    }
    let resolveReady!: () => void;
    let rejectReady!: (error: Error) => void;
    let resolveExit!: () => void;
    const readyPromise = new Promise<void>((resolve, reject) => {
      resolveReady = resolve;
      rejectReady = reject;
    });
    // A scheduled restart may not have a UI caller awaiting readiness.
    void readyPromise.catch(() => undefined);
    const exitPromise = new Promise<void>((resolve) => { resolveExit = resolve; });
    const record: ChildRecord = {
      child,
      generation: ++this.generation,
      expectedExit: false,
      finished: false,
      restartRequested: false,
      readySettled: false,
      readyPromise,
      resolveReady,
      rejectReady,
      exitPromise,
      resolveExit,
      heartbeatTimer: null,
      stableTimer: null,
      heartbeatFailures: 0,
      healthySince: null,
    };
    this.current = record;

    child.once("exit", (code, signal) => {
      this.finishRecord(record, code === 0 ? undefined : new Error(`Core 已退出（code=${String(code)}, signal=${String(signal)}）`));
    });
    child.once("error", (error) => {
      // spawn failures have no PID and do not reliably emit "exit". Errors from
      // kill()/send() on an already spawned process are not proof of termination.
      if (child.pid === undefined || child.pid === null) this.finishRecord(record, error);
      else this.log("本机采集服务进程错误", error);
    });

    void this.monitorStartup(record);
    return record;
  }

  private async monitorStartup(record: ChildRecord) {
    const deadline = Date.now() + this.options.readyTimeoutMs;
    while (this.isCurrent(record) && !this.quitting && Date.now() < deadline) {
      if (await this.probeHealth()) {
        if (!this.isCurrent(record) || this.quitting) return;
        this.resolveReady(record);
        this.markHealthy(record);
        this.scheduleHeartbeat(record);
        return;
      }
      if (!this.isCurrent(record) || this.quitting) return;
      await delay(Math.min(this.options.readyPollMs, Math.max(1, deadline - Date.now())));
    }
    if (!this.isCurrent(record) || this.quitting) return;
    const error = new Error(`本机采集服务在 ${this.options.readyTimeoutMs}ms 内未就绪`);
    this.rejectReady(record, error);
    this.requestRestart(record, error.message);
  }

  private scheduleHeartbeat(record: ChildRecord) {
    if (!this.isCurrent(record) || this.quitting || record.finished) return;
    if (record.heartbeatTimer) clearTimeout(record.heartbeatTimer);
    record.heartbeatTimer = setTimeout(() => {
      record.heartbeatTimer = null;
      void this.runHeartbeat(record);
    }, this.options.heartbeatIntervalMs);
  }

  private async runHeartbeat(record: ChildRecord) {
    if (!this.isCurrent(record) || this.quitting || record.finished) return;
    const healthy = await this.probeHealth();
    if (!this.isCurrent(record) || this.quitting || record.finished) return;
    if (healthy) {
      record.heartbeatFailures = 0;
      this.markHealthy(record);
      this.scheduleHeartbeat(record);
      return;
    }

    record.heartbeatFailures += 1;
    record.healthySince = null;
    if (record.stableTimer) clearTimeout(record.stableTimer);
    record.stableTimer = null;
    if (record.heartbeatFailures >= this.options.heartbeatFailureThreshold) {
      this.requestRestart(record, `健康检查连续失败 ${record.heartbeatFailures} 次`);
      return;
    }
    this.scheduleHeartbeat(record);
  }

  private markHealthy(record: ChildRecord) {
    if (record.healthySince === null) record.healthySince = Date.now();
    if (record.stableTimer || this.restartAttempt === 0) return;
    const elapsed = Date.now() - record.healthySince;
    const remaining = Math.max(0, this.options.stableResetMs - elapsed);
    record.stableTimer = setTimeout(() => {
      record.stableTimer = null;
      if (!this.isCurrent(record) || record.finished || record.healthySince === null) return;
      if (Date.now() - record.healthySince < this.options.stableResetMs) {
        this.markHealthy(record);
        return;
      }
      this.restartAttempt = 0;
    }, remaining);
  }

  private requestRestart(record: ChildRecord, reason: string) {
    if (!this.isCurrent(record) || this.quitting || record.restartRequested) return;
    record.restartRequested = true;
    record.expectedExit = true;
    this.clearRecordTimers(record);
    this.abortHealthProbes();
    void this.runExclusive(async () => {
      if (this.quitting || !this.isCurrent(record)) return;
      try {
        await this.stopRecord(record, reason);
      } catch (error) {
        // Never launch a replacement until termination is confirmed.
        this.log("本机采集服务无法停止，已阻止重复启动", error);
        this.restoreMonitoringAfterFailedStop(record);
        return;
      }
      if (!this.quitting) this.scheduleRestart(reason);
    });
  }

  private scheduleRestart(reason: string) {
    if (this.quitting || this.current || this.restartTimer) return;
    const delays = this.options.restartDelaysMs;
    const waitMs = delays[Math.min(this.restartAttempt, delays.length - 1)];
    this.restartAttempt += 1;
    this.log(`本机采集服务将在 ${waitMs}ms 后重启：${reason}`);
    this.restartTimer = setTimeout(() => {
      this.restartTimer = null;
      if (this.quitting || this.current) return;
      void this.runExclusive(async () => {
        if (this.quitting || this.current) return;
        try {
          const replacement = this.launchChild();
          await replacement.readyPromise;
        } catch {
          // launchChild/monitorStartup/finishRecord owns the next backoff.
        }
      });
    }, waitMs);
  }

  private async stopRecord(record: ChildRecord, reason: string) {
    record.expectedExit = true;
    this.clearRecordTimers(record);
    this.abortHealthProbes();
    this.rejectReady(record, new Error(reason));
    if (record.finished) return;

    if (this.options.requestShutdown) {
      const controller = new AbortController();
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        // A host hook that ignores AbortSignal cannot hold shutdown forever.
        const accepted = await Promise.race([
          this.options.requestShutdown(controller.signal),
          new Promise<false>((resolve) => { timer = setTimeout(() => { controller.abort(); resolve(false); }, 3000); }),
        ]);
        if (accepted && await this.waitForExit(record, this.options.stopGraceMs)) return;
      } catch (error) { this.log("本机服务未确认正常退出，保留异常恢复记录", error); }
      finally { if (timer) clearTimeout(timer); controller.abort(); }
      if (record.finished) return;
      this.log("本机服务正常退出等待结束，开始进程级收尾；下次启动将核验未完成任务");
    }
    try { record.child.kill(); } catch (error) { this.log("停止本机采集服务失败，准备强制停止", error); }
    if (await this.waitForExit(record, this.options.stopGraceMs)) return;

    try { record.child.kill("SIGKILL"); } catch (error) { this.log("强制停止本机采集服务失败", error); }
    if (await this.waitForExit(record, this.options.stopForceMs)) return;
    throw new Error("本机采集服务在强制停止后仍未退出；为避免双开，已取消启动新实例");
  }

  private async waitForExit(record: ChildRecord, timeoutMs: number) {
    if (record.finished) return true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const timedOut = new Promise<false>((resolve) => {
      timer = setTimeout(() => resolve(false), timeoutMs);
    });
    const exited = record.exitPromise.then(() => true as const);
    const result = await Promise.race([exited, timedOut]);
    if (timer) clearTimeout(timer);
    return result;
  }

  private finishRecord(record: ChildRecord, error?: Error) {
    if (record.finished) return;
    record.finished = true;
    this.clearRecordTimers(record);
    this.rejectReady(record, error ?? new Error("本机采集服务已退出"));
    record.resolveExit();
    if (this.current === record) this.current = null;
    if (error) this.log("本机采集服务进程异常", error);
    if (!record.expectedExit && !this.quitting) this.scheduleRestart(error?.message ?? "进程意外退出");
  }

  private resolveReady(record: ChildRecord) {
    if (record.readySettled) return;
    record.readySettled = true;
    record.resolveReady();
  }

  private rejectReady(record: ChildRecord, error: Error) {
    if (record.readySettled) return;
    record.readySettled = true;
    record.rejectReady(error);
  }

  private clearRecordTimers(record: ChildRecord) {
    if (record.heartbeatTimer) clearTimeout(record.heartbeatTimer);
    if (record.stableTimer) clearTimeout(record.stableTimer);
    record.heartbeatTimer = null;
    record.stableTimer = null;
  }

  private restoreMonitoringAfterFailedStop(record: ChildRecord) {
    if (!this.isCurrent(record) || this.quitting) return;
    record.expectedExit = false;
    record.restartRequested = false;
    record.heartbeatFailures = 0;
    record.healthySince = null;
    this.scheduleHeartbeat(record);
  }

  private cancelScheduledRestart() {
    if (this.restartTimer) clearTimeout(this.restartTimer);
    this.restartTimer = null;
  }

  private abortHealthProbes() {
    for (const controller of this.probeControllers) controller.abort();
    this.probeControllers.clear();
  }

  private async probeHealth() {
    const controller = new AbortController();
    this.probeControllers.add(controller);
    const timer = setTimeout(() => controller.abort(), this.options.healthTimeoutMs);
    try {
      return await this.options.healthCheck(controller.signal);
    } catch {
      return false;
    } finally {
      clearTimeout(timer);
      this.probeControllers.delete(controller);
    }
  }

  private isCurrent(record: ChildRecord) {
    return this.current === record && record.generation === this.generation && !record.finished;
  }

  private log(message: string, error?: unknown) {
    this.options.log?.(message, error);
  }
}
