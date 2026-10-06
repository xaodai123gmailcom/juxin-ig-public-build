import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import * as requestPolicy from "../../dist-electron/core-request-policy.js";
import { assertDirectCoreResponse, coreFetchRedirectMode, coreRequestTimeoutMs, isAllowedCorePath, isAllowedCoreRequest, normalizeCoreMethod, resumeTokenFromBody, sessionTokenFromResponse, shouldClearSessionTokenAfterRequest } from "../../dist-electron/core-request-policy.js";

test('storage diagnosis is read-only and cannot address arbitrary files or owners', () => {
  assert.equal(isAllowedCoreRequest('/api/workbench/storage', 'GET'), true);
  assert.equal(isAllowedCoreRequest('/api/workbench/storage', 'POST'), false);
  for (const path of ['/api/workbench/storage?path=C:/', '/api/workbench/storage?owner=other', '/api/workbench/storage/../snapshot'])
    assert.equal(isAllowedCorePath(path), false);
});

test("account CSV export is allowed only as an explicit POST through the authenticated bridge", () => {
  assert.equal(isAllowedCoreRequest("/api/workbench/accounts/export", "POST"), true);
  assert.equal(isAllowedCoreRequest("/api/workbench/accounts/export", "GET"), false);
  for (const path of ["/api/workbench/accounts/export?owner=other", "/api/workbench/accounts/export/", "/api/workbench/accounts/export/../query"])
    assert.equal(isAllowedCorePath(path), false);
});

test("browser login has time to start and load while ordinary requests stay bounded", () => {
  const startupAndImportAndLoad = 60_000 + 15_000 + 30_000;
  for (const action of ["open", "import_cookies"])
    assert.ok(coreRequestTimeoutMs("/api/accounts/command", { action }) > startupAndImportAndLoad);
  assert.ok(coreRequestTimeoutMs("/api/workbench/commands", { type: "bitbrowser_open" }) > startupAndImportAndLoad);
  for (const [path, body] of [
    ["/api/accounts/snapshot", { action: "open" }],
    ["/api/accounts/command", { action: "close" }],
    ["/api/cloud/command", { action: "open" }],
    ["/api/workbench/commands", { type: "task_control" }],
    ["/api/accounts/command", null],
  ]) assert.equal(coreRequestTimeoutMs(path, body), 30_000);
});

test("new-generation API bridge stays on explicit local routes", () => {
  for (const path of [
    "/api/session/register",
    "/api/session/login",
    "/api/session/resume",
    "/api/session/logout",
    "/api/workbench/snapshot?limit=500&history_limit=500",
    "/api/workbench/snapshot?limit=1&history_limit=5000",
    "/api/workbench/commands",
    "/api/follow-monitor/snapshot",
    "/api/follow-monitor/diagnostics",
    "/api/follow-monitor/runs",
    "/api/follow-monitor/control",
    "/api/studio/snapshot",
    "/api/cloud/status",
    "/api/cloud/command",
    "/api/accounts/snapshot",
    "/api/accounts/command",
    "/api/studio/command",
  ]) assert.equal(isAllowedCorePath(path), true, path);
  for (const path of [
    "/api/health",
    "/api/workbench/review",
    "/api/bitbrowser/profiles",
    "/api/tasks/task-1/windows/window-2/pause",
    "/api/history/target-1",
    "/api/results",
    "/api/split-candidates/candidate-1",
    "/v1/events",
    "/api/workbench/snapshot?token=secret",
    "/api/workbench/snapshot?limit=0&history_limit=500",
    "/api/workbench/snapshot?limit=5001&history_limit=500",
    "/api/workbench/snapshot?history_limit=500&limit=500",
    "/api/workbench/snapshot?limit=500&history_limit=500&token=secret",
    "/api/results?all=true",
    "/api/../docs",
    "https://example.com/api/health",
  ]) assert.equal(isAllowedCorePath(path), false, path);

  assert.equal(isAllowedCoreRequest("/api/workbench/snapshot?limit=500&history_limit=500", "GET"), true);
  assert.equal(isAllowedCoreRequest("/api/workbench/snapshot?limit=500&history_limit=500", "POST"), false);
  assert.equal(isAllowedCoreRequest("/api/workbench/commands", "POST"), true);
  assert.equal(isAllowedCoreRequest("/api/follow-monitor/snapshot", "GET"), true);
  assert.equal(isAllowedCoreRequest("/api/follow-monitor/diagnostics", "GET"), true);
  assert.equal(isAllowedCoreRequest("/api/follow-monitor/diagnostics", "POST"), false);
  assert.equal(isAllowedCorePath("/api/follow-monitor/diagnostics?owner_id=other"), false);
  assert.equal(isAllowedCoreRequest("/api/follow-monitor/runs", "POST"), true);
  assert.equal(isAllowedCoreRequest("/api/follow-monitor/control", "POST"), true);
  assert.equal(isAllowedCoreRequest("/api/follow-monitor/control", "GET"), false);
  assert.equal(isAllowedCorePath("/api/follow-monitor/control?owner_id=other"), false);
  assert.equal(isAllowedCoreRequest("/api/workbench/commands", "GET"), false);
  assert.equal(isAllowedCoreRequest("/api/studio/snapshot", "GET"), true);
  assert.equal(isAllowedCoreRequest("/api/cloud/status", "GET"), true);
  assert.equal(isAllowedCoreRequest("/api/cloud/command", "POST"), true);
  assert.equal(isAllowedCoreRequest("/api/cloud/status", "POST"), false);
  assert.equal(isAllowedCoreRequest("/api/cloud/command", "GET"), false);
  assert.equal(isAllowedCorePath("/api/cloud/status?owner_id=other"), false);
  assert.equal(isAllowedCoreRequest("/api/accounts/snapshot", "GET"), true);
  assert.equal(isAllowedCoreRequest("/api/accounts/command", "POST"), true);
  assert.equal(isAllowedCoreRequest("/api/accounts/snapshot", "POST"), false);
  assert.equal(isAllowedCoreRequest("/api/accounts/command", "GET"), false);
  assert.equal(isAllowedCorePath("/api/accounts/snapshot?owner_id=other"), false);
  assert.equal(isAllowedCoreRequest("/api/studio/command", "POST"), true);
  assert.equal(isAllowedCoreRequest("/api/studio/snapshot", "POST"), false);
  assert.equal(isAllowedCoreRequest("/api/studio/command", "GET"), false);
  assert.equal(isAllowedCorePath("/api/studio/snapshot?owner_id=other"), false);
  assert.equal(isAllowedCorePath("/api/studio/command/extra"), false);
  assert.equal(isAllowedCoreRequest("/api/session/logout", "POST"), true);
  assert.equal(isAllowedCoreRequest("/api/session/logout", "GET"), false);
});

test("review layers and split/private-follow audits accept only their exact authenticated POST routes", () => {
  for (const path of ["/api/workbench/review/query", "/api/reports/split-review", "/api/reports/private-follow-review", "/api/reports/review-decision"]) {
    assert.equal(isAllowedCoreRequest(path, "POST"), true);
    for (const method of ["GET", "DELETE", "PATCH"]) assert.equal(isAllowedCoreRequest(path, method), false);
    for (const changed of [`${path}/`, `${path}?owner_id=other`, `${path}/extra`, `https://example.com${path}`]) {
      assert.equal(isAllowedCoreRequest(changed, "POST"), false);
    }
  }
});

test("bridge accepts only the required HTTP verbs", () => {
  assert.equal(normalizeCoreMethod(), "GET");
  assert.equal(normalizeCoreMethod("post"), "POST");
  assert.throws(() => normalizeCoreMethod("DELETE"), /不允许/);
  assert.throws(() => normalizeCoreMethod("PATCH"), /不允许/);
  assert.throws(() => normalizeCoreMethod("PUT"), /不允许/);
  assert.throws(() => normalizeCoreMethod("OPTIONS"), /不允许/);
});

test("only session endpoints may change the desktop bearer token", () => {
  assert.equal(resumeTokenFromBody("/api/session/resume", { session_token: "resume" }), "resume");
  assert.equal(resumeTokenFromBody("/api/workbench/commands", { session_token: "attack" }), null);
  assert.equal(sessionTokenFromResponse("/api/session/login", { session_token: "login" }), "login");
  assert.equal(sessionTokenFromResponse("/api/workbench/snapshot", { token: "not-a-session" }), null);
  assert.equal(shouldClearSessionTokenAfterRequest("/api/session/logout"), true);
  assert.equal(shouldClearSessionTokenAfterRequest("/api/session/login"), false);
  assert.equal(shouldClearSessionTokenAfterRequest("/api/workbench/commands"), false);
});

test("Core fetches never follow or accept redirects", () => {
  assert.equal(coreFetchRedirectMode, "manual");
  assert.throws(
    () => assertDirectCoreResponse({ status: 302, redirected: false, type: "basic" }),
    /不允许的重定向/,
  );
  assert.throws(
    () => assertDirectCoreResponse({ status: 0, redirected: false, type: "opaqueredirect" }),
    /不允许的重定向/,
  );
  assert.throws(
    () => assertDirectCoreResponse({ status: 200, redirected: true, type: "basic" }),
    /不允许的重定向/,
  );
  assert.doesNotThrow(() => assertDirectCoreResponse({ status: 200, redirected: false, type: "basic" }));
  assert.doesNotThrow(() => assertDirectCoreResponse({ status: 401, redirected: false, type: "basic" }));
});


// Exercise the compiled production IPC handler with controlled HTTP completion
// order. No Electron window or platform credential store is required.
function controlledSessionBridge() {
  const source = readFileSync(new URL("../../dist-electron/main.js", import.meta.url), "utf8");
  const begin = source.indexOf('ipcMain.handle("core:request"');
  const end = source.indexOf('// The backend authorizes account ownership', begin);
  assert.ok(begin >= 0 && end > begin);
  const pending = [];
  let handler;
  const ipcMain = { handle: (_name, value) => { handler = value; } };
  const fetch = () => new Promise((resolve, reject) => pending.push({ resolve, reject }));
  const setup = new Function("ipcMain", "fetch", "policy", `
    const { normalizeCoreMethod, isAllowedCoreRequest, resumeTokenFromBody,
      coreRequestTimeoutMs, shouldClearSessionTokenAfterRequest,
      coreFetchRedirectMode, assertDirectCoreResponse, sessionTokenFromResponse } = policy;
    const coreSessionFence = policy.CoreSessionFence ? new policy.CoreSessionFence() : null;
    const requireMainSender = () => {};
    const coreBaseUrl = () => "http://127.0.0.1:17831";
    const coreToken = "startup";
    let activeSessionToken = "old-token", activeSessionOwner = "old-owner";
    let clearCount = 0;
    const clearNotificationSession = () => { activeSessionOwner = null; clearCount++; };
    const embeddedBrowser = { hide() {} };
    let notificationProfiles = new Set();
    ${source.slice(begin, end)}
    return () => ({ token: activeSessionToken, owner: activeSessionOwner, clearCount });
  `);
  const state = setup(ipcMain, fetch, requestPolicy);
  const request = (path, body) => handler({}, { path, method: "POST", body });
  const finish = (index, status, payload = {}) => pending[index].resolve({
    status, ok: status >= 200 && status < 300, redirected: false, type: "basic",
    json: async () => payload,
  });
  return { request, finish, state, pending };
}

test("late login response cannot sign the desktop back in after logout", async () => {
  const bridge = controlledSessionBridge();
  const login = bridge.request("/api/session/login", {});
  const rejected = assert.rejects(login, /登录状态已变化/);
  const logout = bridge.request("/api/session/logout");
  bridge.finish(1, 204);
  await logout;
  bridge.finish(0, 200, { session_token: "late-token", user: { id: "late-owner" } });
  await rejected;
  assert.equal(bridge.state().token, null);
  assert.equal(bridge.state().owner, null);
});

test("an older resume failure cannot erase a newer successful login", async () => {
  const bridge = controlledSessionBridge();
  const resume = bridge.request("/api/session/resume", { session_token: "expired" });
  const rejected = assert.rejects(resume);
  const login = bridge.request("/api/session/login", {});
  bridge.finish(1, 200, { session_token: "new-token", user: { id: "new-owner" } });
  await login;
  bridge.finish(0, 401, { detail: "expired" });
  await rejected;
  assert.equal(bridge.state().token, "new-token");
  assert.equal(bridge.state().owner, "new-owner");
});

test("logout clears locally before the network completes and cannot erase a subsequent login", async () => {
  const bridge = controlledSessionBridge();
  const logout = bridge.request("/api/session/logout");
  const rejected = assert.rejects(logout, /登录状态已变化/);
  const immediatelyCleared = bridge.state().token;
  const login = bridge.request("/api/session/login", {});
  bridge.finish(1, 200, { session_token: "new-token", user: { id: "new-owner" } });
  await login;
  bridge.finish(0, 204);
  await rejected;
  assert.equal(immediatelyCleared, null);
  assert.equal(bridge.state().token, "new-token");
  assert.equal(bridge.state().owner, "new-owner");
});

test("a session change during response-body decoding also rejects the obsolete token", async () => {
  const bridge = controlledSessionBridge();
  let releaseBody;
  const body = new Promise(resolve => { releaseBody = resolve; });
  const login = bridge.request("/api/session/login", {});
  const rejected = assert.rejects(login, /登录状态已变化/);
  bridge.finish(0, 200, body);
  await new Promise(resolve => setImmediate(resolve));
  const logout = bridge.request("/api/session/logout");
  bridge.finish(1, 204);
  await logout;
  releaseBody({ session_token: "late-body-token", user: { id: "old-owner" } });
  await rejected;
  assert.equal(bridge.state().token, null);
  assert.equal(bridge.state().owner, null);
});

test('platform snapshot scope accepts only explicit IG while preserving bounded query policy',()=>{
 for(const platform of ['instagram']){
  const path=`/api/workbench/snapshot?limit=2000&history_limit=2000&platform=${platform}`;
  assert.equal(isAllowedCoreRequest(path,'GET'),true);
  assert.equal(isAllowedCoreRequest(path,'POST'),false);
 }
 for(const suffix of ['facebook','all','Facebook','facebook&owner=other','instagram&platform=facebook','facebook%00',''])
  assert.equal(isAllowedCorePath('/api/workbench/snapshot?limit=500&history_limit=500&platform='+suffix),false);
});

test('compact snapshots retain the same bounded GET-only IPC scope', () => {
  const base='/api/workbench/snapshot?limit=2000&history_limit=2000';
  for(const platform of ['', '&platform=instagram']) {
    const path=base+platform+'&compact=1';
    assert.equal(isAllowedCorePath(path),true,path);
    assert.equal(isAllowedCoreRequest(path,'GET'),true,path);
    assert.equal(isAllowedCoreRequest(path,'POST'),false,path);
  }
  for(const suffix of ['&platform=facebook&compact=1','&compact=0','&compact=true','&compact=2','&compact=1&compact=1','&compact=1&platform=facebook','&compact=1&owner=other','&compact=1#ignored'])
    assert.equal(isAllowedCorePath(base+suffix),false,suffix);
  assert.equal(isAllowedCorePath('/api/workbench/snapshot?limit=5001&history_limit=2000&compact=1'),false);
});

test('posting bridge is narrow and never exposes internal credential activation', () => {
  for (const path of ['/api/posting/snapshot','/api/posting/snapshot?timezone=UTC','/api/posting/snapshot?timezone=Asia%2FShanghai','/api/posting/snapshot?timezone=Etc%2FGMT%2B8','/api/posting/snapshot?timezone=UTC&cursor=WyJhIiwiYiJd&limit=50','/api/posting/snapshot?limit=200']) {
    assert.equal(isAllowedCoreRequest(path,'GET'),true,path);
    assert.equal(isAllowedCoreRequest(path,'POST'),false,path);
  }
  assert.equal(isAllowedCoreRequest('/api/posting/command','POST'),true);
  assert.equal(isAllowedCoreRequest('/api/posting/command','GET'),false);
  for (const path of ['/api/internal/integrations/pexels','/api/posting/snapshot?limit=0','/api/posting/snapshot?limit=201','/api/posting/snapshot?limit=1&limit=2','/api/posting/snapshot?cursor=abc%2Fdef','/api/posting/snapshot?cursor=abc&owner=other','/api/posting/snapshot?owner=other','/api/posting/snapshot?timezone=UTC&timezone=UTC','/api/posting/snapshot?timezone=UTC&key=secret','/api/posting/snapshot?timezone=..%2FUTC','/api/posting/command?owner=other'])
    for (const method of ['GET','POST']) assert.equal(isAllowedCoreRequest(path,method),false,path);
});
