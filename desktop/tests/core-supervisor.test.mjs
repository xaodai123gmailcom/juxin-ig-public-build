import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { CoreSupervisor } from "../../dist-electron/core-supervisor.js";

class FakeChild extends EventEmitter {
  static nextPid = 1000;

  constructor({ exitOnKill = true, killDelayMs = 0 } = {}) {
    super();
    this.pid = FakeChild.nextPid++;
    this.exitCode = null;
    this.signalCode = null;
    this.exitOnKill = exitOnKill;
    this.killDelayMs = killDelayMs;
    this.killSignals = [];
    this.finished = false;
  }

  kill(signal = "SIGTERM") {
    this.killSignals.push(signal);
    if (this.exitOnKill && !this.finished) {
      setTimeout(() => this.finish(0, signal), this.killDelayMs);
    }
    return true;
  }

  crash(code = 1) {
    this.finish(code, null);
  }

  finish(code, signal) {
    if (this.finished) return;
    this.finished = true;
    this.exitCode = code;
    this.signalCode = signal;
    this.emit("exit", code, signal);
  }
}

async function waitFor(predicate, timeoutMs = 500) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("Timed out waiting for supervisor state");
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
}

function fastOptions(overrides = {}) {
  return {
    healthCheck: async () => true,
    restartDelaysMs: [1, 5],
    readyTimeoutMs: 100,
    readyPollMs: 1,
    stableResetMs: 20,
    heartbeatIntervalMs: 1000,
    healthTimeoutMs: 10,
    heartbeatFailureThreshold: 3,
    stopGraceMs: 5,
    stopForceMs: 5,
    ...overrides,
  };
}

test("concurrent start callers share one child-bound readiness probe", async () => {
  let spawnCount = 0;
  let probeCount = 0;
  let releaseHealth;
  const healthGate = new Promise((resolve) => { releaseHealth = resolve; });
  const supervisor = new CoreSupervisor(fastOptions({
    spawnChild: () => {
      spawnCount += 1;
      return new FakeChild();
    },
    healthCheck: async () => {
      probeCount += 1;
      await healthGate;
      return true;
    },
  }));

  const first = supervisor.start();
  const second = supervisor.start();
  await waitFor(() => probeCount === 1);
  releaseHealth();
  await Promise.all([first, second]);
  assert.equal(spawnCount, 1);
  assert.equal(probeCount, 1);
  await supervisor.shutdown();
});

test("spawn error followed by exit schedules exactly one replacement", async () => {
  const children = [];
  let restartSchedules = 0;
  const supervisor = new CoreSupervisor(fastOptions({
    spawnChild: () => {
      const child = new FakeChild();
      children.push(child);
      if (children.length === 1) {
        queueMicrotask(() => {
          child.pid = undefined;
          child.emit("error", new Error("spawn failed"));
          child.emit("exit", 1, null);
        });
      }
      return child;
    },
    log: (message) => {
      if (message.includes("后重启")) restartSchedules += 1;
    },
  }));

  await assert.rejects(supervisor.start(), /spawn failed/);
  await waitFor(() => children.length === 2);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(restartSchedules, 1);
  assert.equal(children.length, 2);
  await supervisor.shutdown();
});

test("configuration restart confirms exit and never overlaps Core children", async () => {
  const children = [];
  let live = 0;
  let maxLive = 0;
  const supervisor = new CoreSupervisor(fastOptions({
    spawnChild: () => {
      const child = new FakeChild({ killDelayMs: 3 });
      children.push(child);
      live += 1;
      maxLive = Math.max(maxLive, live);
      child.once("exit", () => { live -= 1; });
      return child;
    },
  }));

  await supervisor.start();
  await supervisor.restartForConfiguration();
  assert.equal(children.length, 2);
  assert.equal(maxLive, 1);
  await supervisor.shutdown();
});

test("configuration restart refuses to double-start when force kill is unconfirmed", async () => {
  let spawnCount = 0;
  const supervisor = new CoreSupervisor(fastOptions({
    spawnChild: () => {
      spawnCount += 1;
      return new FakeChild({ exitOnKill: false });
    },
  }));

  await supervisor.start();
  await assert.rejects(supervisor.restartForConfiguration(), /避免双开/);
  assert.equal(spawnCount, 1);
  await assert.rejects(supervisor.shutdown(), /避免双开/);
});

test("restart backoff resets only after a continuously healthy stable period", async () => {
  const children = [];
  const scheduledDelays = [];
  const supervisor = new CoreSupervisor(fastOptions({
    spawnChild: () => {
      const child = new FakeChild();
      children.push(child);
      return child;
    },
    log: (message) => {
      const match = message.match(/将在 (\d+)ms 后重启/);
      if (match) scheduledDelays.push(Number(match[1]));
    },
  }));

  await supervisor.start();
  children[0].crash();
  await waitFor(() => children.length === 2);
  children[1].crash();
  await waitFor(() => children.length === 3);
  await new Promise((resolve) => setTimeout(resolve, 30));
  children[2].crash();
  await waitFor(() => scheduledDelays.length === 3);
  assert.deepEqual(scheduledDelays, [1, 5, 1]);
  await supervisor.shutdown();
});

test("three failed heartbeats restart once, while shutdown never relaunches", async () => {
  const children = [];
  let probes = 0;
  const supervisor = new CoreSupervisor(fastOptions({
    spawnChild: () => {
      const child = new FakeChild();
      children.push(child);
      return child;
    },
    healthCheck: async () => {
      probes += 1;
      return probes === 1 || probes >= 5;
    },
    heartbeatIntervalMs: 2,
  }));

  await supervisor.start();
  await waitFor(() => children.length === 2);
  assert.equal(children.length, 2);
  await supervisor.shutdown();
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.equal(children.length, 2);
});

test("shutdown wins a race with configuration readiness and prevents relaunch", async () => {
  const children = [];
  let probeCount = 0;
  const supervisor = new CoreSupervisor(fastOptions({
    spawnChild: () => {
      const child = new FakeChild({ killDelayMs: 1 });
      children.push(child);
      return child;
    },
    healthCheck: async (signal) => {
      probeCount += 1;
      if (probeCount === 1) return true;
      return await new Promise((resolve) => {
        signal.addEventListener("abort", () => resolve(false), { once: true });
      });
    },
  }));

  await supervisor.start();
  const configuring = supervisor.restartForConfiguration();
  await waitFor(() => children.length === 2);
  const shuttingDown = supervisor.shutdown();
  await assert.rejects(configuring, /应用正在退出/);
  await shuttingDown;
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.equal(children.length, 2);
});

test('r18 graceful shutdown acknowledges before any process kill', async () => {
  const child = new FakeChild();
  let calls = 0;
  const supervisor = new CoreSupervisor({
    spawnChild: () => child, healthCheck: async () => true,
    requestShutdown: async () => { calls++; setTimeout(() => child.finish(0, null), 2); return true; },
    stopGraceMs: 50, stopForceMs: 10, readyPollMs: 1,
  });
  await supervisor.start();
  await supervisor.shutdown();
  assert.equal(calls, 1);
  assert.deepEqual(child.killSignals, []);
});

test('r18 unacknowledged shutdown uses bounded process fallback', async () => {
  const child = new FakeChild();
  const supervisor = new CoreSupervisor({
    spawnChild: () => child, healthCheck: async () => true,
    requestShutdown: async () => false, stopGraceMs: 20, stopForceMs: 10, readyPollMs: 1,
  });
  await supervisor.start();await supervisor.shutdown();
  assert.deepEqual(child.killSignals, ['SIGTERM']);
});

test("a synchronous startup failure is retried with bounded backoff", async () => {
  let attempts = 0;
  const children = [];
  const supervisor = new CoreSupervisor(fastOptions({
    spawnChild: () => {
      attempts += 1;
      if (attempts === 1) throw new Error("temporary secure-store sharing violation");
      const child = new FakeChild();
      children.push(child);
      return child;
    },
  }));
  try {
    await assert.rejects(supervisor.start(), /sharing violation/);
    await waitFor(() => children.length === 1);
    await supervisor.start();
    assert.equal(attempts, 2);
    assert.equal(children.length, 1);
  } finally {
    await supervisor.shutdown();
  }
});

test("a thrown spawn during crash recovery does not abandon automatic recovery", async () => {
  let attempts = 0;
  const children = [];
  const delays = [];
  const supervisor = new CoreSupervisor(fastOptions({
    spawnChild: () => {
      attempts += 1;
      if (attempts === 2) throw new Error("temporary startup configuration read failure");
      const child = new FakeChild();
      children.push(child);
      return child;
    },
    log: message => {
      const match = message.match(/将在 (\d+)ms 后重启/);
      if (match) delays.push(Number(match[1]));
    },
  }));
  try {
    await supervisor.start();
    children[0].crash();
    await waitFor(() => children.length === 2);
    await supervisor.start();
    assert.equal(attempts, 3);
    assert.deepEqual(delays, [1, 5]);
  } finally {
    await supervisor.shutdown();
  }
});
