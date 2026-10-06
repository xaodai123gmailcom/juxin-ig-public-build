export type RendererNavigationDisposition = "local" | "external-https" | "deny";

export type WindowRectangle = {
  x: number;
  y: number;
  width: number;
  height: number;
};

const instagramHosts = new Set(["instagram.com", "www.instagram.com"]);
const instagramUsernamePattern = /^[A-Za-z0-9._]{1,30}$/;
const instagramReservedPaths = new Set([
  "about", "accounts", "challenge", "developer", "direct", "directory",
  "explore", "legal", "p", "reel", "reels", "stories", "tv", "web",
]);

function parsedUrl(value: string): URL | null {
  try {
    return new URL(value);
  } catch {
    return null;
  }
}

export function isAllowedDevelopmentRendererUrl(value: string): boolean {
  const url = parsedUrl(value);
  return Boolean(
    url
      && url.protocol === "http:"
      && url.hostname === "127.0.0.1"
      && !url.username
      && !url.password,
  );
}

export function normalizedExternalHttpsUrl(value: string): string | null {
  const target = parsedUrl(value);
  if (
    !target
    || target.protocol !== "https:"
    || target.username
    || target.password
  ) return null;
  return target.href;
}

function normalizedInstagramUrl(value: string): URL | null {
  const target = parsedUrl(value);
  if (
    !target
    || target.protocol !== "https:"
    || target.username
    || target.password
    || target.port
    || !instagramHosts.has(target.hostname.toLowerCase())
  ) return null;
  target.hostname = "www.instagram.com";
  return target;
}

/** Only a direct Instagram username URL may originate from an audit-row click. */
export function normalizedInstagramProfilePreviewUrl(value: string): string | null {
  const target = normalizedInstagramUrl(value);
  if (!target || target.search || target.hash) return null;
  const segments = target.pathname.split("/").filter(Boolean);
  if (segments.length !== 1) return null;
  let username: string;
  try { username = decodeURIComponent(segments[0]); } catch { return null; }
  if (
    !instagramUsernamePattern.test(username)
    || instagramReservedPaths.has(username.toLowerCase())
  ) return null;
  target.pathname = `/${username}/`;
  return target.href;
}

/** Once isolated in the preview, navigation may continue only within Instagram. */
export function normalizedInstagramPreviewNavigationUrl(value: string): string | null {
  return normalizedInstagramUrl(value)?.href ?? null;
}

/** Keep the owned preview centered on its parent while remaining inside the work area. */
export function centeredChildWindowBounds(
  parent: WindowRectangle,
  workArea: WindowRectangle,
  preferredWidth = 1080,
  preferredHeight = 780,
): WindowRectangle {
  const width = Math.max(1, Math.min(preferredWidth, parent.width - 80, workArea.width - 32));
  const height = Math.max(1, Math.min(preferredHeight, parent.height - 80, workArea.height - 32));
  const maximumX = workArea.x + workArea.width - width;
  const maximumY = workArea.y + workArea.height - height;
  const centeredX = parent.x + (parent.width - width) / 2;
  const centeredY = parent.y + (parent.height - height) / 2;
  return {
    x: Math.round(Math.min(Math.max(centeredX, workArea.x), maximumX)),
    y: Math.round(Math.min(Math.max(centeredY, workArea.y), maximumY)),
    width: Math.round(width),
    height: Math.round(height),
  };
}

export function classifyRendererNavigation(
  targetValue: string,
  trustedRendererValue: string,
): RendererNavigationDisposition {
  const target = parsedUrl(targetValue);
  const trusted = parsedUrl(trustedRendererValue);
  if (!target || !trusted) return "deny";

  const sameRendererDocument = (
    target.protocol === trusted.protocol
    // `URL.origin` is always the opaque string "null" for file: URLs.  It
    // therefore cannot distinguish the packaged local file from a crafted UNC
    // host such as file://attacker/C:/... .  Match every authority component
    // explicitly before accepting a hash-only navigation.
    && target.username === trusted.username
    && target.password === trusted.password
    && target.host === trusted.host
    && target.pathname === trusted.pathname
    && target.search === trusted.search
    && (
      trusted.protocol === "file:"
      || (trusted.protocol === "http:" && trusted.hostname === "127.0.0.1")
    )
  );
  if (sameRendererDocument) return "local";

  if (normalizedExternalHttpsUrl(targetValue)) return "external-https";

  return "deny";
}
