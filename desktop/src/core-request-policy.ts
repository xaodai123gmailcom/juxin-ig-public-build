const allowedPostPaths = new Set([
  "/api/session/register",
  "/api/session/login",
  "/api/session/resume",
  "/api/session/logout",
  "/api/workbench/commands",
  "/api/workbench/review/query",
  "/api/workbench/accounts/export",
  "/api/follow-monitor/runs",
  "/api/follow-monitor/control",
  "/api/studio/command",
  "/api/posting/command",
  "/api/reports/query",
  "/api/reports/split-review",
  "/api/reports/private-follow-review",
  "/api/reports/review-decision",
  "/api/accounts/command",
  "/api/cloud/command",
]);

const allowedMethods = new Set(["GET", "POST"]);

/** Node/Electron fetch must expose redirects instead of following them. */
export const coreFetchRedirectMode = "manual" as const;

/** Login commands include browser startup, CDP attachment and page loading. */
export function coreRequestTimeoutMs(path: string, body: unknown) {
  const command = body && typeof body === "object"
    ? body as { action?: unknown; type?: unknown }
    : {};
  if (path === "/api/accounts/command"
      && (command.action === "save_with_cookies" || command.action === "open" || command.action === "import_cookies" || command.action === "profile_preview")) return 180_000;
  if (path === "/api/workbench/commands" && command.type === "bitbrowser_open") return 180_000;
  return 30_000;
}

export function assertDirectCoreResponse(
  response: Pick<Response, "status" | "redirected" | "type">,
) {
  if (
    response.redirected
    || response.type === "opaqueredirect"
    || (response.status >= 300 && response.status < 400)
  ) {
    throw new Error("本机 Core 返回了不允许的重定向响应");
  }
}

function isAllowedWorkbenchSnapshotQuery(path: string) {
  const match = /^\/api\/workbench\/snapshot\?limit=(\d{1,4})&history_limit=(\d{1,4})(?:&platform=(instagram))?(?:&compact=1)?$/.exec(path);
  if (!match) return false;
  const limit = Number(match[1]);
  const historyLimit = Number(match[2]);
  return Number.isSafeInteger(limit)
    && Number.isSafeInteger(historyLimit)
    && limit >= 1
    && historyLimit >= 1
    && limit <= 5_000
    && historyLimit <= 5_000;
}

function isAllowedPostingSnapshot(path: string) {
  if (path === "/api/posting/snapshot") return true;
  if (!path.startsWith("/api/posting/snapshot?")) return false;
  const query = new URLSearchParams(path.slice(path.indexOf("?") + 1));
  const keys = [...query.keys()];
  return keys.length > 0 && keys.every(key => ["timezone", "cursor", "limit"].includes(key) && query.getAll(key).length === 1)
    && (!query.has("timezone") || /^[A-Za-z0-9_+\/-]{1,100}$/.test(query.get("timezone") || ""))
    && (!query.has("cursor") || /^[A-Za-z0-9_-]{1,254}={0,2}$/.test(query.get("cursor") || ""))
    && (!query.has("limit") || /^(?:[1-9]\d?|1\d{2}|200)$/.test(query.get("limit") || ""));
}

export function isAllowedCorePath(path: string) {
  return typeof path === "string"
    && path.length <= 512
    && !path.includes("..")
    && (
      path === "/api/workbench/snapshot" || path === "/api/workbench/storage" || path === "/api/workbench/live-status"
      || path === "/api/cloud/status" || path === "/api/accounts/unread" || path === "/api/accounts/snapshot" || path === "/api/studio/snapshot"
      || path === "/api/follow-monitor/snapshot"
      || path === "/api/follow-monitor/diagnostics"
      || isAllowedWorkbenchSnapshotQuery(path)
      || isAllowedPostingSnapshot(path)
      || allowedPostPaths.has(path)
    );
}

export function normalizeCoreMethod(method?: string) {
  const normalized = (method || "GET").toUpperCase();
  if (!allowedMethods.has(normalized)) throw new Error("不允许使用该本机请求方法");
  return normalized;
}

export function isAllowedCoreRequest(path: string, method: string) {
  if (!isAllowedCorePath(path)) return false;
  return method === "GET"
    ? isAllowedPostingSnapshot(path) || path === "/api/cloud/status" || path === "/api/accounts/unread" || path === "/api/accounts/snapshot" || path === "/api/studio/snapshot" || path === "/api/workbench/snapshot" || path === "/api/workbench/storage" || path === "/api/workbench/live-status" || path === "/api/follow-monitor/snapshot" || path === "/api/follow-monitor/diagnostics" || isAllowedWorkbenchSnapshotQuery(path)
    : method === "POST" && allowedPostPaths.has(path);
}

export function resumeTokenFromBody(path: string, body: unknown) {
  if (path !== "/api/session/resume" || !body || typeof body !== "object" || !("session_token" in body)) return null;
  const value = String((body as { session_token: unknown }).session_token ?? "").trim();
  return value || null;
}

export function sessionTokenFromResponse(path: string, payload: unknown) {
  if (!["/api/session/login", "/api/session/register", "/api/session/resume"].includes(path)) return null;
  if (!payload || typeof payload !== "object" || !("session_token" in payload)) return null;
  const value = (payload as { session_token?: unknown }).session_token;
  return typeof value === "string" && value.trim() ? value : null;
}

export function shouldClearSessionTokenAfterRequest(path: string) {
  return path === "/api/session/logout";
}


/** A response may only update/return data for the latest login boundary. */
export class CoreSessionFence {
  private revision = 0;

  begin(path: string): number {
    if (["/api/session/login", "/api/session/register", "/api/session/resume", "/api/session/logout"].includes(path)) {
      this.revision += 1;
    }
    return this.revision;
  }

  assertCurrent(revision: number): void {
    if (revision !== this.revision) throw new Error("登录状态已变化，请重新操作");
  }
}
