/** Write-only local credential configuration; never starts or stops Core. */
export type PexelsSaveResult = {
  restarted: false;
  saved: true;
  pexels_configured: boolean;
  pexels_activated: boolean;
  restart_required: boolean;
};

export type PexelsCredentialDependencies = {
  encryptionAvailable: () => boolean;
  writeEncrypted: (value: string) => void;
  rememberSecret: (value: string) => void;
  activate: (value: string) => Promise<unknown>;
};

export function validatePexelsKey(value: unknown): string {
  if (typeof value !== 'string' || value.length > 1024) throw new Error('Pexels 密钥格式无效');
  const key = value.trim();
  // Empty explicitly removes the saved key. Reject header/control characters,
  // but do not guess a provider-specific key length or expose it in errors.
  if (key && /[^\x21-\x7e]/.test(key)) throw new Error('Pexels 密钥格式无效');
  return key;
}

export function secureCredentialStorageAvailable(
  storage: {isEncryptionAvailable(): boolean; getSelectedStorageBackend?: () => string},
  platform: string,
): boolean {
  try {
    if (!storage.isEncryptionAvailable()) return false;
    // Electron's Linux basic_text fallback does not provide an OS key store.
    if (platform === 'linux') {
      const backend = storage.getSelectedStorageBackend?.();
      if (!backend || backend === 'basic_text' || backend === 'unknown') return false;
    }
    return true;
  } catch {
    return false;
  }
}

export class PexelsCredentialController {
  private queue: Promise<unknown> = Promise.resolve();
  constructor(private readonly dependencies: PexelsCredentialDependencies) {}

  save(input: unknown): Promise<PexelsSaveResult> {
    // Serialize both persistence and activation: an earlier slow response can
    // never activate an obsolete key after a newer user save has completed.
    const key = validatePexelsKey(input);
    const operation = this.queue.then(() => this.commit(key));
    this.queue = operation.catch(() => undefined);
    return operation;
  }

  private async commit(key: string): Promise<PexelsSaveResult> {
    let encryptionAvailable = false;
    try { encryptionAvailable = this.dependencies.encryptionAvailable(); } catch { /* fail closed */ }
    if (!encryptionAvailable) throw new Error('系统密钥库不可用，未保存 Pexels 密钥');
    try {
      this.dependencies.writeEncrypted(key);
    } catch {
      throw new Error('Pexels 密钥未能安全保存，请检查系统密钥库');
    }
    // Keep old and new values in the current process log redaction set. Never
    // send the key to Core before its diagnostic output is covered.
    this.dependencies.rememberSecret(key);
    let activated = false;
    try {
      const reply = await this.dependencies.activate(key);
      activated = Boolean(reply && typeof reply === 'object'
        && (reply as {activated?: unknown}).activated === true
        && (reply as {configured?: unknown}).configured === Boolean(key));
    } catch {
      // Durable storage succeeded. Preserve live tasks and make the deferred
      // activation explicit instead of restarting Core or echoing its error.
    }
    return {restarted: false, saved: true, pexels_configured: Boolean(key),
      pexels_activated: activated, restart_required: !activated};
  }
}
