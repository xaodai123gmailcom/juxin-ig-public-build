// Test-only bridge for the actual React target-preview controls.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { ProfilePreviewHost } from '../../src/profile-preview';
import '../../src/formal-workbench.css';
import '../../src/account-workspace.css';
const fixture = { listMode: 'normal', status: 403, locked: false, commands: [] as any[], surfaces: [] as any[], open: (_url: string) => {} };
Object.assign(window, { fixture });
window.collectorCore = {
  secureSet: async () => true, secureGet: async () => null, secureDelete: async () => true,
  configureIntegrations: async () => ({ restarted: true }),
  onProfilePreview(listener) { fixture.open = listener; return () => { fixture.open = () => {}; }; },
  async accountSurface(body) { const result = { attached: body.visible && !fixture.locked }; fixture.surfaces.push({ ...body, ...result }); return result; },
  async request(path, options) {
    if (path.endsWith('/snapshot') && fixture.listMode === 'error') throw new Error('fixture unavailable');
    if (path.endsWith('/snapshot') && fixture.listMode === 'empty') return { plans: [], locks: {} } as any;
    if (path.endsWith('/snapshot')) return { plans: [
      { id: 'idle', name: '审核账号', username: 'review.user', native: true, platform: 'instagram', profile_id: 'profile-idle' },
      { id: 'busy', name: '正在采集的账号', username: 'busy.user', native: true, platform: 'instagram', profile_id: 'profile-busy' },
    ], locks: { 'profile-busy': {}, ...(fixture.locked ? { 'profile-idle': {} } : {}) } } as any;
    const body = options?.body as any; fixture.commands.push(body);
    if (body.id !== 'idle' || fixture.locked) throw new Error('窗口正由任务使用');
    const ok = fixture.status === 200 || body.preview_action === 'login';
    return { opened: true, page_loaded: ok, http_status: ok ? 200 : 403, message: ok ? '' : 'Instagram 或代理拒绝了请求（HTTP 403）。请检查登录状态。' } as any;
  },
};
createRoot(document.getElementById('root')!).render(<div className="formal-shell" style={{ minHeight: '100vh', display: 'block', padding: 30 }}>
  <h1>人工审核</h1><p>本地测试页面</p>
  <button id="open-target" onClick={() => fixture.open('https://www.instagram.com/target.user/')}>查看目标账户</button>
  <ProfilePreviewHost windows={[]} />
</div>);
