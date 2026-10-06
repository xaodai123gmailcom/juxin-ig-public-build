import type { CoreBitBrowserWindow, CoreCollectionTask } from "./core-client";

export type CollectionPlatform = "instagram";
type ProfileSubject = { username?: string; platform?: unknown; profile_url?: unknown; profile?: Record<string, unknown>; settings?: Record<string, unknown> };
export type CollectionSeed = { platform: CollectionPlatform; target: string; label: string };
const INSTAGRAM_HOSTS = new Set(["instagram.com", "www.instagram.com", "m.instagram.com"]);
const INSTAGRAM_NON_PROFILES = new Set(["p", "reel", "reels", "stories", "explore", "direct", "accounts"]);

/** Strict Instagram profiles only: no arbitrary hosts, credentials or post URLs. */
export function parseCollectionSeed(input: string, platform: CollectionPlatform = "instagram"): CollectionSeed | null {
  if (platform !== "instagram") return null;
  const raw = input.trim();
  if (!raw) return null;
  const looksLikeUrl = /^(?:https?:\/\/|(?:www\.|m\.)?instagram\.com(?:\/|$))/i.test(raw);
  if (!looksLikeUrl) {
    const username = raw.replace(/^@+/, "");
    return /^[A-Za-z0-9._]{1,30}$/.test(username) ? { platform: "instagram", target: username, label: username } : null;
  }
  try {
    const url = new URL(/^https?:\/\//i.test(raw) ? raw : `https://${raw}`);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.port || !INSTAGRAM_HOSTS.has(url.hostname.toLowerCase())) return null;
    const segments = url.pathname.split("/").filter(Boolean).map(decodeURIComponent);
    const username = segments[0] || "";
    if (segments.length !== 1 || INSTAGRAM_NON_PROFILES.has(username.toLowerCase()) || !/^[A-Za-z0-9._]{1,30}$/.test(username)) return null;
    return { platform: "instagram", target: username, label: username };
  } catch { return null; }
}

export function parseCollectionSeedDraft(draft: string, platform: CollectionPlatform = "instagram") {
  const inputs = draft.split(/[\s,，]+/).filter(Boolean);
  const parsed = inputs.map(input => ({ input, seed: parseCollectionSeed(input, platform) }));
  const invalid = parsed.filter(item => !item.seed).map(item => item.input);
  const targets = [...new Set(parsed.filter(item => item.seed).map(item => item.seed!.target))];
  return { targets, invalid, mismatched: [], valid: targets.length > 0 && invalid.length === 0 };
}

/** Canonical identity used to reconcile URL drafts with persisted queue rows. */
export function collectionSeedIdentity(input: string, platform: CollectionPlatform = "instagram"): string {
  return parseCollectionSeed(input, platform)?.target.toLowerCase() || "";
}

/** Do not silently relabel foreign or malformed records as Instagram. */
export function collectionPlatform(subject?: ProfileSubject | null): CollectionPlatform | null {
  const tags = [subject?.platform, subject?.settings?.platform, subject?.profile?.platform];
  return tags.some(value => value != null && value !== "" && value !== "instagram") || subject?.username?.includes(":") ? null : "instagram";
}

export function collectionProfileLabel(subject: ProfileSubject): string {
  return collectionPlatform(subject) === "instagram" ? (subject.username || "").replace(/^@+/, "") : "";
}

export function collectionProfileUrl(subject: ProfileSubject): string {
  if (collectionPlatform(subject) !== "instagram") return "#";
  const explicit = subject.profile_url || subject.profile?.profile_url;
  const parsed = typeof explicit === "string" ? parseCollectionSeed(explicit) : null;
  return `https://www.instagram.com/${encodeURIComponent(parsed?.target || collectionProfileLabel(subject))}/`;
}

export function collectionPlatformWindows(windows: CoreBitBrowserWindow[], platform: CollectionPlatform = "instagram") {
  return platform === "instagram" ? windows.filter(window => !window.platform || window.platform === "instagram") : [];
}

export function collectionPlatformTasks(tasks: CoreCollectionTask[], platform: CollectionPlatform = "instagram") {
  return platform === "instagram" ? tasks.filter(task => collectionPlatform(task) === "instagram") : [];
}
