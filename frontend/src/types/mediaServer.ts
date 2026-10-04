/** Media servers Trackseerr can push playlists to; `none` means it only manages the library. */
export type MediaServerType = 'plex' | 'none';

export interface MediaServerCapabilities {
  playlists: boolean;
  users: boolean;
  mixes: boolean;
  library_refresh: boolean;
}

/** GET /api/system/media-server (unauthenticated). */
export interface MediaServerStatus {
  type: MediaServerType;
  connected: boolean;
  capabilities: MediaServerCapabilities;
}
