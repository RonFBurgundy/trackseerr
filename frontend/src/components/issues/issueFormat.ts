/**
 * Parse server timestamps which may arrive in two formats:
 * - SQLite: "2026-10-06 12:34:56" (UTC, space separator, no zone)
 * - Python isoformat: "2026-10-06T12:34:56+00:00" or "2026-10-06T12:34:56"
 *
 * Chrome parses space-separated dates as local time; Safari/iOS parses as NaN.
 * This function normalizes to ISO 8601 with Z suffix before parsing.
 *
 * @returns milliseconds since epoch, or null if input is null/undefined/invalid
 */
export function parseServerTimestamp(
  value: string | null | undefined
): number | null {
  if (!value) return null;

  const trimmed = value.trim();
  if (!trimmed) return null;

  // Replace space between date and time with T (SQLite format: "2026-10-06 12:34:56")
  const normalized = trimmed.replace(/^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})/, '$1T$2');

  // Check if there's a zone designator (Z or ±HH:MM or ±HHMM at the end)
  const hasZone = /[Z+-](?:\d{2}:?\d{2})?$/.test(normalized);

  // Append Z if no zone designator (assumes UTC)
  const isoString = hasZone ? normalized : `${normalized}Z`;

  const parsed = Date.parse(isoString);
  return Number.isNaN(parsed) ? null : parsed;
}

/** Compact relative time ("5m ago", "3d ago"); falls back to a date past a month or "—" if invalid. */
export function relativeTime(iso: string | null | undefined, now: number = Date.now()): string {
  const then = parseServerTimestamp(iso);
  if (then === null) return '—';
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(then).toLocaleDateString();
}
