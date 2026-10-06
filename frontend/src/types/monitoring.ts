/** Native-library monitor options (Lidarr's own set is `LidarrMonitorOption`; it is intentionally separate). */
export type MonitorOption = 'all' | 'albums' | 'singles_eps' | 'existing' | 'future' | 'none';

export const MONITOR_OPTION_LABELS: Readonly<Record<MonitorOption, string>> = {
  all: 'All albums',
  albums: 'Albums only',
  singles_eps: 'Singles & EPs only',
  existing: 'Existing tracks',
  future: 'Future releases only',
  none: 'None',
};

/** One-line explanation shown under the select. */
export const MONITOR_OPTION_HINTS: Readonly<Record<MonitorOption, string>> = {
  all: 'Monitor every album and track of the artist.',
  albums: 'Monitor studio albums only.',
  singles_eps: 'Monitor singles and EPs only.',
  existing: 'Monitor only the tracks you already have files for. Other tracks and albums stay unmonitored.',
  future: 'Monitor only releases dated after the artist was added.',
  none: 'Monitor nothing.',
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
  /** Native only: metadata profile id; an explicit null clears it, omitted leaves it alone. */
  metadata_profile_id?: number | null;
  apply_monitor_to_albums?: boolean;
  /** Native only: tag ids to add to / remove from every targeted artist (409 in Lidarr mode). */
  add_tags?: number[];
  remove_tags?: number[];
}

export interface BulkArtistEditResult {
  artists_updated: number;
  /** Artist-tag links created / removed; absent from older servers. */
  tags_added?: number;
  tags_removed?: number;
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
