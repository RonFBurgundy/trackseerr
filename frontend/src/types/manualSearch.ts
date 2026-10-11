import type { Schema } from './apiSchema';

export type SearchRelease = Schema<'SearchRelease'>;

/** What a manual search is looking for. A null target means the modal is closed. */
export interface ManualSearchTarget {
  artist: string;
  album?: string | null;
  title?: string | null;
  itemType: 'album' | 'track';
  albumId?: string | number | null;
  trackId?: string | number | null;
}

export type ManualSearchFilter = 'all' | 'accepted' | 'rejected';
export type ManualSearchSort = 'default' | 'score' | 'size' | 'seeders' | 'age';

export interface IndexerSearchError {
  source: string;
  error: string;
}
