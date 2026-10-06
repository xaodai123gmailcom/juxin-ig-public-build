import { useEffect, useRef, useState } from 'react';
import { getCollectorCoreClient, type CoreStorageStatus } from './core-client';

export function storageDiagnosis(status: CoreStorageStatus | null) {
  const free = status?.disk_free_bytes;
  if (!status?.disk_probe_available || typeof free !== 'number' || !Number.isFinite(free) || free < 0) {
    return '尚无法确认磁盘剩余空间，不能据此判断磁盘已满';
  }
  const amount = free === 0 ? '0 B' : `${(free / 1024 ** 3).toFixed(2)} GB`;
  return `数据所在磁盘可用 ${amount}${status.low_space_warning === true
    ? '，空间不足，请先释放空间' : '；快照异常仍需检查其他原因'}`;
}

/** Small independent read: never scans history or automatically removes files. */
export function StorageStatus() {
  const [status, setStatus] = useState<CoreStorageStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const active = useRef(false), inFlight = useRef(false);
  async function refresh() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const result = await getCollectorCoreClient().storageStatus();
      if (active.current) setStatus(result);
    } catch {
      // An old positive disk reading must not masquerade as a current one.
      if (active.current) setStatus(null);
    } finally {
      inFlight.current = false;
      if (active.current) setBusy(false);
    }
  }
  useEffect(() => {
    active.current = true;
    void refresh();
    return () => { active.current = false; };
  }, []);
  return <div className="formal-capacity-banner" role="status">
    <span>{busy ? '正在检查数据所在磁盘的空间…' : storageDiagnosis(status)}</span>
    <button className="formal-button compact" disabled={busy} onClick={() => void refresh()}>检查磁盘空间</button>
  </div>;
}
