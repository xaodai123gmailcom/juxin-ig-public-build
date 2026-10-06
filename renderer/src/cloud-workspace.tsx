import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Cloud, RefreshCw, LogOut } from 'lucide-react';
import { getCollectorCoreClient } from './core-client';

type CloudState = {
  project_url: string;
  enabled: boolean;
  configured: boolean;
  signed_in: boolean;
  email: string;
  revision: number;
  last_sync_at: string | null;
  message: string;
  auto_sync: boolean;
};

export function CloudWorkspace() {
  const [state, setState] = useState<CloudState | null>(null);
  const [email, setEmail] = useState(''), [password, setPassword] = useState('');
  const [enabled, setEnabled] = useState(false), [projectUrl, setProjectUrl] = useState('');
  const [publishableKey, setPublishableKey] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [note, setNote] = useState('');
  const alive = useRef(false), pending = useRef(false), initialized = useRef(false), epoch = useRef(0);

  const load = useCallback(async () => {
    const requestEpoch = epoch.current;
    try {
      const value = await getCollectorCoreClient().cloudStatus<CloudState>();
      if (!alive.current || pending.current || requestEpoch !== epoch.current) return;
      setState(value);
      if (!initialized.current) {
        initialized.current = true;
        setEnabled(value.enabled);
        setProjectUrl(value.project_url);
      }
    } catch {
      if (alive.current && !pending.current && requestEpoch === epoch.current) setError('暂时无法读取云端状态，请稍后重试');
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void load();
    const timer = setInterval(() => void load(), 5000);
    return () => { alive.current = false; epoch.current++; clearInterval(timer); };
  }, [load]);

  async function command(action: string) {
    if (pending.current || !state?.configured) return;
    pending.current = true; epoch.current++; setBusy(true); setError(''); setNote('');
    try {
      const result = await getCollectorCoreClient().cloudCommand<CloudState>({
        action, ...(['login', 'signup'].includes(action) ? { email, password } : {}),
      });
      if (alive.current) setState(result);
    } catch {
      if (alive.current) setError('云端操作未完成，请检查项目配置、邮箱验证和登录信息后重试');
    } finally {
      pending.current = false;
      if (alive.current) { setPassword(''); setBusy(false); }
    }
  }

  async function saveConfiguration(event: FormEvent) {
    event.preventDefault();
    if (pending.current) return;
    pending.current = true; epoch.current++; setBusy(true); setError(''); setNote('');
    try {
      const result = await getCollectorCoreClient().configureIntegrations({
        cloud: { enabled, projectUrl: projectUrl.trim(), publishableKey: publishableKey.trim() },
      });
      if (!alive.current) return;
      setPublishableKey(''); setPassword('');
      setNote(result.cloud_activated
        ? (result.cloud_configured ? '项目配置已安全保存并启用。登录后开始自动备份' : '云端已停用，本机数据保留')
        : '配置已安全保存，云端已停用，需重启应用后激活新配置');
    } catch {
      if (alive.current) setError('配置未激活。请检查 HTTPS 项目地址、发布密钥和系统安全存储后重试');
    } finally {
      pending.current = false;
      if (alive.current) { setPublishableKey(''); setBusy(false); void load(); }
    }
  }

  function submit(event: FormEvent) { event.preventDefault(); void command('login'); }

  return <section className="formal-panel cloud-panel">
    <header className="formal-panel-header">
      <div className="formal-panel-title"><span><Cloud size={21}/></span><div><h2>云端工作区</h2>{state?.signed_in && <p>{state.email} · 自动备份已开启</p>}</div></div>
      <button className="formal-button" disabled={busy || !state?.configured} onClick={() => void command('probe')}><RefreshCw size={15}/>检查连接</button>
    </header>
    <div style={{ padding: 20 }}>
      {error && <div className="formal-error-banner">{error}</div>}
      {note && <p role="status">{note}</p>}
      <p>{state?.message || '正在读取云端状态…'}</p>
      <form onSubmit={saveConfiguration} className="account-actions">
        <label className="formal-field"><span>自己的 Supabase 项目地址</span><input type="url" className="formal-input" maxLength={2048} disabled={busy} required={enabled} value={projectUrl} onChange={event => setProjectUrl(event.target.value)} placeholder="https://your-project.supabase.co"/></label>
        <label className="formal-field"><span>发布密钥 / anon key</span><input type="password" autoComplete="off" spellCheck={false} className="formal-input" maxLength={4096} disabled={busy} value={publishableKey} onChange={event => setPublishableKey(event.target.value)} placeholder="首次配置必填；同一项目可留空沿用已保存密钥"/></label>
        <label><input type="checkbox" disabled={busy} checked={enabled} onChange={event => setEnabled(event.target.checked)}/>启用此项目的云端备份</label>
        <button className="formal-button" disabled={busy || !state || (enabled && !projectUrl.trim())} type="submit">保存云端配置</button>
      </form>
      <p>默认关闭。启用并登录后，工作区记录与管理素材将自动上传到此项目。请先在自己的项目中执行 cloud/supabase-init.sql，并使用发布密钥或 anon key，勿使用 service_role 或 secret key。</p>
      {state?.configured && <>
        <p>当前项目：{state.project_url}</p>
        {state.signed_in ? <div className="account-actions">
          <span>云端版本 {state.revision} · {state.last_sync_at ? new Date(state.last_sync_at).toLocaleString() : '尚未备份'}</span>
          <button className="formal-button primary" disabled={busy} onClick={() => void command('sync')}><Cloud size={16}/>立即同步</button>
          <button className="formal-button" disabled={busy} onClick={() => void command('logout')}><LogOut size={15}/>断开</button>
        </div> : <form onSubmit={submit} className="account-actions">
          <label className="formal-field"><span>云端邮箱</span><input required type="email" autoComplete="username" className="formal-input" disabled={busy} value={email} onChange={event => setEmail(event.target.value)} placeholder="你的邮箱"/></label>
          <label className="formal-field"><span>云端密码</span><input required minLength={8} type="password" autoComplete="current-password" className="formal-input" disabled={busy} value={password} onChange={event => setPassword(event.target.value)} placeholder="此项目邮箱账号的密码"/></label>
          <button className="formal-button primary" disabled={busy || !email || !password} type="submit">{busy ? '正在连接…' : '登录并开启自动备份'}</button>
          <button className="formal-button" disabled={busy || !email || password.length < 8} type="button" onClick={() => void command('signup')}>注册云端账号</button>
        </form>}
      </>}
    </div>
  </section>;
}
