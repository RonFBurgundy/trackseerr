/** Phase 3 contract: Activity (queue/history/blocklist) and Wanted (missing/cutoff) list payloads. */

export type LibraryListMode = 'native' | 'lidarr';
export type ListSortDir = 'asc' | 'desc';

/** Envelope shared by every paged list endpoint. */
export interface PagedResponse<T> {
  mode: LibraryListMode;
  page: number;
  page_size: number;
  total: number;
  sort_key: string;
  sort_dir: ListSortDir;
  records: T[];
}

export interface ListQuery {
  page: number;
  pageSize: number;
  sortKey: string;
  sortDir: ListSortDir;
  /** Extra query params, e.g. `{ event: 'grabbed' }`. Empty values are omitted. */
  filters?: Readonly<Record<string, string>>;
}

export type ActivitySource = 'native' | 'lidarr';

export interface ActivityQueueRecord {
  id: string | number;
  source: ActivitySource;
  artist: string | null;
  album: string | null;
  title: string | null;
  item_type: string | null;
  quality: string | null;
  protocol: string | null;
  indexer: string | null;
  client: string | null;
  status: string;
  /** 0-1 fraction. */
  progress: number;
  size_bytes: number | null;
  sizeleft_bytes: number | null;
  eta_seconds: number | null;
  added_at: string | null;
  stalled: boolean;
  stalled_reason: string | null;
  messages: string[];
  request_id: string | null;
  /** Download-client id the files live under; null when unknown. */
  download_id: string | null;
  /** The download finished but some files could not be matched; offer Manual import. */
  needs_manual_import: boolean;
  unmatched_count: number;
  /** Seeding progress for a completed torrent still held for seeding; null otherwise. */
  seeding?: ActivitySeeding | null;
}

export interface ActivitySeeding {
  ratio: number;
  ratio_target: number | null;
  seeding_minutes: number;
  time_target_minutes: number | null;
  /** Minutes until seed cleanup acts on this torrent; null when no goal/ETA applies. */
  removes_in_minutes?: number | null;
  action?: 'keep' | 'remove' | 'remove_and_delete';
}

export type ActivityHistoryEvent =
  | 'grabbed'
  | 'imported'
  | 'failed'
  | 'deleted'
  | 'blocklisted'
  | 'upgraded';

export interface ActivityHistoryRecord {
  id: string | number;
  source: ActivitySource;
  event: ActivityHistoryEvent;
  artist: string | null;
  album: string | null;
  title: string | null;
  quality: string | null;
  indexer: string | null;
  client: string | null;
  date: string | null;
  message: string | null;
  /** True only for a grab that has not already failed / been blocklisted (the server enforces it with a 409). */
  can_mark_failed: boolean;
}

export interface ActivityBlocklistRecord {
  id: string | number;
  source: ActivitySource;
  artist: string | null;
  album: string | null;
  title: string | null;
  release_title: string | null;
  quality: string | null;
  indexer: string | null;
  protocol: string | null;
  reason: string | null;
  date: string | null;
}

export interface WantedRecord {
  id: string | number;
  source: ActivitySource;
  artist: string | null;
  album: string | null;
  title: string | null;
  item_type: 'track' | 'album';
  /** Album the item belongs to; populated in native mode, null for Lidarr. */
  album_id: string | null;
  release_date: string | null;
  monitored: boolean;
  last_searched_at: string | null;
}

export interface WantedCutoffRecord extends WantedRecord {
  current_quality: string | null;
  cutoff_quality: string | null;
}

export type WantedListName = 'missing' | 'cutoff';

export type WantedSearchRequest =
  | { ids: Array<string | number> }
  | { all: true; list: WantedListName };

export interface WantedSearchResponse {
  queued: number;
  /** Set when the server skipped or refused part of the request (batch already running, recently searched). */
  message?: string;
}

/** Body of every mutating Activity action. `success: false` is a handled refusal (HTTP 200), not an exception. */
export interface ActivityActionResult {
  success: boolean;
  message: string;
}

export interface QueueRemoveOptions {
  removeFromClient: boolean;
  blocklist: boolean;
}

/** Phase 4: one group of a list's index (an alphabet letter, a year, a size bucket). */
export interface GroupIndexGroup {
  label: string;
  /** Absolute index of the group's first row in the list's current ordering. */
  offset: number;
  count: number;
}

/** Response of every `<list endpoint>/index`. */
export interface GroupIndexResponse {
  sort_key: string;
  sort_dir: ListSortDir;
  total: number;
  groups: GroupIndexGroup[];
}

/** Same sort and filters as the list the index belongs to. */
export interface IndexQuery {
  sortKey: string;
  sortDir: ListSortDir;
  filters?: Readonly<Record<string, string>>;
}

export type IndexFetcher = (q: IndexQuery, signal?: AbortSignal) => Promise<GroupIndexResponse>;
