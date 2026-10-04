export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${parseFloat((bytes / Math.pow(1024, i)).toFixed(1))} ${units[i]}`;
}

export function formatEta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return '-';
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '-';
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/** `YYYY`, `YYYY-MM`, `YYYY-MM-DD`, optionally with a midnight-UTC time part (`T00:00:00Z`, as Lidarr sends release dates). */
const CALENDAR_DATE_RE = /^(\d{4})(?:-(\d{2})(?:-(\d{2})(?:T00:00:00(?:\.0+)?(?:Z|[+-]00:?00)?)?)?)?$/;

export interface CalendarDateParts {
  year: number;
  month: number | null;
  day: number | null;
}

/** Parses a calendar-date string into its components without any timezone arithmetic; null when it is not one. */
export function parseCalendarDate(value: string): CalendarDateParts | null {
  const m = CALENDAR_DATE_RE.exec(value.trim());
  if (!m) return null;
  const year = Number(m[1]);
  const month = m[2] !== undefined ? Number(m[2]) : null;
  const day = m[3] !== undefined ? Number(m[3]) : null;
  if (month !== null && (month < 1 || month > 12)) return null;
  if (day !== null && month !== null) {
    const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
    if (day < 1 || day > daysInMonth) return null;
  }
  return { year, month, day };
}

/**
 * A release date shown as the calendar date it names, in every timezone: a bare year renders as the year,
 * year-month as `Oct 2026`, a full date as `Oct 4, 2026`. A value that is really a timestamp falls back to
 * `formatDate` (local time); anything unparseable is returned as-is.
 */
export function formatCalendarDate(value: string | null | undefined): string {
  if (!value) return '-';
  const parts = parseCalendarDate(value);
  if (parts === null) return formatDate(value);
  if (parts.month === null) return String(parts.year);
  // Format the components as UTC so the viewer's offset can never move the day.
  const d = new Date(0);
  d.setUTCFullYear(parts.year, parts.month - 1, parts.day ?? 1);
  return d.toLocaleDateString(
    undefined,
    parts.day === null
      ? { timeZone: 'UTC', year: 'numeric', month: 'short' }
      : { timeZone: 'UTC', year: 'numeric', month: 'short', day: 'numeric' }
  );
}

/** A timestamp rendered as a date in the viewer's local timezone. Use `formatCalendarDate` for release dates. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return '-';
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

/** Em-dash placeholder for nullable text cells. */
export function orDash(value: string | null | undefined): string {
  return value && value.trim() ? value : '-';
}
