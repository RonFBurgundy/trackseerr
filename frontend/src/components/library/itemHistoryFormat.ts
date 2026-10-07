import type { ItemHistoryEvent, ItemHistoryOrigin } from '@/types/itemHistory';

export type HistoryCategory = 'acquisition' | 'failure' | 'file' | 'monitoring' | 'issues' | 'health';

export const EVENT_LABELS: Readonly<Record<string, string>> = {
  requested: 'Requested',
  request_approved: 'Request approved',
  request_declined: 'Request declined',
  added_to_library: 'Added to library',
  monitored: 'Monitored',
  unmonitored: 'Unmonitored',
  searched: 'Searched',
  grabbed: 'Grabbed',
  download_failed: 'Download failed',
  blocklisted: 'Blocklisted',
  imported: 'Imported',
  upgraded: 'Upgraded',
  file_replaced: 'File replaced',
  file_deleted: 'File deleted',
  file_missing: 'File missing',
  quarantined: 'Quarantined',
  restored: 'Restored',
  issue_opened: 'Issue opened',
  issue_resolved: 'Issue resolved',
  health_finding: 'Health finding',
  renamed: 'Renamed',
  moved: 'Moved',
  removed_from_library: 'Removed from library',
};

const EVENT_CATEGORY: Readonly<Record<string, HistoryCategory>> = {
  requested: 'acquisition',
  request_approved: 'acquisition',
  request_declined: 'failure',
  added_to_library: 'acquisition',
  searched: 'acquisition',
  grabbed: 'acquisition',
  imported: 'acquisition',
  upgraded: 'acquisition',
  download_failed: 'failure',
  blocklisted: 'failure',
  file_replaced: 'file',
  file_deleted: 'file',
  file_missing: 'file',
  quarantined: 'file',
  restored: 'file',
  renamed: 'file',
  moved: 'file',
  removed_from_library: 'file',
  monitored: 'monitoring',
  unmonitored: 'monitoring',
  issue_opened: 'issues',
  issue_resolved: 'issues',
  health_finding: 'health',
};

export function eventLabel(event: string): string {
  return EVENT_LABELS[event] ?? event.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
}

export function eventCategory(event: string): HistoryCategory {
  return EVENT_CATEGORY[event] ?? 'file';
}

interface TriggerSource {
  trigger?: string | null;
  trigger_label?: string | null;
  actor_display: string;
}

/** Human phrase for what set an item off, e.g. "Requested by ron" or "From playlist Daily Mix 1". Null when unknown. */
export function triggerPhrase(src: TriggerSource): string | null {
  const label = src.trigger_label?.trim() || null;
  const actor = src.actor_display.trim() || label || 'system';
  switch (src.trigger) {
    case 'request':
      return `Requested by ${actor}`;
    case 'request_approved':
      return `Approved by ${actor}`;
    case 'retry':
      return 'Retried after a failed download';
    case 'wanted':
      return 'Grabbed by wanted search';
    case 'upgrade':
      return 'Quality upgrade search';
    case 'issue':
      return label ? `Issue replacement (${label})` : 'Issue replacement';
    case 'rss':
      return 'Grabbed by RSS sync';
    case 'playlist':
      return label ? `From playlist ${label}` : 'From a playlist';
    case 'mix':
      return label ? `From mix ${label}` : 'From a tailored mix';
    case 'import_list':
      return label ? `From import list ${label}` : 'From an import list';
    case 'manual':
      return `Added manually by ${actor}`;
    case 'scan':
      return 'Added by library scan';
    case 'manual_import':
      return `Manual import by ${actor}`;
    case 'seed_cleanup':
      return 'Seed cleanup';
    case 'recycle_cleanup':
      return 'Recycle bin cleanup';
    case 'user':
      return `By ${actor}`;
    case 'system':
      return 'By the system';
    default:
      return label ?? (src.trigger ? `By ${src.trigger.replace(/_/g, ' ')}` : null);
  }
}

export function originCaption(origin: ItemHistoryOrigin): string | null {
  const phrase = triggerPhrase(origin);
  if (!phrase) return null;
  const when = shortDate(origin.created_at);
  return when ? `${phrase} · ${when}` : phrase;
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, sameYear ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', year: 'numeric' });
}

export function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function historyRelativeTime(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '';
  const secs = Math.round((now - t) / 1000);
  if (secs < 45) return 'just now';
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.round(days / 365)}y ago`;
}

/** Key/value rows for the collapsible details block; primitives as text, nested values as small JSON. */
export function detailRows(details: ItemHistoryEvent['details']): Array<[string, string]> {
  return Object.entries(details).flatMap(([key, value]): Array<[string, string]> => {
    if (value === null || value === undefined || value === '') return [];
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return [[key, String(value)]];
    const json = JSON.stringify(value);
    return [[key, json.length > 160 ? `${json.slice(0, 157)}...` : json]];
  });
}

/** `details.failure_count` as a positive integer, else null. */
export function failureCount(details: ItemHistoryEvent['details']): number | null {
  const n = details.failure_count;
  return typeof n === 'number' && Number.isFinite(n) && n > 0 ? n : null;
}
