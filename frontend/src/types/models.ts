import type { MonitorOption } from './monitoring';
import type { ListMonitorMode } from './importLists';
/**
 * Domain & UI models for TrackSeerr React SPA.
 * Strictly typed with zero `any` / `as any`.
 */

export type DeploymentTier = 'gateway' | 'core' | 'all-in-one';

export interface User {
  id: number | string;
  plex_username: string;
  /** Present for local accounts (and mirrored for Plex users on newer backends). */
  username?: string;
  auth_type?: 'plex' | 'local';
  plex_id?: string;
  email?: string;
  thumb?: string;
  is_admin: boolean;
  is_active: boolean;
  quota_limit?: number;
  quota_period_days?: number;
  requests_remaining?: number;
  tier?: DeploymentTier;
}

export interface UserQuota {
  remaining: number;
  limit: number;
  period_days: number;
}

export interface AuthPinResponse {
  id: number;
  code: string;
  auth_url: string;
  expires_in: number;
}

export interface AuthVerifyResponse {
  token: string;
  user: User;
}

export interface DiscoveryItem {
  id: string;
  title: string;
  artist: string;
  album?: string;
  cover_url?: string;
  release_date?: string;
  type: 'album' | 'track' | 'artist';
  preview_url?: string;
  popularity?: number;
  source?: string;
  requested?: boolean;
  in_library?: boolean;
  status?: 'in_library' | 'available' | 'requested' | 'pending' | 'processing' | 'rejected' | 'none';
}

export interface RequestItem {
  id: number;
  title: string;
  artist: string;
  album?: string;
  status: 'pending' | 'approved' | 'processing' | 'rejected' | 'fulfilled' | 'available';
  requested_by_id?: number;
  /** Owner id as returned by the backend DB row (plex id stored as TEXT, so usually a string). */
  user_id?: string | number;
  requested_by_username?: string;
  created_at: string;
  cover_url?: string;
  type?: string;
  quality_profile?: string;
  /** Machine-readable reason a processing request is stuck, e.g. `not_in_metadata_profile`. */
  status_reason?: string | null;
  /** Human-readable explanation of `status_reason`. */
  status_message?: string | null;
}

export interface Playlist {
  id: number;
  name: string;
  source_url: string;
  source_type: 'spotify' | 'deezer' | 'm3u' | 'csv';
  target_user_ids: number[];
  is_active: boolean;
  monitor_mode?: ListMonitorMode;
  last_synced?: string;
  track_count?: number;
  matched_count?: number;
}

export interface MissingTrack {
  id: number;
  playlist_id: number;
  playlist_name?: string;
  title: string;
  artist: string;
  album?: string;
  created_at: string;
}

export interface CollectionItem {
  id: string;
  name: string;
  clean_name?: string;
  summary?: string;
  poster_url?: string;
  monitored: boolean;
  foreign_id?: string;
  album_count?: number;
  preview_covers?: string[];
  albums?: AlbumItem[];
  created_at?: string;
  updated_at?: string;
}

export interface ArtistItem {
  id: number | string;
  name: string;
  monitored: boolean;
  overview?: string;
  artist_type?: string;
  disambiguation?: string;
  genres?: string[] | string;
  images?: Array<{ cover_type: string; url: string }>;
  /** Null when the record has no artwork (Lidarr mode); never a broken URL. */
  image_url?: string | null;
  banner_url?: string | null;
  bio?: string | null;
  country?: string | null;
  mbid?: string | null;
  album_count?: number;
  track_count?: number;
  track_file_count?: number;
  size_bytes?: number;
  path?: string | null;
  status?: string | null;
  added_at?: string | null;
  /** Native artist detail only: the artist's metadata profile id, null when none. */
  metadata_profile_id?: number | null;
  /** `lidarr` for records served live from Lidarr; absent or `native` otherwise. */
  source?: 'native' | 'lidarr';
}

export interface AlbumItem {
  id: number | string;
  artist_id: number | string;
  artist_name?: string;
  title: string;
  monitored: boolean;
  release_date?: string | null;
  year?: number | null;
  album_type?: string | null;
  genres?: string[];
  images?: Array<{ cover_type: string; url: string }>;
  cover_url?: string | null;
  track_count?: number;
  total_tracks?: number;
  track_file_count?: number;
  size_bytes?: number;
  mbid?: string | null;
  added_at?: string | null;
  mb_release_group_id?: string;
  mb_release_id?: string;
  /** MusicBrainz secondary types; null/absent = unknown (treated as studio). */
  secondary_types?: string[] | null;
  /** Native artist detail: false when outside the artist's metadata profile (informational only; never hidden). */
  in_profile?: boolean;
  source?: 'native' | 'lidarr';
}

export interface TrackItem {
  id: number | string;
  album_id: number | string;
  artist_id: number | string;
  title: string;
  /** Joined by the track list endpoints. */
  artist_name?: string;
  album_title?: string;
  added_at?: string;
  track_number?: number;
  disc_number?: number;
  /** Both list backends send `duration_seconds`; `duration_ms` is kept for older payloads. */
  duration_seconds?: number | null;
  duration_ms?: number;
  /** `null` when the backend cannot tell (Lidarr mode): show no monitor indicator rather than "Unmonitored". */
  monitored: boolean | null;
  source?: 'native' | 'lidarr';
  file_path?: string;
  has_file?: boolean;
  preview_url?: string;
  quality?: string;
  file?: {
    id?: string;
    file_path?: string;
    format?: string;
    bitrate?: number;
    sample_rate?: number;
    bits_per_sample?: number;
    size_bytes?: number;
    cutoff_met?: boolean;
    /** Lidarr mode: the quality name Lidarr reports for the file (e.g. "FLAC", "MP3-320"). */
    quality?: string;
  } | null;
}

export interface LibraryStats {
  artist_count: number;
  album_count: number;
  track_count: number;
  monitored_artist_count?: number;
  /** 'lidarr' when the numbers are Lidarr's own statistics, 'native' for the TrackSeerr catalog. */
  source?: 'lidarr' | 'native';
  unmonitored_artist_count?: number;
  /** Null in native mode (native artists carry no status). */
  continuing_artist_count?: number | null;
  ended_artist_count?: number | null;
  /** Tracks on every edition (Lidarr totalTrackCount); `track_count` is Lidarr's trackCount (monitored releases). */
  total_track_count?: number;
  track_file_count?: number;
  file_count?: number;
  missing_track_count?: number;
  total_size_bytes?: number;
  monitored_track_count?: number;
  cutoff_unmet_track_count?: number;
}

export interface QueueItem {
  id: string;
  title: string;
  artist?: string;
  album?: string;
  status: string;
  size?: number;
  progress?: number;
  timeleft?: string;
  download_client?: string;
  protocol?: string;
  error_message?: string;
}

export interface BacklogStatus {
  is_running: boolean;
  total_missing: number;
  in_progress: number;
  last_run?: string;
}

export interface DownloadClientItem {
  id: number;
  name: string;
  client_type: 'slskd' | 'sabnzbd' | 'qbittorrent' | 'deluge' | 'transmission';
  host: string;
  port: number;
  use_ssl: boolean;
  is_enabled: boolean;
  priority: number;
}

export interface IndexerItem {
  id: string;
  name: string;
  indexer_type: 'torznab' | 'newznab' | 'soulseek';
  host_url: string;
  /** Masked by the server on read. */
  api_key?: string | null;
  categories: string;
  enabled: boolean;
  priority: number;
  created_at?: string | null;
  updated_at?: string | null;
  /** Seed ratio to reach; null = inherit the global limit, 0 = no requirement. */
  seed_ratio?: number | null;
  /** Seed time in minutes; null = inherit the global limit, 0 = no requirement. */
  seed_time_minutes?: number | null;
  /** Seed time for discography/multi-album grabs, in minutes; null = inherit. */
  discography_seed_time_minutes?: number | null;
  /** Reject releases with fewer seeders; null = inherit, 0 = no requirement. */
  minimum_seeders?: number | null;
}

/** POST /api/settings/indexers body (create when `id` is omitted, otherwise update). */
export interface IndexerPayload {
  id?: string;
  name: string;
  indexer_type: IndexerItem['indexer_type'];
  host_url: string;
  api_key?: string | null;
  categories?: string;
  enabled?: boolean;
  priority?: number;
  seed_ratio?: number | null;
  seed_time_minutes?: number | null;
  discography_seed_time_minutes?: number | null;
  minimum_seeders?: number | null;
}

/** POST /api/settings/indexers/test body. */
export interface TestIndexerPayload {
  /** Stored indexer id; lets the server substitute the saved API key when the key is empty or masked. */
  id?: string | null;
  indexer_type: IndexerItem['indexer_type'];
  host_url: string;
  api_key?: string | null;
  categories?: string;
}

export interface SystemStatusInfo {
  version: string;
  database_status: string;
  media_path?: string;
  download_path?: string;
  plex_connected: boolean;
  lidarr_connected: boolean;
  storage?: {
    free_space: number;
    total_space: number;
  };
}

export interface AudioPreviewTrack {
  id: string;
  title: string;
  artist: string;
  cover_url?: string;
  preview_url: string;
}

export interface ScanStatus {
  status: 'idle' | 'running' | 'completed' | 'failed' | 'scanning' | 'cancelled' | 'skipped';
  is_scanning?: boolean;
  total_files_found?: number;
  processed_files?: number;
  files_indexed?: number;
  artists_created?: number;
  albums_created?: number;
  tracks_created?: number;
  files_pruned?: number;
  current_file?: string | null;
  error?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  processed_tracks?: number;
  total_tracks?: number;
  current_path?: string;
}

export interface LidarrStatus {
  is_migrating: boolean;
  progress: number;
  migrated_artists: number;
  total_artists: number;
  status: string;
}

export interface GeneralSettings {
  server_name: string;
  base_url: string;
  port: number;
  plex_url: string;
  plex_token: string;
  lidarr_url?: string;
  lidarr_api_key?: string;
  music_directory?: string;
}

export type ImportBitrateCheck = 'off' | 'warn' | 'reject';

export type SeedCompleteAction = 'keep' | 'remove' | 'remove_and_delete';

export interface MediaManagementSettings {
  artist_folder_format: string;
  album_folder_format: string;
  standard_track_format: string;
  compilation_track_format?: string;
  multi_disc_folder_format?: string;
  /** Lidarr-style: path below the artist folder for multi-disc releases (album folder(s)/file name). */
  multi_disc_track_format?: string;
  root_folder_path: string;
  staging_folder_path: string;
  import_mode: 'move' | 'hardlink' | 'copy';
  /** How a hardlinked torrent file is tagged: write tags into a private copy, or keep the hardlink untouched. */
  torrent_hardlink_tags?: 'copy_and_tag' | 'keep_hardlink';
  write_audio_tags: boolean;
  embed_artwork: boolean;
  /** AcoustID application key; the server may return a masked value (only '*'/bullets) when configured. */
  acoustid_api_key?: string | null;
  /** Look a file up by audio fingerprint when its tags match weakly (server default false). */
  fingerprint_on_weak_match?: boolean;
  save_cover_art_file?: boolean;
  /** What happens to a torrent once its seed goal is met. */
  seed_complete_action?: SeedCompleteAction;
  enable_quality_upgrades?: boolean;
  library_mode?: string;
  colon_replacement_format?: string;
  clean_artist_names?: boolean;
  /** Monitoring applied to artists found by a library scan (server default 'existing'). */
  scan_monitor_option?: MonitorOption;
  /** Monitoring applied to artists added manually (server default 'all'). */
  add_monitor_option?: MonitorOption;
  /** Metadata profile given to artists added manually; null = none. */
  add_metadata_profile_id?: number | null;
  /** Per-track bitrate check on import: warn (default) records out-of-range files, reject fails the import. */
  import_bitrate_check?: ImportBitrateCheck;
  /** Global seed ratio limit; null/absent = none. */
  seed_ratio_limit?: number | null;
  /** Global seed time limit in minutes; null/absent = none. */
  seed_time_limit_minutes?: number | null;
  /** True when import_mode is move and an enabled torrent indexer has a seed rule (server-computed). */
  seed_rule_conflict?: boolean;
}

export interface LidarrSettings {
  url?: string;
  api_key?: string;
  /** @deprecated Legacy mirror of `search_on_add`; read and write `search_on_add` instead. */
  auto_search?: boolean;
  root_folder?: string;
  trickle_rate_seconds: number;
  trickle_batch_size: number;
  auto_trickle: boolean;
  auto_trickle_interval_minutes?: number;
  /** Search immediately after add (stored in the legacy `auto_search` column server-side). */
  search_on_add?: boolean;
  /** Song requests monitor the single (when one exists) instead of the album it appears on. Server default true. */
  prefer_singles?: boolean;
}

export type LibraryManagerMode = 'native' | 'lidarr';

export interface LibraryManagerState {
  mode: LibraryManagerMode;
  lidarr_configured: boolean;
  native_configured: boolean;
  can_switch: boolean;
  blocking_reason: string | null;
}

export interface LidarrNamedOption {
  id: number;
  name: string;
}

export interface LidarrTagOption {
  id: number;
  label: string;
}

/** The root-folder defaults Trackseerr adds artists with, as Lidarr reports them. */
export interface LidarrDefaults {
  root_folder: string;
  quality_profile: LidarrNamedOption;
  metadata_profile: LidarrNamedOption;
  monitor: string;
  new_item_monitor: string;
  tags: LidarrTagOption[];
  /** `fallback` when this Lidarr reports no per-folder defaults (first profiles, monitor all, no tags). */
  source: 'rootfolder' | 'fallback';
  root_folders: string[];
  /** False when the resolved Lidarr metadata profile excludes the "Single" primary album type. */
  singles_enabled: boolean;
}

export type SystemJobState = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';

export interface SystemJob {
  id: string;
  task_id: string;
  name: string;
  state: SystemJobState;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  message: string | null;
}

export interface SystemQueueResponse {
  running: SystemJob[];
  queued: SystemJob[];
  recent: SystemJob[];
}

export interface LidarrHealthItem {
  source: string;
  type: 'ok' | 'notice' | 'warning' | 'error';
  message: string;
  wiki_url?: string | null;
}

export interface LidarrHealth {
  mode: LibraryManagerMode;
  reachable: boolean | null;
  version?: string | null;
  health: LidarrHealthItem[];
}

export interface LidarrTestResult {
  online: boolean;
  version?: string;
  error?: string;
}

export interface SystemEventItem {
  id: number;
  event_type: string;
  severity: 'info' | 'warn' | 'error';
  source: string;
  message: string;
  details?: Record<string, unknown>;
  created_at: string;
}

export interface SystemEventsResponse {
  items: SystemEventItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface SystemLogItem {
  id: string;
  timestamp: string;
  level: 'info' | 'warn' | 'error' | 'debug';
  name: string;
  message: string;
  raw?: string;
}

export interface ScheduledTaskItem {
  id: string;
  name: string;
  description: string;
  interval: string;
  status: 'idle' | 'running' | 'paused' | 'failed';
  last_run_at?: string | null;
  can_trigger: boolean;
  can_cancel: boolean;
}


// ---------------------------------------------------------------------------
// Plex Playlist Control
// ---------------------------------------------------------------------------

export type PlexPlaylistKind = 'regular' | 'smart';
export type PlexPlaylistOwner = 'trackseerr' | 'user' | 'plexamp';

export interface PlexUserOption {
  username: string;
  is_admin_account: boolean;
  is_self: boolean;
}

export interface PlexPlaylistSummary {
  rating_key: string;
  title: string;
  kind: PlexPlaylistKind;
  owner: PlexPlaylistOwner;
  ignored: boolean;
  track_count: number;
  duration_ms: number;
  thumb_url: string | null;
  updated_at: string | null;
  trackseerr_playlist_id: string | null;
  plex_user: string;
}

export interface PlexPlaylistItem {
  playlist_item_id: number;
  rating_key: string;
  title: string;
  artist: string;
  album: string;
  duration_ms: number;
}

export interface PlexMix {
  mix_key: string;
  title: string;
  hub_title: string;
  track_count: number | null;
  thumb_url: string | null;
  snapshot_id: string | null;
}

export interface PlexMixSnapshot {
  id: string;
  plex_user: string;
  mix_key: string;
  mix_title: string;
  playlist_title: string;
  rating_key: string | null;
  auto_refresh: boolean;
  last_refreshed_at: string | null;
}

export interface PlexRenameBody {
  title: string;
}

export interface PlexAddItemsBody {
  track_rating_keys: string[];
}

export interface PlexMoveItemBody {
  after_playlist_item_id: number | null;
}

export interface PlexCopyBody {
  target_users: string[];
  title?: string;
}

export interface PlexCopyResult {
  username: string;
  success: boolean;
  rating_key?: string | null;
  error?: string | null;
  copied_tracks: number;
  omitted_tracks: number;
}

/** TrackSeerr playlist row returned by POST /plex/playlists/{key}/adopt. */
export interface AdoptedPlexPlaylist {
  id: string;
  name: string;
  service: 'plex';
  targets: string[];
  description?: string;
  poster_url?: string;
  enabled?: boolean;
  tracks_json?: string | null;
  creator_id?: string | null;
}

export interface PlexFlagsBody {
  ignored?: boolean;
  owner?: 'user' | 'trackseerr';
}

export interface PlexMixSnapshotBody {
  mix_key: string;
  title?: string;
  auto_refresh: boolean;
}

export interface PlexMixSnapshotUpdateBody {
  auto_refresh: boolean;
}

// ---------------------------------------------------------------------------
// Scrobbling
// ---------------------------------------------------------------------------

export type ListenForwardStatus = 'skipped' | 'pending' | 'sent' | 'failed';
export type ListenSource = 'plex_webhook' | 'plex_history';

export interface ScrobbleConfig {
  user_id: string;
  username: string;
  scrobbling_enabled: boolean;
  lastfm_connected: boolean;
  lastfm_username: string | null;
  listenbrainz_connected: boolean;
  listenbrainz_username: string | null;
  updated_at: string | null;
}

export interface UserListen {
  id: number;
  artist: string;
  title: string;
  album: string | null;
  played_at: string;
  source: ListenSource;
  lastfm_status: ListenForwardStatus;
  listenbrainz_status: ListenForwardStatus;
}

export interface ScrobbleServerConfig {
  lastfm_configured: boolean;
  lastfm_api_key_masked: string;
  lastfm_from_env: boolean;
  plex_history_poll_minutes: number;
}

export interface ScrobbleServerConfigBody {
  lastfm_api_key?: string;
  lastfm_api_secret?: string;
  plex_history_poll_minutes?: number;
}

export interface ScrobbleConfigBody {
  scrobbling_enabled?: boolean;
  listenbrainz_token?: string | null;
  unlink_lastfm?: boolean;
}

export interface AdminScrobbleConfigBody extends ScrobbleConfigBody {
  lastfm_username?: string;
  lastfm_session_key?: string;
}

export interface ScrobbleUrlResponse {
  url: string;
}

// ---------------------------------------------------------------------------
// Tailored mixes
// ---------------------------------------------------------------------------

export type MixType = 'discover_weekly' | 'daily_blend' | 'artist_radio';
export type MixTrackOrigin = 'familiar' | 'discovery';
export type MixTrackStatus = 'available' | 'missing' | 'queued';

export interface MixConfig {
  id: string;
  user_id: string;
  mix_type: MixType;
  name: string;
  seed_artist: string | null;
  track_count: number;
  discovery_ratio: number;
  seed_window_days: number;
  excluded_genres: string[];
  auto_acquire_missing: boolean;
  max_weekly_acquisitions: number;
  quality_profile_id: string | null;
  enabled: boolean;
  last_generated_at: string | null;
}

export interface MixConfigCreateBody {
  mix_type: MixType;
  name: string;
  seed_artist?: string | null;
  track_count?: number;
  discovery_ratio?: number;
  seed_window_days?: number;
  excluded_genres?: string[];
  auto_acquire_missing?: boolean;
  max_weekly_acquisitions?: number;
  quality_profile_id?: string | null;
  enabled?: boolean;
  user_id?: string;
}

export type MixConfigUpdateBody = Partial<Omit<MixConfigCreateBody, 'mix_type' | 'user_id'>>;

export interface MixTrack {
  artist: string;
  title: string;
  album: string | null;
  origin: MixTrackOrigin;
}

export interface MixPreviewResult {
  tracks: MixTrack[];
}

export interface MixResultTrack extends MixTrack {
  status: MixTrackStatus;
}

export interface TailoredMixResult {
  mix_id: string;
  generated_at: string;
  total: number;
  available: number;
  missing: number;
  acquisitions_queued: number;
  quota_remaining: number;
  synced: boolean;
  sync_error: string | null;
  tracks: MixResultTrack[];
}

export interface MixGenerateResponse {
  status: string;
}

// --- Issue reporting ---

export type IssueType =
  | 'audio_quality'
  | 'corrupted_file'
  | 'wrong_release'
  | 'missing_tracks'
  | 'incorrect_tags'
  | 'other';

export type IssueStatus = 'open' | 'in_progress' | 'resolved';

export interface Issue {
  id: number;
  media_title: string;
  artist: string;
  issue_type: IssueType;
  problem_details: string;
  status: IssueStatus;
  user_id: number;
  request_id: string | null;
  username: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface CreateIssuePayload {
  media_title: string;
  artist: string;
  issue_type: IssueType;
  problem_details: string;
  request_id?: string;
}

export const ISSUE_TYPE_LABELS: Record<IssueType, string> = {
  audio_quality: 'Audio quality',
  corrupted_file: 'Corrupted file',
  wrong_release: 'Wrong release',
  missing_tracks: 'Missing tracks',
  incorrect_tags: 'Incorrect tags',
  other: 'Other',
};

export const ISSUE_STATUS_LABELS: Record<IssueStatus, string> = {
  open: 'Open',
  in_progress: 'In progress',
  resolved: 'Resolved',
};

export const ISSUE_MAX_TITLE = 300;
export const ISSUE_MAX_DETAILS = 2000;

/** One release in an artist's discography (GET /api/discovery/artist/{id}). */
export interface ArtistDiscographyAlbum {
  id: string;
  title: string;
  artist: string;
  cover_url?: string;
  release_date?: string;
  record_type?: string;
}

export interface ArtistDetail {
  id?: string;
  name: string;
  image_url?: string;
  albums?: ArtistDiscographyAlbum[];
  singles_eps?: ArtistDiscographyAlbum[];
  compilations?: ArtistDiscographyAlbum[];
}
