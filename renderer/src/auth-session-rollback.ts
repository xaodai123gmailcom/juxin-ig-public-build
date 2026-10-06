/** Minimal surface needed to revoke a Core session after persistence fails. */
export type AuthSessionRollbackClient = {
  logout(): Promise<unknown>;
  secureDelete(key: string): Promise<boolean>;
};

/**
 * Revoke the Electron/Core bearer token and remove any partially-written
 * encrypted token.  Both operations are attempted even if the first fails.
 */
export async function rollbackAuthenticatedSession(client: AuthSessionRollbackClient) {
  const failures: string[] = [];
  try {
    await client.logout();
  } catch (reason) {
    failures.push(reason instanceof Error ? reason.message : String(reason));
  }
  try {
    const deleted = await client.secureDelete("session-token");
    if (!deleted) failures.push("桌面安全存储未确认删除会话令牌");
  } catch (reason) {
    failures.push(reason instanceof Error ? reason.message : String(reason));
  }
  if (failures.length) throw new Error(failures.join("；"));
}
