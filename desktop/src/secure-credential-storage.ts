/** Shared OS-backed credential-storage check for optional integrations. */
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
