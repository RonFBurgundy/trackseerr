import type { Schema } from '@/types/apiSchema';
import { apiRequest } from './apiClient';
import { buildIndexUrl, buildListUrl } from './listUrl';
import type { GroupIndexResponse, IndexQuery, ListQuery, PagedResponse } from '@/types/activity';
import type {
  ArtistItem,
  AlbumItem,
  TrackItem,
  CollectionItem,
  LibraryStats,
  ScanStatus,
  LidarrStatus,
} from '@/types/models';
import type {
  BulkAlbumEditRequest,
  BulkAlbumEditResult,
  BulkArtistEditRequest,
  BulkArtistEditResult,
  BulkTrackEditRequest,
  BulkTrackEditResult,
  MonitorOption,
} from '@/types/monitoring';

export async function getLibraryStats(): Promise<LibraryStats> {
  return apiRequest<LibraryStats>('/api/library/stats');
}

export async function getArtists(query?: string, monitoredOnly: boolean = false): Promise<ArtistItem[]> {
  const params = new URLSearchParams();
  if (query) params.set('query', query);
  if (monitoredOnly) params.set('monitored_only', 'true');
  params.set('limit', '500');
  const res = await apiRequest<ArtistItem[]>(`/api/library/artists?${params.toString()}`);
  return res || [];
}

export async function getArtistDetail(artistId: number | string): Promise<ArtistItem> {
  return apiRequest<ArtistItem>(`/api/library/artists/${artistId}`);
}

export async function getAlbums(artistId?: number | string, query?: string, monitoredOnly: boolean = false): Promise<AlbumItem[]> {
  const params = new URLSearchParams();
  if (artistId !== undefined) params.set('artist_id', String(artistId));
  if (query) params.set('query', query);
  if (monitoredOnly) params.set('monitored_only', 'true');
  params.set('limit', '500');
  const res = await apiRequest<AlbumItem[]>(`/api/library/albums?${params.toString()}`);
  return res || [];
}

export async function getAlbumDetail(albumId: number | string): Promise<AlbumItem> {
  return apiRequest<AlbumItem>(`/api/library/albums/${albumId}`);
}

export async function getTracks(albumId?: number | string, artistId?: number | string, query?: string): Promise<TrackItem[]> {
  const params = new URLSearchParams();
  if (albumId !== undefined) params.set('album_id', String(albumId));
  if (artistId !== undefined) params.set('artist_id', String(artistId));
  if (query) params.set('query', query);
  params.set('limit', '500');
  const res = await apiRequest<TrackItem[]>(`/api/library/tracks?${params.toString()}`);
  return res || [];
}

export async function toggleArtistMonitored(artistId: number | string, monitored: boolean): Promise<void> {
  await apiRequest<void>(`/api/library/artists/${artistId}/monitored`, {
    method: 'PUT',
    body: { monitored },
  });
}

export async function toggleAlbumMonitored(albumId: number | string, monitored: boolean): Promise<void> {
  await apiRequest<void>(`/api/library/albums/${albumId}/monitored`, {
    method: 'PUT',
    body: { monitored },
  });
}

export async function toggleTrackMonitored(trackId: number | string, monitored: boolean): Promise<void> {
  await apiRequest<void>(`/api/library/tracks/${trackId}/monitored`, {
    method: 'PUT',
    body: { monitored },
  });
}

export async function refreshArtist(
  artistId: number | string
): Promise<Schema<'ArtistRefreshResponse'>> {
  return apiRequest<Schema<'ArtistRefreshResponse'>>(
    `/api/library/artists/${artistId}/refresh`,
    {
      method: 'POST',
    }
  );
}

export type LidarrSearchResult = Schema<'CommandResponse'>;

/** Lidarr mode only (the route is 409 while TrackSeerr manages the library): queue a search for the artist. */
export async function searchArtist(artistId: number | string): Promise<LidarrSearchResult> {
  return apiRequest<LidarrSearchResult>(`/api/library/artists/${artistId}/search`, { method: 'POST' });
}

/** Lidarr mode only: queue a search for the album. */
export async function searchAlbum(albumId: number | string): Promise<LidarrSearchResult> {
  return apiRequest<LidarrSearchResult>(`/api/library/albums/${albumId}/search`, { method: 'POST' });
}

export async function setArtistMonitoringPreset(
  artistId: number | string,
  option: MonitorOption
): Promise<void> {
  await apiRequest<void>(`/api/library/artists/${artistId}/monitored`, {
    method: 'PUT',
    body: {
      monitored: option !== 'none',
      cascade_children: true,
      monitor_option: option,
    },
  });
}

export async function triggerScan(pruneMissing: boolean = false): Promise<void> {
  await apiRequest<void>('/api/library/scan', {
    method: 'POST',
    body: { prune_missing: pruneMissing },
  });
}

export async function getScanStatus(): Promise<ScanStatus> {
  return apiRequest<ScanStatus>('/api/library/scan/status');
}

export async function cancelScan(): Promise<void> {
  await apiRequest<void>('/api/library/scan/cancel', {
    method: 'POST',
  });
}

export async function getLidarrStatus(): Promise<LidarrStatus> {
  return apiRequest<LidarrStatus>('/api/library/migrate-lidarr/status');
}

export async function startLidarrMigration(autoSwitch: boolean = true): Promise<void> {
  await apiRequest<void>('/api/library/migrate-lidarr', {
    method: 'POST',
    body: { auto_switch: autoSwitch },
  });
}

export async function getCollections(query?: string): Promise<CollectionItem[]> {
  const params = new URLSearchParams();
  if (query) params.set('query', query);
  params.set('limit', '500');
  const res = await apiRequest<CollectionItem[]>(`/api/library/collections?${params.toString()}`);
  return res || [];
}

export async function getCollectionDetail(id: string): Promise<CollectionItem> {
  return apiRequest<CollectionItem>(`/api/library/collections/${id}`);
}

export async function createCollection(data: {
  name: string;
  summary?: string;
  poster_url?: string;
  monitored?: boolean;
}): Promise<CollectionItem> {
  return apiRequest<CollectionItem>('/api/library/collections', {
    method: 'POST',
    body: data,
  });
}

export async function deleteCollection(id: string): Promise<boolean> {
  const res = await apiRequest<Schema<'CollectionDeleteResponse'>>(`/api/library/collections/${id}`, {
    method: 'DELETE',
  });
  return Boolean(res?.success);
}

export async function addAlbumToCollection(
  collectionId: string,
  albumId: string | number,
  orderIndex: number = 0
): Promise<boolean> {
  const res = await apiRequest<Schema<'CollectionAlbumResponse'>>(
    `/api/library/collections/${collectionId}/albums`,
    {
      method: 'POST',
      body: { album_id: String(albumId), order_index: orderIndex },
    }
  );
  return Boolean(res?.success);
}

export async function removeAlbumFromCollection(
  collectionId: string,
  albumId: string | number
): Promise<boolean> {
  const res = await apiRequest<Schema<'CollectionAlbumResponse'>>(
    `/api/library/collections/${collectionId}/albums/${albumId}`,
    {
      method: 'DELETE',
    }
  );
  return Boolean(res?.success);
}


/**
 * Phase 4 paged library endpoints. `q.filters` carries the server-side filters: `q` (search), `monitored_only`
 * (`'true'`), and for albums/tracks `artist_id` / `album_id`. Empty values are omitted.
 * Sort keys: artists `name|added_at|album_count`; albums `title|artist|release_date|added_at`;
 * tracks `title|artist|album|added_at|size_bytes`.
 */
export function getArtistsPaged(q: ListQuery, signal?: AbortSignal): Promise<PagedResponse<ArtistItem>> {
  return apiRequest<PagedResponse<ArtistItem>>(buildListUrl('/api/library/artists/paged', q), { signal });
}

export function getAlbumsPaged(q: ListQuery, signal?: AbortSignal): Promise<PagedResponse<AlbumItem>> {
  return apiRequest<PagedResponse<AlbumItem>>(buildListUrl('/api/library/albums/paged', q), { signal });
}

export function getTracksPaged(q: ListQuery, signal?: AbortSignal): Promise<PagedResponse<TrackItem>> {
  return apiRequest<PagedResponse<TrackItem>>(buildListUrl('/api/library/tracks/paged', q), { signal });
}

export function getArtistsIndex(q: IndexQuery, signal?: AbortSignal): Promise<GroupIndexResponse> {
  return apiRequest<GroupIndexResponse>(buildIndexUrl('/api/library/artists', q), { signal });
}

export function getAlbumsIndex(q: IndexQuery, signal?: AbortSignal): Promise<GroupIndexResponse> {
  return apiRequest<GroupIndexResponse>(buildIndexUrl('/api/library/albums', q), { signal });
}

export function getTracksIndex(q: IndexQuery, signal?: AbortSignal): Promise<GroupIndexResponse> {
  return apiRequest<GroupIndexResponse>(buildIndexUrl('/api/library/tracks', q), { signal });
}

const ALBUM_TRACK_PAGE_SIZE = 200;

const ARTIST_TRACK_FILTER_CAP = 1000;

/** Tracks of one artist whose search text matches (`q` + `artist_id`), up to a safety cap. */
export async function searchArtistTracks(artistId: number | string, text: string, signal?: AbortSignal): Promise<TrackItem[]> {
  const tracks: TrackItem[] = [];
  for (let page = 1; tracks.length < ARTIST_TRACK_FILTER_CAP; page += 1) {
    const res = await getTracksPaged(
      { page, pageSize: ALBUM_TRACK_PAGE_SIZE, sortKey: 'title', sortDir: 'asc', filters: { artist_id: String(artistId), q: text } },
      signal
    );
    tracks.push(...res.records);
    if (res.records.length === 0 || tracks.length >= res.total) break;
  }
  return tracks;
}

/** Every track of one album via the paged endpoint (`album_id` filter), in disc and track order. */
export async function getAlbumTracksPaged(albumId: number | string, signal?: AbortSignal): Promise<TrackItem[]> {
  const tracks: TrackItem[] = [];
  for (let page = 1; ; page += 1) {
    const res = await getTracksPaged(
      {
        page,
        pageSize: ALBUM_TRACK_PAGE_SIZE,
        sortKey: 'title',
        sortDir: 'asc',
        filters: { album_id: String(albumId) },
      },
      signal
    );
    tracks.push(...res.records);
    if (res.records.length === 0 || tracks.length >= res.total) break;
  }
  return tracks.sort(
    (a, b) => (a.disc_number ?? 1) - (b.disc_number ?? 1) || (a.track_number ?? 0) - (b.track_number ?? 0)
  );
}

export async function bulkEditArtists(body: BulkArtistEditRequest): Promise<BulkArtistEditResult> {
  return apiRequest<BulkArtistEditResult>('/api/library/artists/bulk-edit', { method: 'POST', body });
}

export async function bulkEditAlbums(body: BulkAlbumEditRequest): Promise<BulkAlbumEditResult> {
  return apiRequest<BulkAlbumEditResult>('/api/library/albums/bulk-edit', { method: 'POST', body });
}

/** Native mode only: Lidarr cannot monitor tracks independently, so the route answers 409 there. */
export async function bulkEditTracks(body: BulkTrackEditRequest): Promise<BulkTrackEditResult> {
  return apiRequest<BulkTrackEditResult>('/api/library/tracks/bulk-edit', { method: 'POST', body });
}

export interface IngestArtistInput {
  foreign_artist_id: string;
  artist_name: string;
  monitor_option: MonitorOption;
  monitored?: boolean;
  quality_profile_id?: string | null;
  root_folder?: string | null;
  /** Omitted: the saved default for added artists; an explicit null means no profile. */
  metadata_profile_id?: number | null;
}

/** Ingest an artist discography from discovery metadata into the native catalog. */
export async function ingestArtist(input: IngestArtistInput): Promise<void> {
  await apiRequest<void>('/api/library/artists/ingest', { method: 'POST', body: input });
}

export type LibraryFacetsResponse = Schema<'LibraryFacetsResponse'>;

export async function getLibraryFacets(signal?: AbortSignal): Promise<Schema<'LibraryFacetsResponse'>> {
  return apiRequest<Schema<'LibraryFacetsResponse'>>('/api/library/facets', { signal });
}
