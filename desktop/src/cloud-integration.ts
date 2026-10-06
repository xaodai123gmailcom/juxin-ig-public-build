export type CloudConfiguration = { enabled: boolean; projectUrl: string; publishableKey: string };
const disabled = (): CloudConfiguration => ({ enabled: false, projectUrl: '', publishableKey: '' });

export function validateCloudConfiguration(input: unknown, previous = disabled()): CloudConfiguration {
  if (!input || typeof input !== 'object' || Array.isArray(input)
      || Object.keys(input).some(key => !['enabled', 'projectUrl', 'publishableKey'].includes(key)))
    throw new Error('云端配置格式无效');
  const value = input as Record<string, unknown>;
  if (typeof value.enabled !== 'boolean' || typeof value.projectUrl !== 'string'
      || typeof value.publishableKey !== 'string' || value.projectUrl.length > 2048 || value.publishableKey.length > 4096)
    throw new Error('云端配置格式无效');
  let projectUrl = value.projectUrl.trim();
  if (projectUrl) {
    let url: URL;
    try { url = new URL(projectUrl); } catch { throw new Error('请输入 HTTPS 云端项目地址'); }
    if (url.protocol !== 'https:' || !url.hostname || url.username || url.password
        || url.pathname !== '/' || url.search || url.hash || (url.port && url.port !== '443')
        || /[^\x21-\x7e]|\\/.test(projectUrl) || !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/.test(url.hostname)
        || !url.hostname.includes('.') || /(?:^|\.)(?:localhost|local|internal)$/.test(url.hostname)
        || /^[\d.]+$/.test(url.hostname))
      throw new Error('云端项目地址必须是 HTTPS 站点根地址');
    projectUrl = url.origin;
  }
  const publishableKey = value.publishableKey.trim() || (projectUrl && projectUrl === previous.projectUrl ? previous.publishableKey : '');
  if (publishableKey) {
    let publicKey = /^sb_publishable_[A-Za-z0-9_-]+$/.test(publishableKey);
    if (!publicKey) {
      try {
        const parts = publishableKey.split('.');
        publicKey = parts.length === 3 && parts.every(part => /^[A-Za-z0-9_-]+$/.test(part))
          && JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8')).role === 'anon';
      } catch { /* Secrets and malformed keys are rejected without echoing them. */ }
    }
    if (!publicKey) throw new Error('仅可配置 Supabase publishable 或 anon 公钥，请勿填写服务端密钥');
  }
  if (value.enabled && (!projectUrl || !publishableKey)) throw new Error('启用云端前请填写项目地址和公钥');
  return { enabled: value.enabled, projectUrl, publishableKey };
}

export function readCloudConfiguration(read: () => string | null, encryptionAvailable: boolean): CloudConfiguration {
  if (!encryptionAvailable) return disabled();
  try { return validateCloudConfiguration(JSON.parse(read() || 'null')); } catch { return disabled(); }
}

type Dependencies = {
  encryptionAvailable: () => boolean;
  readEncrypted: () => string | null;
  writeEncrypted: (value: string) => void;
  rememberSecret: (value: string) => void;
  activate: (value: CloudConfiguration) => Promise<unknown>;
};

/** Serialized, write-only configuration with no network contact to a cloud service. */
export class CloudConfigurationController {
  private queue: Promise<unknown> = Promise.resolve();
  constructor(private readonly dependencies: Dependencies) {}
  save(input: unknown) {
    // Capture the renderer value before waiting for a previous save.
    const value = structuredClone(input);
    const operation = this.queue.then(() => this.commit(value));
    this.queue = operation.catch(() => undefined);
    return operation;
  }
  private async commit(input: unknown) {
    const d = this.dependencies;
    if (!d.encryptionAvailable()) throw new Error('系统密钥库不可用，未保存云端配置');
    const previous = readCloudConfiguration(d.readEncrypted, true);
    const next = validateCloudConfiguration(input, previous);
    // Confirm that the old destination is stopped before replacing persistent
    // configuration. A failed activation must never leave old auto-sync alive.
    let stopped: unknown;
    try { stopped = await d.activate(disabled()); } catch { throw new Error('未能暂停当前云端连接，配置未更改，请稍后重试'); }
    if (!(stopped && typeof stopped === 'object' && (stopped as any).activated === true && (stopped as any).configured === false))
      throw new Error('未能确认云端连接已暂停，配置未更改');
    try { d.writeEncrypted(JSON.stringify(next)); } catch { throw new Error('云端连接已暂停，但配置未能安全保存，请检查系统密钥库'); }
    d.rememberSecret(next.publishableKey);
    let activated = false;
    try {
      const reply = await d.activate(next) as any;
      activated = reply?.activated === true && reply?.configured === next.enabled
        && reply?.enabled === next.enabled && reply?.project_url === next.projectUrl;
    } catch { /* Old connection stays stopped; new settings apply at next launch. */ }
    return { restarted: false, saved: true, cloud_configured: next.enabled,
      cloud_activated: activated, restart_required: !activated };
  }
}
