/* Test-runner deadlines. Progress output never extends a deadline. */
function createIntegrationWatchdog({ onTimeout, log = console.log,
  now = () => performance.now(), timers = globalThis,
  overallMs = 15 * 60 * 1000, heartbeatMs = 15000 }) {
  const started = now();
  let active = null, finished = false, sequence = 0;
  const completed = [];
  const seconds = ms => (ms / 1000).toFixed(1);
  const status = () => ({ stage: active?.name || (completed.length ? 'completed' : 'startup'),
    stageElapsedMs: active ? Math.round(now() - active.started) : 0,
    stageLimitMs: active?.limit || 0, totalElapsedMs: Math.round(now() - started),
    overallLimitMs: overallMs, completed: completed.map(item => ({ ...item })) });
  const dispose = () => {
    finished = true;
    timers.clearTimeout(overall);
    timers.clearInterval(heartbeat);
    if (active) timers.clearTimeout(active.timer);
  };
  const fail = reason => {
    if (finished) return;
    const result = { ...status(), reason };
    dispose();
    log(`FAIL embedded test timeout: ${result.stage}; stage ${seconds(result.stageElapsedMs)}s/${seconds(result.stageLimitMs)}s; total ${seconds(result.totalElapsedMs)}s; reason=${reason}`);
    onTimeout(result);
  };
  const overall = timers.setTimeout(() => fail('overall deadline'), overallMs);
  const heartbeat = timers.setInterval(() => {
    if (finished || !active) return;
    const result = status();
    log(`WAIT ${active.name}: ${seconds(result.stageElapsedMs)}s / ${seconds(active.limit)}s; total ${seconds(result.totalElapsedMs)}s`);
  }, heartbeatMs);
  const completeStage = () => {
    if (!active) return;
    timers.clearTimeout(active.timer);
    const elapsedMs = Math.round(now() - active.started);
    completed.push({ stage: active.name, elapsedMs });
    log(`PASS stage ${active.name}: ${seconds(elapsedMs)}s`);
    active = null;
  };
  return {
    begin(name, limit = 120000) {
      if (finished) throw new Error('Embedded watchdog has already stopped');
      if (!name || !Number.isFinite(limit) || limit <= 0) throw new Error('Invalid embedded test stage');
      // Also check elapsed time synchronously: a blocked event loop must not
      // mark an overdue stage successful before its timer gets a turn.
      if (now() - started >= overallMs || (active && now() - active.started >= active.limit)) {
        fail(now() - started >= overallMs ? 'overall deadline' : 'stage deadline');
        throw new Error('Embedded test deadline exceeded');
      }
      completeStage();
      const generation = ++sequence;
      active = { name, started: now(), limit, timer: timers.setTimeout(() => {
        if (active?.generation === generation) fail('stage deadline');
      }, limit), generation };
      log(`CHECK stage ${name} (limit ${seconds(limit)}s)`);
    },
    finish() {
      if (finished) throw new Error('Cannot mark a stopped embedded test successful');
      if (now() - started >= overallMs || (active && now() - active.started >= active.limit)) {
        fail(now() - started >= overallMs ? 'overall deadline' : 'stage deadline');
        throw new Error('Embedded test deadline exceeded');
      }
      completeStage();
      const result = status();
      dispose();
      return result;
    },
    status, dispose,
  };
}
module.exports = { createIntegrationWatchdog };
