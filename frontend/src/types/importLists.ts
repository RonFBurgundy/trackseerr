import type { MonitorOption } from './monitoring';

/** How far a list item is escalated when applied. Shared by import lists and synced playlists. */
export type ListMonitorMode = 'track' | 'album' | 'artist' | 'none';

export const LIST_MONITOR_MODES: ReadonlyArray<ListMonitorMode> = ['track', 'album', 'artist', 'none'];

export const LIST_MONITOR_MODE_LABELS: Readonly<Record<ListMonitorMode, string>> = {
  track: 'Track only',
  album: 'Album',
  artist: 'Artist',
  none: 'None',
};

export const LIST_MONITOR_MODE_HELP: Readonly<Record<ListMonitorMode, string>> = {
  track: 'Request just the songs on the list',
  album: "Monitor each song's whole album",
  artist: 'Add the artist with the chosen monitor option',
  none: 'Match only, never search',
};

export type ProviderFieldType = 'text' | 'secret' | 'select' | 'number';

export interface ProviderField {
  key: string;
  label: string;
  type: ProviderFieldType;
  required: boolean;
  options?: string[];
}

export interface ProviderMeta {
  provider: string;
  label: string;
  sources: string[];
  fields: ProviderField[];
}

export type ImportListConfig = Record<string, string | number>;

export interface ImportListInput {
  name: string;
  provider: string;
  config: ImportListConfig;
  enabled: boolean;
  monitor_mode: ListMonitorMode;
  artist_monitor_option: MonitorOption | null;
  quality_profile_id: string | null;
  /** Minimum 60. */
  sync_interval_minutes: number;
  /** Tag labels applied to every artist this list adds (native library). */
  tags: string[];
}

export type ImportListStatus = 'ok' | 'error';

export interface ImportListItemCounts {
  applied: number;
  pending: number;
  unresolved: number;
  failed: number;
  skipped: number;
}

export interface ImportList extends ImportListInput {
  id: string;
  last_synced_at: string | null;
  last_status: ImportListStatus | null;
  last_error: string | null;
  item_counts: ImportListItemCounts;
  created_at: string;
  updated_at: string;
}

export type ImportListItemKind = 'artist' | 'album' | 'track';
export type ImportListItemStatus = 'pending' | 'applied' | 'unresolved' | 'skipped' | 'failed';

export interface ImportListItemOut {
  /** 0 for test-sample rows, which are not stored. */
  id: number;
  kind: ImportListItemKind;
  mbid: string | null;
  artist_name: string;
  album_title: string;
  track_title: string;
  status: ImportListItemStatus;
  applied_level: ListMonitorMode | null;
  error: string | null;
  /** Null for test-sample rows. */
  first_seen_at: string | null;
  last_seen_at: string | null;
}

export interface ImportListItemsPage {
  items: ImportListItemOut[];
  total: number;
}

export interface ImportListTestResult {
  ok: boolean;
  item_count: number;
  sample: ImportListItemOut[];
  error: string | null;
}

/** Placeholder the server returns for stored secrets; sending it back keeps the stored value. */
export const SECRET_MASK = '********';

export const SYNC_INTERVAL_OPTIONS: ReadonlyArray<{ value: number; label: string }> = [
  { value: 60, label: 'Every hour' },
  { value: 360, label: 'Every 6 hours' },
  { value: 720, label: 'Every 12 hours' },
  { value: 1440, label: 'Every 24 hours' },
  { value: 10080, label: 'Every 7 days' },
];

/** Strips server-managed fields so a stored list can be sent back through create/update/test. */
export function importListToInput(list: ImportList): ImportListInput {
  return {
    name: list.name,
    provider: list.provider,
    config: { ...list.config },
    enabled: list.enabled,
    monitor_mode: list.monitor_mode,
    artist_monitor_option: list.artist_monitor_option,
    quality_profile_id: list.quality_profile_id,
    sync_interval_minutes: list.sync_interval_minutes,
    tags: [...(list.tags ?? [])],
  };
}
