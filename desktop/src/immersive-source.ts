import { readFileSync, statSync } from 'node:fs';
import { IMMERSIVE_VERSION, verifyImmersiveSource } from './immersive-policy.js';

export const IMMERSIVE_UNAVAILABLE = '可选翻译组件未安装。请自行从官方渠道取得受支持的版本，按说明放入本机配置目录；采集和养号功能可继续使用。';
export type ImmersiveAvailability = { available: boolean; version: string; message: string };
export interface ImmersiveSource {
  status(): ImmersiveAvailability;
  load(): string;
}

/** An explicitly installed local file only. No download, bundled fallback or hash override. */
export class OptionalImmersiveSource implements ImmersiveSource {
  private cached?: { identity: string; source: string };
  constructor(private readonly file?: string) {}
  load(): string {
    try {
      if (!this.file) throw new Error('missing');
      const stat = statSync(this.file);
      if (!stat.isFile() || stat.size <= 0 || stat.size > 10_000_000) throw new Error('invalid');
      const identity = `${stat.dev}:${stat.ino}:${stat.size}:${stat.mtimeMs}:${stat.ctimeMs}`;
      if (this.cached?.identity === identity) return this.cached.source;
      const source = verifyImmersiveSource(readFileSync(this.file));
      this.cached = { identity, source };
      return source;
    } catch {
      this.cached = undefined;
      throw Object.assign(new Error(IMMERSIVE_UNAVAILABLE + ` 需要官方 ${IMMERSIVE_VERSION} 原始文件并通过完整性校验。`), { requiresConfiguration: true });
    }
  }
  status(): ImmersiveAvailability {
    try { this.load(); return { available: true, version: IMMERSIVE_VERSION, message: '' }; }
    catch (error) { return { available: false, version: IMMERSIVE_VERSION, message: (error as Error).message }; }
  }
}
