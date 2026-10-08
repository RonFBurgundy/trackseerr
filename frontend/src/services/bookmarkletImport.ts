/**
 * Bookmarklet 1-Click Helper and URL Import validation / persistence.
 */

export const ALLOWED_IMPORT_HOSTS = new Set([
  'open.spotify.com',
  'spotify.link',
  'www.deezer.com',
  'deezer.com',
  'link.deezer.com',
]);

const STORAGE_KEY = 'trackseerr:pendingImport';

export interface PendingImport {
  url?: string;
  error?: string;
}

/**
 * Validates that an import URL uses http(s) and belongs to an allowed streaming host.
 */
export function validateImportUrl(rawUrl: string): PendingImport {
  try {
    const parsed = new URL(rawUrl.trim());
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return { error: 'Invalid URL protocol. Only http(s) links are supported.' };
    }
    const host = parsed.hostname.toLowerCase();
    if (!ALLOWED_IMPORT_HOSTS.has(host)) {
      return {
        error: `Unsupported domain "${host}". Only Spotify and Deezer playlist URLs are supported.`,
      };
    }
    return { url: parsed.toString() };
  } catch {
    return { error: 'Invalid URL provided in import link.' };
  }
}

/**
 * Stores a pending import intent into sessionStorage.
 */
export function storePendingImport(pending: PendingImport): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(pending));
  } catch {
    // sessionStorage unavailable (e.g. private browsing with strict storage blocking)
  }
}

/**
 * Retrieves and clears the pending import intent from sessionStorage.
 */
export function consumePendingImport(): PendingImport | null {
  try {
    const item = window.sessionStorage.getItem(STORAGE_KEY);
    if (item) {
      window.sessionStorage.removeItem(STORAGE_KEY);
      return JSON.parse(item) as PendingImport;
    }
  } catch {
    // ignore
  }
  return null;
}

/**
 * Checks whether a pending import intent exists without clearing it.
 */
export function peekPendingImport(): PendingImport | null {
  try {
    const item = window.sessionStorage.getItem(STORAGE_KEY);
    if (item) {
      return JSON.parse(item) as PendingImport;
    }
  } catch {
    // ignore
  }
  return null;
}

/**
 * Parses an `#import?url=...` or `#/import?url=...` hash string.
 */
export function parseImportHash(hash: string): PendingImport | null {
  const trimmed = hash.trim();
  if (!trimmed.startsWith('#import') && !trimmed.startsWith('#/import')) {
    return null;
  }
  const qIndex = trimmed.indexOf('?');
  if (qIndex === -1) {
    return { error: 'No playlist URL provided in import hash.' };
  }
  const queryPart = trimmed.slice(qIndex + 1);
  const params = new URLSearchParams(queryPart);
  const rawUrl = params.get('url');
  if (!rawUrl) {
    return { error: 'No playlist URL provided in import hash.' };
  }
  return validateImportUrl(rawUrl);
}

/**
 * Builds the javascript: bookmarklet string.
 */
export function buildBookmarkletCode(origin?: string): string {
  const base = origin || (typeof window !== 'undefined' ? window.location.origin : '');
  return `javascript:(function(){window.open('${base}/#import?url='+encodeURIComponent(location.href));})();`;
}
