/* Thin Node adapter for the sole Windows Job Object implementation. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');

function bounded(promise, ms, label) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} exceeded ${ms} ms`)), ms);
  })]).finally(() => clearTimeout(timer));
}

async function readSegment(file, position, maxBytes = 65536) {
  let handle;
  try {
    handle = await fs.promises.open(file, 'r');
    const stat = await handle.stat();
    const start = position === null ? Math.max(0, stat.size - maxBytes) : position;
    const buffer = Buffer.alloc(Math.min(maxBytes, Math.max(0, stat.size - start)));
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, start);
    return { data: buffer.subarray(0, bytesRead), next: start + bytesRead, size: stat.size };
  } catch (error) {
    if (error.code === 'ENOENT') return { data: Buffer.alloc(0), next: position || 0, size: 0 };
    throw error;
  } finally {
    if (handle) await handle.close();
  }
}

const outcomes=new Set(['completed','target-exited-nonzero','execution-timeout','cancelled','cancelled-before-launch','descendant-drain-timeout','supervision-error','log-size-limit']);
function receiptShape(receipt) {
  return receipt!==null && typeof receipt==='object' && !Array.isArray(receipt) && receipt.schemaVersion===1 &&
    typeof receipt.requestId==='string' && /^[0-9a-f]{32}$/.test(receipt.requestId) &&
    Number.isInteger(receipt.supervisorPid) && receipt.supervisorPid>0 &&
    (receipt.launchTargetPid===null || (Number.isInteger(receipt.launchTargetPid)&&receipt.launchTargetPid>0)) &&
    (receipt.targetExitCode===null || (Number.isInteger(receipt.targetExitCode)&&receipt.targetExitCode>=-(2**31)&&receipt.targetExitCode<2**31)) &&
    (receipt.targetExitCode===null ? receipt.targetExitCodeUnsigned===null : Number.isInteger(receipt.targetExitCodeUnsigned)&&receipt.targetExitCodeUnsigned===(receipt.targetExitCode>>>0)) &&
    outcomes.has(receipt.outcome) && typeof receipt.confirmedTreeEmpty==='boolean' &&
    Array.isArray(receipt.errors) && receipt.errors.every(error=>typeof error==='string') &&
    typeof receipt.requestedExecutable==='string' && receipt.requestedExecutable.length>0 &&
    (receipt.launchExecutable===null || typeof receipt.launchExecutable==='string') &&
    typeof receipt.budgetLabel==='string' && receipt.budgetLabel.length>0 &&
    Number.isFinite(receipt.executionLimitSeconds) && receipt.executionLimitSeconds>0 &&
    Number.isFinite(receipt.elapsedSeconds) && receipt.elapsedSeconds>=0;
}
function cleanupReceipt(receipt) {
  return Boolean(receiptShape(receipt) && receipt.confirmedTreeEmpty && receipt.errors.length===0 && receipt.launchTargetPid!==null && receipt.supervisorPid!==receipt.launchTargetPid && receipt.launchExecutable);
}
function terminalReceipt(receipt) {
  return cleanupReceipt(receipt) && receipt.targetExitCode!==null &&
    ((receipt.outcome==='completed' && receipt.targetExitCode===0) || (receipt.outcome==='target-exited-nonzero' && receipt.targetExitCode!==0));
}

function matchesRequest(receipt, request) {
  return receiptShape(receipt) && receipt.requestId===request.requestId &&
    path.win32.normalize(receipt.requestedExecutable).toLowerCase()===path.win32.normalize(request.executable).toLowerCase() &&
    receipt.budgetLabel===request.budgetLabel && receipt.executionLimitSeconds===request.timeoutSeconds &&
    receipt.supervisorPid!==receipt.launchTargetPid;
}

function startOwnedWindowsProcess(options, dependencies = {}) {
  const launch = dependencies.spawn || spawn;
  const read = dependencies.readSegment || readSegment;
  const readReceipt = dependencies.readReceipt || (async file => {
    const result = await readSegment(file, 0, 65537);
    if (!result.size || result.size > 65536) throw new Error('Owned receipt is missing or oversized');
    return JSON.parse(result.data.toString('utf8'));
  });
  const timeoutMs = options.timeoutMs;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new Error('Owned target needs a finite existing execution deadline');
  const drainMs = options.drainMs ?? 10000;
  const terminationMs = options.terminationMs ?? 10000;
  const ioMs = options.ioMs ?? 5000;
  const stopGraceMs = options.stopGraceMs ?? 5000;
  const outerGraceMs = options.outerGraceMs ?? 30000;
  const makeDirectory = dependencies.makeDirectory || (directory => {
    fs.mkdirSync(directory, { recursive: true });
    return fs.mkdtempSync(path.join(directory, 'owned-probe-'));
  });
  const directory = makeDirectory(options.logDirectory);
  const logPath = path.join(directory, 'target.log');
  const receiptPath = path.join(directory, 'receipt.json');
  const request = {
    executable: options.executable, arguments: options.args, workingDirectory: options.cwd,
    environment: options.env || {}, stdoutPath: logPath, stderrPath: logPath,
    timeoutSeconds: timeoutMs / 1000, budgetLabel: options.label,
    drainSeconds: drainMs / 1000, terminationSeconds: terminationMs / 1000,
    controlInput: true, receiptPath, requestId: crypto.randomBytes(16).toString('hex'),
  };
  const encoded = Buffer.from(JSON.stringify(request), 'utf8').toString('base64');
  if (encoded.length >= 28000) throw new Error('Owned request exceeds the Windows command-line budget');
  // stdout/stderr belong only to the supervisor; the target inherits explicit
  // file handles and NUL stdin, never these control-plane handles.
  const child = launch(options.python, ['-I', '-X', 'utf8', path.resolve(__dirname, 'owned_process.py'), '--request-base64', encoded], {
    cwd: options.cwd, env: process.env, stdio: ['pipe', 'ignore', 'inherit'], windowsHide: true,
  });
  let receipt = null, exitObserved = false, polling = false, finished = false, position = 0, output = '', ioError = null;
  child.stdin?.on('error', error => { ioError = ioError || error; });
  const exit = new Promise((resolve, reject) => {
    child.once('error', reject);
    // 'close' waits for inherited pipe EOF. 'exit' observes the process itself.
    child.once('exit', (code, signal) => { exitObserved = true; resolve({ code, signal }); });
  });
  // Attach immediately; spawn errors may happen before the caller awaits done.
  exit.catch(() => {});
  async function poll() {
    if (polling || finished || ioError) return;
    polling = true;
    try {
      const chunk = await bounded(read(logPath, position), ioMs, 'Owned log read');
      position = chunk.next;
      if (chunk.data.length) {
        output = (output + chunk.data.toString('utf8')).slice(-64000);
        options.onData?.(chunk.data);
      }
    } catch (error) {
      ioError = error;
      child.stdin?.end();
    } finally { polling = false; }
  }
  const interval = setInterval(() => void poll(), options.pollMs ?? 100);
  async function receiptAfterExit() {
    receipt = await bounded(readReceipt(receiptPath), ioMs, 'Owned receipt read');
    return receipt;
  }
  let stopping;
  function stop() {
    if (!stopping) stopping = (async () => {
      try { child.stdin?.end(); } catch (error) { ioError = ioError || error; }
      try {
        if (!exitObserved) await bounded(exit, terminationMs + stopGraceMs, 'Owned supervisor cancellation');
        const result = receipt || await receiptAfterExit();
        return { confirmedTreeEmpty: matchesRequest(result, request) && cleanupReceipt(result), receipt: result, logPath };
      } catch (error) {
        // The supervisor independently closes its private job on EOF/deadline.
        // Missing evidence remains unconfirmed; never equate a kill request,
        // wrapper exit, taskkill error/nonzero or output EOF with tree death.
        return { confirmedTreeEmpty: false, error: String(error), logPath };
      }
    })();
    return stopping;
  }
  const done = (async () => {
    try {
      const ended = await bounded(exit, timeoutMs + drainMs + terminationMs + outerGraceMs, 'Owned supervisor');
      await receiptAfterExit();
      if (ioError) throw ioError;
      const tail = await bounded(read(logPath, null), ioMs, 'Owned final output read');
      output = tail.data.toString('utf8').slice(-64000);
      if (ioError) throw ioError;
      if (ended.code !== 0 || !matchesRequest(receipt, request) || !terminalReceipt(receipt)) {
        const error = new Error(`Owned target failed: ${receipt?.outcome || 'invalid-receipt'}; cleanup confirmed=${matchesRequest(receipt, request) && cleanupReceipt(receipt)}; log=${logPath}`);
        error.ownedReceipt = receipt;
        throw error;
      }
      return { code: receipt.targetExitCode, unsignedCode: receipt.targetExitCodeUnsigned, output, receipt, logPath };
    } catch (error) {
      error.ownedCleanup = await stop();
      throw error;
    } finally {
      finished = true;
      clearInterval(interval);
      try { child.stdin?.end(); } catch { }
    }
  })();
  done.catch(() => {});
  return { done, stop, logPath, receiptPath, supervisorLaunchPid: child.pid, get receipt() { return receipt; }, get cleanupConfirmed() { return matchesRequest(receipt, request) && cleanupReceipt(receipt); } };
}

async function stopOwnedProbe(probe) {
  if (!probe) return { confirmedTreeEmpty: true, noActiveProbe: true };
  if (typeof probe.stop !== 'function') return { confirmedTreeEmpty: false, error: 'Probe has no owned termination contract' };
  try { return await bounded(probe.stop(), 20000, 'Owned probe cancellation'); }
  catch (error) { return { confirmedTreeEmpty: false, error: String(error) }; }
}
module.exports = { bounded, readSegment, receiptShape, cleanupReceipt, terminalReceipt, matchesRequest, startOwnedWindowsProcess, stopOwnedProbe };
