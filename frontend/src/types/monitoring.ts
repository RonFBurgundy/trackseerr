/** Native-library monitor options (Lidarr's own set is `LidarrMonitorOption`; it is intentionally separate). */
export type MonitorOption = 'all' | 'albums' | 'singles_eps' | 'existing' | 'future' | 'none';

export const MONITOR_OPTION_LABELS: Readonly<Record<MonitorOption, string>> = {
  all: 'All albums',
  albums: 'Albums only',
  singles_eps: 'Singles & EPs only',
  existing: 'Existing albums only',
  future: 'Future releases only',
  none: 'None',
};

export const MONITOR_OPTIONS: ReadonlyArray<{ value: MonitorOption; label: string }> = (
  Object.keys(MONITOR_OPTION_LABELS) as MonitorOption[]
).map((value) => ({ value, label: MONITOR_OPTION_LABELS[value] }));

/** Body of `POST /api/library/artists/bulk-edit`; send exactly one of a non-empty `artist_ids` or `all: true`. */
export interface BulkArtistEditRequest {
  artist_ids?: string[];
  all?: boolean;
  monitored?: boolean;
  monitor_option?: MonitorOption;
  quality_profile_id?: string | null;
  apply_monitor_to_albums?: boolean;
}

export interface BulkArtistEditResult {
  artists_updated: number;
  albums_monitored: number;
  albums_unmonitored: number;
}

/** Body of `POST /api/library/albums/bulk-edit`. */
export interface BulkAlbumEditRequest {
  album_ids: string[];
  monitored: boolean;
}

export interface BulkAlbumEditResult {
  albums_updated: number;
}

/** Body of `POST /api/library/tracks/bulk-edit` (native mode only; 409 while Lidarr manages the library). */
export interface BulkTrackEditRequest {
  track_ids: string[];
  monitored: boolean;
}

export interface BulkTrackEditResult {
  tracks_updated: number;
}
