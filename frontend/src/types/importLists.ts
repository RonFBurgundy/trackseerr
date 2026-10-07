import type { Narrow, Schema } from './apiSchema';
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

export type ProviderField = Narrow<Schema<'ProviderField'>, { type: ProviderFieldType }>;

export type ProviderMeta = Narrow<Schema<'ProviderMeta'>, { fields: ProviderField[] }>;

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

export type ImportListItemCounts = Schema<'ImportListItemCounts'>;

export type ImportList = Narrow<Schema<'ImportList'>, { config: ImportListConfig; item_counts: ImportListItemCounts; tags: string[]; monitor_mode: ListMonitorMode; artist_monitor_option: MonitorOption | null; quality_profile_id: string | null; last_status: ImportListStatus | null; last_error: string | null; last_synced_at: string | null }>;

export type ImportListItemKind = 'artist' | 'album' | 'track';
export type ImportListItemStatus = 'pending' | 'applied' | 'unresolved' | 'skipped' | 'failed';

export type ImportListItemOut = Narrow<Schema<'ImportListItemOut'>, { kind: ImportListItemKind; status: ImportListItemStatus; applied_level?: ListMonitorMode | null }>;

export type ImportListItemsPage = Narrow<Schema<'ImportListItemsPage'>, { items: ImportListItemOut[] }>;

export type ImportListTestResult = Narrow<Schema<'ImportListTestResult'>, { sample: ImportListItemOut[] }>;

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
