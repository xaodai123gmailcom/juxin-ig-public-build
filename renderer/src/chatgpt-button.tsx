import { useEffect, useState } from 'react';
import { Sparkles } from 'lucide-react';

export function ChatGPTButton() {
  const [opened, setOpened] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => window.collectorCore?.onChatGPTVisibility?.(setOpened), []);
  useEffect(() => {
    if (!opened) return;
    const hide = () => void window.collectorCore?.chatgptPage?.({ hide: true })
      .then(() => setOpened(false)).catch(e => setError(String(e)));
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') hide(); };
    const observer = new MutationObserver(() => {
      if (document.querySelector('[aria-modal="true"], .account-drawer')) hide();
    });
    observer.observe(document.body, { childList: true, subtree: true });
    window.addEventListener('keydown', key);
    return () => { observer.disconnect(); window.removeEventListener('keydown', key); };
  }, [opened]);
  useEffect(() => {
    if (!opened) return;
    const header = document.querySelector('.formal-header');
    const update = () => void window.collectorCore?.chatgptPage?.({boundsOnly:true,top:(header?.getBoundingClientRect().bottom || 74)+4}).catch(()=>{});
    const observer = new ResizeObserver(update);if(header)observer.observe(header);
    window.addEventListener('resize',update);update();
    return()=>{observer.disconnect();window.removeEventListener('resize',update)};
  },[opened]);
  async function toggle() {
    setError('');
    if (!window.collectorCore?.chatgptPage) { setError('请使用新版桌面程序'); return; }
    setBusy(true);
    try { setOpened((await window.collectorCore.chatgptPage({top:(document.querySelector('.formal-header')?.getBoundingClientRect().bottom || 74)+4})).visible); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  return <button className="formal-button compact" title={error || 'ChatGPT'}
    aria-pressed={opened} disabled={busy} onClick={() => void toggle()}>
    <Sparkles size={15} />ChatGPT
  </button>;
}
