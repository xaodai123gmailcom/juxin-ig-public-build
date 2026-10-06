/** Reuse equal JSON branches without mutating either authoritative response.
 * Metadata, removals, nulls and newly introduced fields always remain visible.
 * Inputs are decoded JSON snapshots, not cyclic objects or class instances.
 */
export function shareUnchangedJson<T>(previous: T | null | undefined, next: T): T {
  if (Object.is(previous, next)) return previous as T;
  if (previous === null || next === null || typeof previous !== 'object' || typeof next !== 'object') return next;
  if (Array.isArray(previous) !== Array.isArray(next)) return next;
  if (Array.isArray(next)) {
    const before = previous as unknown as unknown[];
    const result = next.map((value, index) => shareUnchangedJson(before[index], value));
    return (before.length === result.length && result.every((value, index) => Object.is(value, before[index]))
      ? previous : result) as T;
  }
  const before = previous as Record<string, unknown>, after = next as Record<string, unknown>;
  const keys = Object.keys(after);
  let equal = Object.keys(before).length === keys.length;
  const result = { ...after };
  for (const key of keys) {
    result[key] = shareUnchangedJson(before[key], after[key]);
    if (!Object.prototype.hasOwnProperty.call(before, key) || !Object.is(result[key], before[key])) equal = false;
  }
  return (equal ? previous : result) as T;
}
