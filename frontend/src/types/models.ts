import type { Narrow, Schema } from './apiSchema';
import type { MonitorOption } from './monitoring';
import type { ListMonitorMode } from './importLists';
/**
 * Domain & UI models for TrackSeerr React SPA.
 * Strictly typed with zero `any` / `as any`.
 */

export type DeploymentTier = 'gateway' | 'core' | 'all-in-one';

/** The session user: the backend `UserRecord`, plus the deployment tier that `/api/auth/me` returns beside it. */
export type User = Schema<'UserRecord'> & { tier?: DeploymentTier };

export type QuotaTypeKind = 'tracks' | 'albums' | 'discographies';

export interface QuotaTypeStatus {
  kind: QuotaTypeKind;
  used: number;
  limit: number;
  remaining: number;
}

/** Per-type rolling request quota. `unlimited` users have no `types` entries. */
export interface UserQuota {
  unlimited: boolean;
  period_days: number;
  types: QuotaTypeStatus[];
}

export type AuthPinResponse = Schema<'PlexPinResponse'>;

export type AuthVerifyResponse = Schema<'PlexVerifyResponse'>;

export type DiscoveryItem = Schema<'DiscoveryItem'> & { type: 'album' | 'track' | 'artist' };

export type DiscoveryTrackContributor = Schema<'TrackContributor'>;

export type DiscoveryTrackDetail = Schema<'DiscoveryTrackDetail'>;

export const DISCOVERY_STATUSES = [
  'in_library',
  'available',
  'partial',
  'missing',
  'requested',
  'pending',
  'processing',
  'rejected',
  'none',
] as const;

export type DiscoveryStatus = (typeof DISCOVERY_STATUSES)[number];

export function isDiscoveryStatus(value: string | null | undefined): value is DiscoveryStatus {
  return DISCOVERY_STATUSES.some((s) => s === value);
}

/** A request as `/api/requests` returns it: string `id` / `user_id`, requester name in `username`, free-form `status`. */
export type RequestItem = Schema<'RequestRecord'>;

/** A playlist row as `/api/playlists` returns it (`service`, `enabled`, `targets`, `last_synced_at`, `sync_status`). */
export type Playlist = Narrow<Schema<'PlaylistRecord'>, { monitor_mode: ListMonitorMode }>;

export type MissingTrack = Schema<'MissingTrack'>;

export type CollectionItem = Schema<'LibraryCollectionRecord'>;

export type ArtistItem = Schema<'LibraryArtistRecord'>;

export type AlbumItem = Schema<'LibraryAlbumRecord'>;

export type TrackItem = Schema<'LibraryTrackRecord'>;

export type LibraryStats = Schema<'LibraryStats'>;

export type QueueItem = Schema<'QueueItemResponse'>;

/** Drivers `DownloadDriverType` accepts on the backend. */
export type DownloadDriverType = 'slskd' | 'sabnzbd' | 'qbittorrent' | 'lidarr';

/** A configured download client (credentials arrive masked). `host_url` is one URL including scheme and port. */
export type DownloadClientItem = Narrow<Schema<'DownloadClientItem'>, { driver_type: DownloadDriverType }>;

export type DownloadClientRoots = Schema<'DownloadRootsItem'>;

export type IndexerItem = Schema<'IndexerItem'>;

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

export type SystemStatusInfo = Schema<'SystemStatusResponse'>;

export interface AudioPreviewTrack {
  id: string;
  title: string;
  artist: string;
  cover_url?: string;
  preview_url: string;
}

export type ScanStatus = Schema<'ScanStatus'>;

/** Lidarr migration job state: running counts only, the backend sends no totals to compute a percentage from. */
export type LidarrStatus = Schema<'MigrationStatus'>;

/** The only general setting the backend stores: the external application URL (redirects, invites, notifications). */
export type GeneralSettings = Schema<'GeneralSettingsModel'>;

export type ImportBitrateCheck = 'off' | 'warn' | 'reject';

export type SeedCompleteAction = 'keep' | 'remove' | 'remove_and_delete';

export type MediaManagementSettings = Narrow<Schema<'MediaManagementSettingsModel'>, { add_monitor_option: MonitorOption; scan_monitor_option: MonitorOption; import_bitrate_check: ImportBitrateCheck }> & { seed_rule_conflict?: boolean };

export type RecycleBinEmptyResult = Schema<'RecycleBinEmptyResponse'>;

export type LidarrSettings = Schema<'LidarrSettingsModel'>;

export type LibraryManagerMode = 'native' | 'lidarr';

export type LibraryManagerState = Schema<'LibraryManagerModel'>;

export type LidarrNamedOption = Schema<'LidarrNamedOption'>;

export type LidarrTagOption = Schema<'LidarrTagOption'>;

export type LidarrDefaults = Schema<'LidarrDefaultsResponse'>;

export type LidarrHealthItem = Schema<'LidarrHealthCheck'>;

export type LidarrHealth = Schema<'LidarrHealthResponse'>;

export type LidarrTestResult = Schema<'LidarrTestConnectionResponse'>;

export type SystemLogItem = Schema<'LogEntry'>;

export type LogFileItem = Schema<'LogFileEntry'>;

export type LogSettings = Schema<'LogSettingsResponse'>;

export type LogSettingsUpdate = Schema<'LogSettingsUpdate'>;

export type ScheduledTaskItem = Schema<'ScheduledTaskItem'>;

export type TaskProgress = Schema<'TaskProgress'>;

export type TaskRunItem = Schema<'TaskRunItem'>;

export type SystemActivity = Schema<'ActivityResponse'>;

export type SystemActivityRunning = Schema<'ActivityRunningItem'>;

export type SystemActivityRecent = Schema<'ActivityRecentItem'>;

export type SystemResources = Schema<'ResourcesResponse'>;


// ---------------------------------------------------------------------------
// Plex Playlist Control
// ---------------------------------------------------------------------------

export type PlexPlaylistKind = 'regular' | 'smart';
export type PlexPlaylistOwner = 'trackseerr' | 'user' | 'plexamp';

export type PlexUserOption = Schema<'PlexUser'>;

export type PlexPlaylistSummary = Narrow<Schema<'PlexPlaylistSummary'>, { kind: PlexPlaylistKind; owner: PlexPlaylistOwner }>;

export type PlexPlaylistItem = Schema<'PlexPlaylistItem'>;

export type PlexMix = Schema<'PlexMix'>;

export type PlexMixSnapshot = Schema<'MixSnapshot'>;

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

export type PlexCopyResult = Schema<'PlaylistCopyResult'>;

export type AdoptedPlexPlaylist = Schema<'PlaylistRecord'>;

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

export type ScrobbleConfig = Schema<'ScrobbleConfig'>;

export type UserListen = Narrow<Schema<'Listen'>, { source: ListenSource; lastfm_status: ListenForwardStatus; listenbrainz_status: ListenForwardStatus }>;

export type ScrobbleServerConfig = Schema<'ServerConfig'>;

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

export type ScrobbleUrlResponse = Schema<'WebhookUrl'>;

// ---------------------------------------------------------------------------
// Tailored mixes
// ---------------------------------------------------------------------------

export type MixType = 'discover_weekly' | 'daily_blend' | 'artist_radio';
export type MixTrackOrigin = 'familiar' | 'discovery';
export type MixTrackStatus = 'available' | 'missing' | 'queued';

export type MixConfig = Schema<'MixConfig'>;

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

export type MixTrack = Schema<'MixPreviewTrack'>;

export type MixPreviewResult = Schema<'MixPreviewResponse'>;

export type MixResultTrack = Narrow<Schema<'MixResultTrack'>, { origin: MixTrackOrigin; status: MixTrackStatus }>;

export type TailoredMixResult = Narrow<Schema<'MixResult'>, { tracks: MixResultTrack[] }>;

export type MixGenerateResponse = Schema<'MixGenerateResponse'>;

// --- Issue reporting ---

export type IssueType =
  | 'audio_quality'
  | 'corrupted_file'
  | 'wrong_release'
  | 'missing_tracks'
  | 'incorrect_tags'
  | 'request_stuck'
  | 'other';

export type IssueStatus = 'open' | 'in_progress' | 'resolved' | 'wont_fix';

/** Admin fix actions an issue can offer (`available_actions`). */
export type IssueAction = 'retry_request' | 'research' | 'blocklist_and_research' | 'rematch';

export type IssueItemType = 'album' | 'track';

export type Issue = Narrow<Schema<'IssueResponse'>, { status: IssueStatus; issue_type: IssueType }>;

export type IssueComment = Schema<'CommentResponse'>;

export interface CreateIssuePayload {
  media_title: string;
  artist: string;
  issue_type: IssueType;
  problem_details: string;
  request_id?: string;
  discovery_id?: string;
  item_type?: IssueItemType;
  /** Admin only. */
  album_id?: string;
  track_id?: string;
}

export interface IssueListFilters {
  status?: IssueStatus;
  media_title?: string;
  artist?: string;
}

export type IssueOpenCount = Schema<'IssueCount'>;

/** Body of `rematch`: what the Manual Import modal needs. */
export interface IssueRematchResult {
  scope: { album_id: string };
  album: { id: string; title?: string | null; artist_name?: string | null };
}

export type IssueActionResponse = Narrow<Schema<'ActionResponse'>, { issue: Issue }>;

export const ISSUE_TYPE_LABELS: Record<IssueType, string> = {
  audio_quality: 'Audio quality',
  corrupted_file: 'Corrupted file',
  wrong_release: 'Wrong release',
  missing_tracks: 'Missing tracks',
  incorrect_tags: 'Incorrect tags',
  request_stuck: 'Request is stuck',
  other: 'Other',
};

export const ISSUE_STATUS_LABELS: Record<IssueStatus, string> = {
  open: 'Open',
  in_progress: 'In progress',
  resolved: 'Resolved',
  wont_fix: "Won't fix",
};

export const ISSUE_ACTION_LABELS: Record<IssueAction, string> = {
  retry_request: 'Retry request',
  research: 'Search again',
  blocklist_and_research: 'Blocklist & search again',
  rematch: 'Rematch files',
};

/** Types offered for a report about media we have (Discover, Library, available requests). */
export const MEDIA_ISSUE_TYPES: readonly IssueType[] = [
  'audio_quality',
  'corrupted_file',
  'wrong_release',
  'missing_tracks',
  'incorrect_tags',
  'other',
];

/** Types offered for a report about a request that is not yet fulfilled. */
export const REQUEST_ISSUE_TYPES: readonly IssueType[] = ['request_stuck', 'other'];

export const ISSUE_MAX_TITLE = 300;
export const ISSUE_MAX_DETAILS = 2000;
export const ISSUE_MAX_COMMENT = 2000;

/** Statuses an issue is actively tracked in (a duplicate report is refused while one is active). */
export const ACTIVE_ISSUE_STATUSES: readonly IssueStatus[] = ['open', 'in_progress'];

/** One release in an artist's discography (GET /api/discovery/artist/{id}). */
export interface ArtistDiscographyAlbum {
  id: string;
  title: string;
  artist: string;
  cover_url?: string;
  release_date?: string;
  record_type?: string;
}

export type ArtistLinkConfidence = 'high' | 'medium' | 'low' | 'none';

export type ArtistProfileArtist = Narrow<Schema<'ProfileArtist'>, { link_confidence?: ArtistLinkConfidence | null }>;

export type ArtistProfileLibrary = Schema<'ProfileLibrary'>;

export type ArtistProfileTrack = Narrow<Schema<'ProfileTopTrack'>, { id: string; title: string; status: DiscoveryStatus }>;

export type ArtistProfileAlbum = Narrow<Schema<'ProfileAlbum'>, { id: string; artist: string; status: DiscoveryStatus }>;

export type ArtistProfileLibraryOnly = Narrow<Schema<'ProfileAlbum'>, { status: DiscoveryStatus }>;

export type ArtistProfileDiscography = { albums: ArtistProfileAlbum[]; singles_eps: ArtistProfileAlbum[]; compilations: ArtistProfileAlbum[]; library_only: ArtistProfileLibraryOnly[] };

export type ArtistProfile = Omit<Schema<'ArtistProfileResponse'>, 'top_tracks' | 'discography'> & { top_tracks: ArtistProfileTrack[]; discography: ArtistProfileDiscography };

/** Exactly one id identifies the artist; `libraryArtistId` is admin-only. */
export type ArtistProfileTarget = { discoveryId: string } | { libraryArtistId: string };
