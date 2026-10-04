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
