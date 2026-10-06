/** Only first-party application storage; no notification/device permission. */
export function accountStoragePermission(permission: string, requesting: string, top: string): boolean {
  if (permission !== 'persistent-storage' && permission !== 'storage-access') return false;
  try {
    const origin = new URL(requesting), parent = new URL(top);
    return origin.protocol === 'https:' && !origin.port && !origin.username && !origin.password
      && origin.origin === parent.origin;
  } catch { return false; }
}
