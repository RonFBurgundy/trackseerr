import type { Narrow, Schema } from './apiSchema';
/** Media servers Trackseerr can push playlists to; `none` means it only manages the library. */
export type MediaServerType = 'plex' | 'subsonic' | 'jellyfin' | 'none';

export type MediaServerCapabilities = Schema<'MediaServerCapabilities'>;

export type MediaServerStatus = Narrow<Schema<'MediaServerStatus'>, { type: MediaServerType }>;

export type MediaServerTestResult = Schema<'MediaServerTestResult'>;

export type PlexAccountStatusResponse = Schema<'PlexAccountStatusResponse'>;

export type PlexPinResponse = Schema<'PlexPinResponse'>;

export type PlexAccountCheckResponse = Schema<'PlexAccountCheckResponse'>;

export type PlexAccountDisconnectResponse = Schema<'PlexAccountDisconnectResponse'>;

export type MediaServerKind = 'plex' | 'subsonic' | 'jellyfin';

export type MediaServerItem = Schema<'MediaServerItem'>;

export type MediaServerPayload = Schema<'MediaServerPayload'>;

export type MediaServerDeletedResponse = Schema<'MediaServerDeletedResponse'>;

export type MediaServerLoginResponse = Schema<'MediaServerLoginResponse'>;

export type ServerPlaylistSummary = Schema<'ServerPlaylistSummary'>;

export type ServerPlaylistTrack = Schema<'ServerPlaylistTrack'>;

export type ServerLinkedUser = Schema<'ServerLinkedUser'>;

export type MediaServerLinksGroup = Schema<'MediaServerLinksGroup'>;

export type ServerUserLinkPayload = Schema<'ServerUserLinkPayload'>;

export type ServerUserLinkResponse = Schema<'ServerUserLinkResponse'>;

export type ServerUserLinkDeletedResponse = Schema<'ServerUserLinkDeletedResponse'>;
