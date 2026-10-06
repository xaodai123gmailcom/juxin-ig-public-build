import { Camera, KeyRound, LogIn, UserPlus } from "lucide-react";
import {
  type CollectorCoreClient,
  getCollectorCoreClient,
} from "./core-client";
import { rollbackAuthenticatedSession } from "./auth-session-rollback";
import "./auth-gate.css";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";

type AuthUser = Record<string, unknown>;

type AuthResponse = {
  session_token: string;
  expires_at?: string;
  user: AuthUser;
};

export type AuthContextValue = {
  user: AuthUser;
  logout(): Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth 必须在 AuthGate 内使用");
  return value;
}

function validateAuthResponse(value: unknown): AuthResponse {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("本机 Core 返回了无效的会话响应");
  const response = value as Record<string, unknown>;
  if (typeof response.session_token !== "string" || !response.session_token.trim()) throw new Error("本机 Core 未返回有效会话令牌");
  if (!response.user || typeof response.user !== "object" || Array.isArray(response.user)) throw new Error("本机 Core 未返回有效用户信息");
  return response as AuthResponse;
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<"checking" | "anonymous" | "authenticated" | "blocked">("checking");
  const [user, setUser] = useState<AuthUser | null>(null);
  const [error, setError] = useState<string>("");
  const [retry, setRetry] = useState(0);
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const clientRef = useRef<CollectorCoreClient | null>(null);

  useEffect(() => {
    let active = true;
    setState("checking");
    setError("");
    void (async () => {
      let client: CollectorCoreClient;
      try {
        client = getCollectorCoreClient();
        clientRef.current = client;
      } catch (reason) {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : String(reason));
        setState("blocked");
        return;
      }

      try {
        const storedToken = await client.secureGet("session-token");
        if (!active) return;
        if (!storedToken) {
          if (active) setState("anonymous");
          return;
        }
        let resumedCoreSession = false;
        try {
          const rawResponse = await client.request("/api/session/resume", {
            method: "POST",
            body: { session_token: storedToken },
          });
          // A disposed effect may belong to a previous login/retry. Never let
          // its late response overwrite the currently remembered account.
          if (!active) return;
          resumedCoreSession = true;
          const response = validateAuthResponse(rawResponse);
          const stored = await client.secureSet("session-token", response.session_token);
          if (!stored) throw new Error("会话令牌无法写入桌面安全存储");
          if (!active) return;
          setUser(response.user);
          setState("authenticated");
        } catch (reason) {
          // Clearing/rolling back an expired effect can remove a newer login.
          // The active effect still performs the normal expired-token cleanup.
          if (!active) return;
          let rollbackFailure: unknown;
          try {
            if (resumedCoreSession) await rollbackAuthenticatedSession(client);
            else await client.secureDelete("session-token");
          } catch (rollbackReason) {
            rollbackFailure = rollbackReason;
          }
          if (!active) return;
          const detail = reason instanceof Error ? reason.message : "保存的登录已失效，请重新登录";
          const rollbackDetail = rollbackFailure
            ? `；会话回滚未完全确认：${rollbackFailure instanceof Error ? rollbackFailure.message : String(rollbackFailure)}`
            : "";
          setUser(null);
          setError(`保存的登录已失效：${detail}${rollbackDetail}`);
          setState("anonymous");
        }
      } catch (reason) {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : String(reason));
        setState("blocked");
      }
    })();
    return () => { active = false; };
  }, [retry]);

  const logout = useCallback(async () => {
    const client = clientRef.current;
    let failure: unknown;
    try {
      if (client) await client.logout();
    } catch (reason) {
      failure = reason;
    }
    try {
      if (client) await client.secureDelete("session-token");
    } catch (reason) {
      failure ??= reason;
    }
    // Local authentication must always end even when Core is already offline
    // or secure storage deletion reports an error.  The caller can still show
    // that error, but the workbench is never left authenticated after logout.
    setUser(null);
    setPassword("");
    setState("anonymous");
    if (failure) {
      const detail = failure instanceof Error ? failure.message : String(failure);
      setError(`已退出当前工作台，但本机会话清理未完全确认：${detail}`);
      throw failure;
    }
    setError("");
  }, []);

  const context = useMemo<AuthContextValue | null>(() => user ? { user, logout } : null, [logout, user]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const client = clientRef.current;
    if (!client || username.trim().length < 3 || password.length < 10) return;
    setSubmitting(true);
    setError("");
    let coreSessionCreated = false;
    try {
      const authEndpoint = authMode === "register" ? "/api/session/register" : "/api/session/login";
      const rawResponse = await client.request(authEndpoint, {
        method: "POST",
        body: authMode === "register"
          ? { username: username.trim(), password }
          : { username: username.trim(), password, remember_login: remember, auto_login: remember },
      });
      // Electron has accepted the returned bearer token before this renderer
      // receives the response. Every later failure must revoke that session.
      coreSessionCreated = true;
      const response = validateAuthResponse(rawResponse);
      if (remember) {
        const stored = await client.secureSet("session-token", response.session_token);
        if (!stored) throw new Error("会话令牌无法写入桌面安全存储");
      } else {
        await client.secureDelete("session-token");
      }
      setUser(response.user);
      setPassword("");
      setState("authenticated");
    } catch (reason) {
      let rollbackFailure: unknown;
      if (coreSessionCreated) {
        try {
          await rollbackAuthenticatedSession(client);
        } catch (rollbackReason) {
          rollbackFailure = rollbackReason;
        }
        setUser(null);
        setPassword("");
        setState("anonymous");
      }
      const detail = reason instanceof Error ? reason.message : String(reason);
      const rollbackDetail = rollbackFailure
        ? `；会话回滚未完全确认：${rollbackFailure instanceof Error ? rollbackFailure.message : String(rollbackFailure)}`
        : "";
      setError(`${detail}${rollbackDetail}`);
    } finally {
      setSubmitting(false);
    }
  }

  if (state === "authenticated" && context) {
    return <AuthContext.Provider value={context}>{children}</AuthContext.Provider>;
  }

  if (state === "checking") {
    return (
      <div className="auth-gate">
        <section className="auth-card auth-loading" aria-live="polite">
          <div className="auth-spinner" />
          <h1>正在恢复安全会话</h1>
        </section>
      </div>
    );
  }

  if (state === "blocked") {
    return (
      <div className="auth-gate">
        <section className="auth-card auth-loading">
          <div className="auth-brand" style={{ marginInline: "auto" }}><KeyRound size={28} /></div>
          <h1>本机 Core 连接已阻断</h1>
          <p>{error || "无法连接桌面安全组件，正式运行已停止。"}</p>
          <button className="auth-retry" onClick={() => setRetry((value) => value + 1)}>重新连接</button>
        </section>
      </div>
    );
  }

  return (
    <div className="auth-gate">
      <section className="auth-card">
        <div className="auth-brand"><Camera size={29} /></div>
        <h1>聚鑫国际</h1>
        <div className="auth-tabs" role="tablist" aria-label="身份验证方式">
          <button className={`auth-tab ${authMode === "login" ? "is-active" : ""}`} role="tab" aria-selected={authMode === "login"} onClick={() => { setAuthMode("login"); setError(""); }}>登录</button>
          <button className={`auth-tab ${authMode === "register" ? "is-active" : ""}`} role="tab" aria-selected={authMode === "register"} onClick={() => { setAuthMode("register"); setError(""); }}>首次注册</button>
        </div>
        <form className="auth-form" onSubmit={(event) => void submit(event)}>
          <label className="auth-field"><span>用户名（至少 3 个字符）</span><input className="auth-input" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="输入本机用户名" /></label>
          <label className="auth-field"><span>密码（至少 10 个字符）</span><input className="auth-input" type="password" autoComplete={authMode === "register" ? "new-password" : "current-password"} value={password} onChange={(event) => setPassword(event.target.value)} placeholder="输入密码" /></label>
          <label className="auth-check"><input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} />在此电脑安全保存登录</label>
          {error ? <p className="auth-error" role="alert">{error}</p> : null}
          <button className="auth-submit" type="submit" disabled={submitting || username.trim().length < 3 || password.length < 10}>
            {authMode === "login" ? <LogIn size={18} /> : <UserPlus size={18} />}
            {submitting ? "正在验证…" : authMode === "login" ? "安全登录" : "创建账号并登录"}
          </button>
        </form>
      </section>
    </div>
  );
}

export default AuthGate;
