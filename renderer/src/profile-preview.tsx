import { useEffect, useRef, useState } from 'react';
import { ExternalLink, LogIn, RefreshCw, X } from 'lucide-react';
import { getCollectorCoreClient, type CoreBitBrowserWindow } from './core-client';
import { AccountBrowserSurface } from './account-browser-surface';
import './profile-preview.css';

type Plan = { id: string; name: string; username: string; profile_id: string; native: boolean; platform: string };
type Accounts = { plans: Plan[]; locks: Record<string, unknown> };

export function ProfilePreviewHost({ windows }: { windows: CoreBitBrowserWindow[] }) {
  const [target, setTarget] = useState('');
  const lastPlan = useRef('');
  useEffect(() => window.collectorCore?.onProfilePreview?.(value => {
    try {
      const url = new URL(value), name = url.pathname.split('/').filter(Boolean)[0];
      if (url.origin === 'https://www.instagram.com' && /^[A-Za-z0-9._]{1,30}$/.test(name || '')) setTarget(name);
    } catch { /* Main process already validates the original link. */ }
  }), []);
  return target ? <ProfilePreview key={target} target={target} windows={windows} preferred={lastPlan.current}
    onSelect={id => { lastPlan.current = id; }} onClose={() => setTarget('')} /> : null;
}

function ProfilePreview({ target, windows, preferred, onSelect, onClose }: {
  target: string; windows: CoreBitBrowserWindow[]; preferred: string; onSelect: (id: string) => void; onClose: () => void;
}) {
  const [data, setData] = useState<Accounts | null>(null);
  const [selected, setSelected] = useState('');
  const [opened, setOpened] = useState(false), [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [listError, setListError] = useState('');
  const refreshList = useRef<() => void>(() => {});
  const live = useRef(true), pending = useRef(false);
  const plans = data?.plans.filter(p => p.native && p.platform === 'instagram') || [];
  const plan = plans.find(p => p.id === selected);
  const locked = (p: Plan) => Boolean(data?.locks[p.profile_id] || windows.find(w => w.id === p.profile_id)?.locked);

  async function navigate(id: string, action: 'target' | 'login') {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError('');
    try {
      const result = await getCollectorCoreClient().accountCommand({ action: 'profile_preview', id, username: target, preview_action: action });
      if (live.current) {
        setOpened(Boolean(result.opened));
        if (!result.page_loaded) setError(String(result.message || '目标主页未能加载，请重试。'));
      }
    } catch (reason) {
      if (live.current) setError(reason instanceof Error ? reason.message : String(reason));
    } finally { pending.current = false; if (live.current) setBusy(false); }
  }
  function choose(id: string) {
    if (pending.current) return;
    setSelected(id); setOpened(false); setError(''); onSelect(id);
    if (id) void navigate(id, 'target');
  }
  useEffect(() => {
    live.current = true;
    let polling = false, first = true;
    const refresh = async () => {
      if (polling) return; polling = true;
      try {
        const result = await getCollectorCoreClient().accountSnapshot<Accounts>();
        if (!live.current) return;
        setData(result); setListError('');
        if (first) {
          const available = result.plans.filter(p => p.native && p.platform === 'instagram' && !result.locks[p.profile_id]
            && !windows.find(w => w.id === p.profile_id)?.locked);
          const chosen = available.find(p => p.id === preferred) || (available.length === 1 ? available[0] : undefined);
          if (chosen) { first = false; choose(chosen.id); }
          else if (available.length > 1) first = false;
        }
      } catch (reason) { if (live.current) setListError('账号列表读取失败，请刷新账号列表。'); }
      finally { polling = false; }
    };
    refreshList.current = () => void refresh();
    void refresh(); const timer = setInterval(() => void refresh(), 3000);
    return () => { live.current = false; clearInterval(timer); };
  }, []);

  return <div className="profile-preview-overlay">
    <section className="profile-preview-panel" role="dialog" aria-modal="true" aria-label={`查看目标账号 ${target}`}>
      <header><div><strong>目标主页 · @{target}</strong></div>
        <button className="formal-button compact" aria-label="关闭目标主页" onClick={onClose}><X size={18} /></button></header>
      <div className="profile-preview-toolbar">
        <label>使用账号窗口<select aria-label="预览账号窗口" className="formal-input" disabled={busy || Boolean(listError) || !data} value={selected} onChange={e => choose(e.target.value)}>
          <option value="">请选择空闲的内置 Instagram 窗口</option>
          {plans.map(p => <option key={p.id} value={p.id} disabled={locked(p)}>{p.name}{p.username ? ` · @${p.username}` : ''}{locked(p) ? '（任务占用）' : ''}</option>)}
        </select></label>
        <button className="formal-button" disabled={!plan || busy || Boolean(listError) || locked(plan)} onClick={() => void navigate(selected, 'target')}><RefreshCw size={15} />重新打开目标</button>
        <button className="formal-button" disabled={!plan || busy || Boolean(listError) || locked(plan)} onClick={() => void navigate(selected, 'login')}><LogIn size={15} />登录 / 检查</button>
      </div>
      <div className="profile-preview-list-status"><span>{data ? `内置 Instagram 窗口 ${plans.length} 个 · 空闲 ${plans.filter(p => !locked(p)).length} 个` : '正在读取账号窗口…'}</span><button className="formal-button compact" disabled={busy} onClick={() => refreshList.current()}><RefreshCw size={14} />刷新账号列表</button></div>
      {listError && <div className="formal-error-banner" role="alert">{listError}</div>}
      {error && <div className="formal-error-banner" role="alert">{error}</div>}
      {listError ? <div className="profile-preview-empty">账号列表暂时不可用，可点击上方刷新。</div> : !data ? <div className="profile-preview-empty">正在读取账号窗口…</div> : !plans.length ? <div className="profile-preview-empty"><p>当前账号列表中没有内置 Instagram 窗口。</p>{Boolean(data.plans.length || windows.length) && <p>{data.plans.length ? `账号方案 ${data.plans.length} 个` : `账号窗口 ${windows.length} 个`}</p>}<a className="formal-button" href="#/accounts" onClick={onClose}><ExternalLink size={15} />前往账号页</a></div>
        : !plan ? <div className="profile-preview-empty">未选择账号窗口</div>
        : busy ? <div className="profile-preview-empty">正在加载 @{target}，请稍候…</div>
        : error ? <div className="profile-preview-empty">目标主页加载失败</div>
        : <AccountBrowserSurface id={plan.id} native opened={opened} locked={locked(plan)} visible disabled={busy || locked(plan)} onOpen={() => void navigate(selected, 'target')} />}
    </section>
  </div>;
}
