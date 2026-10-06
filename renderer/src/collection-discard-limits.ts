// The sole count / public post-age limits. Legacy source_limits and mode_limits
// are retained by Core only for compatibility, never used as hidden gates.
export const DISCARD_LIMIT_KEYS = [
  "private_discard_followers_max",
  "private_discard_following_max",
  "private_discard_posts_max",
  "public_discard_followers_max",
  "public_discard_following_max",
  "public_discard_posts_max",
  "public_discard_active_days_max",
] as const;

export type DiscardLimitKey = typeof DISCARD_LIMIT_KEYS[number];
export type DiscardCountLimitsPayload = {
  discard_count_limits_enabled: boolean;
} & Record<DiscardLimitKey, number>;

export type DiscardCountLimitsDraft = {
  enabled: boolean;
  limits: Record<DiscardLimitKey, string>;
};

function defaultDiscardLimit(key: DiscardLimitKey): number {
  return key === "public_discard_active_days_max" ? 0 : 4000;
}

/** Empty/invalid input must never silently disable a limit by becoming zero. */
export function parseDiscardLimit(value: unknown): number | null {
  if (typeof value === "number") return Number.isSafeInteger(value) && value >= 0 ? value : null;
  if (typeof value !== "string" || !/^\d+$/.test(value.trim())) return null;
  const number = Number(value.trim());
  return Number.isSafeInteger(number) ? number : null;
}

export function readDiscardCountLimits(settings: unknown): DiscardCountLimitsDraft {
  const saved = settings && typeof settings === "object" && !Array.isArray(settings)
    ? settings as Record<string, unknown> : {};
  return {
    enabled: saved.discard_count_limits_enabled !== false,
    limits: Object.fromEntries(DISCARD_LIMIT_KEYS.map(key => [key, String(parseDiscardLimit(saved[key]) ?? defaultDiscardLimit(key))])) as Record<DiscardLimitKey, string>,
  };
}

export function discardCountLimitsError(draft: DiscardCountLimitsDraft): string {
  if (!draft.enabled) return "";
  return DISCARD_LIMIT_KEYS.some(key => parseDiscardLimit(draft.limits[key]) === null)
    ? "直接丢弃上限请填写非负整数；0 表示该项不限。" : "";
}

export function discardCountLimitsPayload(draft: DiscardCountLimitsDraft): DiscardCountLimitsPayload {
  const error = discardCountLimitsError(draft);
  if (error) throw new Error(error);
  return {
    discard_count_limits_enabled: draft.enabled,
    // Invalid unfinished input is only possible while this entire rule is off.
    // Keep a valid default for a later re-enable; never save NaN or an implicit 0.
    ...Object.fromEntries(DISCARD_LIMIT_KEYS.map(key => [key, parseDiscardLimit(draft.limits[key]) ?? defaultDiscardLimit(key)])),
  } as DiscardCountLimitsPayload;
}
