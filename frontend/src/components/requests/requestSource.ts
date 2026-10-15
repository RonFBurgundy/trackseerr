export const REQUEST_SOURCE_LABELS: Readonly<Record<string, string>> = Object.freeze({
  request: 'Manual',
  playlist: 'Playlist',
  mix: 'Mix',
  wanted: 'Wanted',
  rss: 'RSS',
  manual: 'Admin',
  issue: 'Issue',
  upgrade: 'Upgrade',
});

/**
 * Returns the human-readable display label for a request source.
 * Discography batches display as "Discography".
 * Unknown sources are capitalized from their raw string.
 */
export function formatSourceLabel(source?: string | null, batchKind?: string | null): string {
  if (batchKind === 'discography') {
    return 'Discography';
  }
  if (!source) {
    return 'Manual';
  }
  const key = source.toLowerCase();
  if (key in REQUEST_SOURCE_LABELS) {
    return REQUEST_SOURCE_LABELS[key];
  }
  return source.charAt(0).toUpperCase() + source.slice(1);
}

export interface RequestSourceRecord {
  trigger?: string | null;
  trigger_label?: string | null;
  batch_kind?: string | null;
}

/**
 * Formats the badge text for a request card:
 * source label + `trigger_label` when present (e.g. "Playlist · Road Trip").
 */
export function formatRequestSourceBadge(req: RequestSourceRecord): string {
  const base = formatSourceLabel(req.trigger, req.batch_kind);
  if (req.trigger_label) {
    return `${base} \u00b7 ${req.trigger_label}`;
  }
  return base;
}
