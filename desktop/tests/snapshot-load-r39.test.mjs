import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { runInNewContext } from "node:vm";
import test from "node:test";
import { createCollectorCoreClient } from "../../renderer/src/core-client.ts";

// Execute the production policy and IPC handler without Electron. Controlled
// transport/body failures verify the same route checks and timeout boundaries.
const policySource = readFileSync(new URL("../src/core-request-policy.ts", import.meta.url), "utf8");
const policy = runInNewContext(stripTypeScriptTypes(policySource).replace(/\bexport /g, "") + `
({ CoreSessionFence, assertDirectCoreResponse, coreFetchRedirectMode,
coreRequestTimeoutMs, isAllowedCoreRequest, isAllowedCorePath, normalizeCoreMethod,
resumeTokenFromBody, sessionTokenFromResponse, shouldClearSessionTokenAfterRequest })`);
const source = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
const begin = source.indexOf('ipcMain.handle("core:request"');
const end = source.indexOf("// The backend authorizes account ownership", begin);
assert.ok(begin >= 0 && end > begin);

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const response = (payload) => ({ status: 200, ok: true, type: "basic", redirected: false, json: async () => payload });

function createIPC(fetcher) {
  const timers = new Map(), calls = [];
  let handler, nextTimer = 0;
  const context = {
    ...policy,
    coreSessionFence: new policy.CoreSessionFence(),
    requireMainSender() {},
    ipcMain: { handle(_name, callback) { handler = callback; } },
    coreBaseUrl: () => "http://127.0.0.1:17831",
    coreToken: "startup-fixture",
    activeSessionToken: "session-fixture",
    activeSessionOwner: "owner-fixture",
    clearNotificationSession() {}, embeddedBrowser: { hide() {} },
    notificationProfiles: new Set(), notificationActivations: new Map(), closeMessageToast() {},
    AbortController,
    setTimeout(callback, delay) { timers.set(++nextTimer, { callback, delay }); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) { calls.push({ url, options }); return fetcher(url, options); },
  };
  runInNewContext(stripTypeScriptTypes(source.slice(begin, end)), context);
  return {
    calls, timers, context,
    request(path, options = {}) { return handler({}, { path, ...options }); },
    expire() { assert.equal(timers.size, 1); const [{ callback, delay }] = timers.values(); assert.equal(delay, 30_000); callback(); },
  };
}

test("renderer task heartbeat reaches the production desktop GET bridge", async () => {
  const live = { revision: 7, generated_at: "2026-09-17T01:00:00Z", tasks: [] };
  const ipc = createIPC(async () => response(live));
  const client = createCollectorCoreClient({
    request: (path, options) => ipc.request(path, options),
    secureSet: async () => true, secureGet: async () => null,
    secureDelete: async () => true, configureIntegrations: async () => ({}),
  });
  assert.equal(await client.liveStatus(), live);
  assert.equal(ipc.calls.length, 1);
  assert.equal(ipc.calls[0].url, "http://127.0.0.1:17831/api/workbench/live-status");
  assert.equal(ipc.calls[0].options.method, "GET");
  assert.equal(ipc.calls[0].options.redirect, "manual");
  assert.equal(ipc.calls[0].options.headers.authorization, "Bearer session-fixture");
  assert.equal(ipc.timers.size, 0);
});

test("heartbeat permission permits no mutation or arbitrary route/query", async () => {
  const ipc = createIPC(async () => { throw new Error("must not fetch"); });
  for (const [path, options] of [
    ["/api/workbench/live-status", { method: "POST" }],
    ["/api/workbench/live-status?owner_id=other", {}],
    ["/api/workbench/live-status/extra", {}],
    ["/api/workbench/live-status", { body: { id: "other" } }],
  ]) await assert.rejects(ipc.request(path, options), /不允许/);
  assert.equal(ipc.calls.length, 0);
  assert.equal(ipc.timers.size, 0);
});

test("snapshot headers timeout aborts transport and removes its timer", async () => {
  const ipc = createIPC(async (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new Error("abort fixture")), { once: true });
  }));
  const pending = ipc.request("/api/workbench/snapshot?limit=2000&history_limit=2000");
  const rejected = assert.rejects(pending, /本机服务响应超时/);
  ipc.expire();
  await rejected;
  assert.equal(ipc.calls[0].options.signal.aborted, true);
  assert.equal(ipc.calls.length, 1);
  assert.equal(ipc.timers.size, 0);
  assert.equal(ipc.context.activeSessionToken, "session-fixture");
});

test("a stalled 200 response body is reported as timeout, never successful data", async () => {
  const reading = deferred();
  const ipc = createIPC(async (_url, { signal }) => ({ ...response(null), json: () => {
    reading.resolve();
    return new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new Error("aborted body")), { once: true }));
  } }));
  const pending = ipc.request("/api/workbench/snapshot");
  const rejected = assert.rejects(pending, /本机服务响应超时/);
  await reading.promise;
  ipc.expire();
  await rejected;
  assert.equal(ipc.timers.size, 0);
  assert.equal(ipc.context.activeSessionToken, "session-fixture");
});

test("invalid JSON rejects even a 200 command and does not resend its mutation", async () => {
  const ipc = createIPC(async () => ({ ...response(null), json: async () => { throw new SyntaxError("truncated JSON"); } }));
  await assert.rejects(ipc.request("/api/workbench/commands", {
    method: "POST", body: { type: "action_campaign_start", payload: {} },
  }), /本机服务返回了无效响应/);
  assert.equal(ipc.calls.length, 1);
  assert.equal(ipc.timers.size, 0);
});

test("failed response decoding still respects a newer login boundary", async () => {
  const body = deferred(), reading = deferred();
  let first = true;
  const ipc = createIPC(async () => first
    ? (first = false, { ...response(null), json: () => { reading.resolve(); return body.promise; } })
    : response({ session_token: "new-session", user: { id: "new-owner" } }));
  const old = ipc.request("/api/workbench/snapshot");
  const rejected = assert.rejects(old, /登录状态已变化/);
  await reading.promise;
  await ipc.request("/api/session/login", { method: "POST", body: {} });
  body.reject(new Error("old broken body"));
  await rejected;
  assert.equal(ipc.context.activeSessionToken, "new-session");
  assert.equal(ipc.context.activeSessionOwner, "new-owner");
  assert.equal(ipc.timers.size, 0);
});
