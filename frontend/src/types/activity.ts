import type { Schema } from './apiSchema';

/** Phase 3 contract: Activity (queue/history/blocklist) and Wanted (missing/cutoff) list payloads. */

export type LibraryListMode = 'native' | 'lidarr';
export type ListSortDir = 'asc' | 'desc';

/** Envelope shared by every paged list endpoint. */
export type PagedResponse<T> = Omit<Schema<'ArtistsPage'>, 'records'> & { records: T[] };

export interface ListQuery {
  page: number;
  pageSize: number;
  sortKey: string;
  sortDir: ListSortDir;
  /** Extra query params, e.g. `{ event: 'grabbed' }`. Empty values are omitted. */
  filters?: Readonly<Record<string, string | readonly string[]>>;
}

export type ActivitySource = 'native' | 'lidarr';

export type ActivityQueueRecord = Schema<'QueueRecord'>;

export type ActivitySeeding = Schema<'Seeding'>;

export type ActivityHistoryEvent =
  | 'grabbed'
  | 'imported'
  | 'failed'
  | 'deleted'
  | 'blocklisted'
  | 'upgraded';

export type ActivityHistoryRecord = Schema<'HistoryRecord'>;

export type ActivityBlocklistRecord = Schema<'BlocklistRecord'>;

export type WantedRecord = Omit<Schema<'WantedRecord'>, 'current_quality' | 'cutoff_quality'>;

export type WantedCutoffRecord = Schema<'WantedRecord'>;

export type WantedListName = 'missing' | 'cutoff';

export type WantedSearchRequest =
  | { ids: Array<string | number> }
  | { all: true; list: WantedListName };

export type WantedSearchResponse = Schema<'WantedSearchResponse'>;

/** Body of every mutating Activity action. `success: false` is a handled refusal (HTTP 200), not an exception. */
export type ActivityActionResult = Schema<'ActionResult'>;

export interface QueueRemoveOptions {
  removeFromClient: boolean;
  blocklist: boolean;
}

/** Phase 4: one group of a list's index (an alphabet letter, a year, a size bucket). */
export type GroupIndexGroup = Schema<'IndexGroup'>;

/** Response of every `<list endpoint>/index`. */
export type GroupIndexResponse = Schema<'LibraryIndexResponse'>;

/** Same sort and filters as the list the index belongs to. */
export interface IndexQuery {
  sortKey: string;
  sortDir: ListSortDir;
  filters?: Readonly<Record<string, string | readonly string[]>>;
}

export type IndexFetcher = (q: IndexQuery, signal?: AbortSignal) => Promise<GroupIndexResponse>;
