/** Media servers Trackseerr can push playlists to; `none` means it only manages the library. */
export type MediaServerType = 'plex' | 'subsonic' | 'none';

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

/** Servers that can be chosen on the Settings page (Plex is configured through environment variables only). */
export type SettableMediaServerType = 'subsonic' | 'none';

/** GET /api/settings/media-server. Secrets come back as `********` when set; send that back to keep them. */
export interface MediaServerSettings {
  type: MediaServerType | '';
  url: string;
  username: string;
  password: string;
  api_key: string;
  /** The server actually in use (the environment can override what is saved here). */
  effective_type: MediaServerType;
  /** True when environment variables configure the media server: the form is read-only. */
  locked_by_env: boolean;
}

export interface MediaServerSettingsInput {
  type: SettableMediaServerType;
  url: string;
  username: string;
  password: string;
  api_key: string;
}

export interface MediaServerTestResult {
  ok: boolean;
  message: string;
}
