import type { Narrow, Schema } from './apiSchema';
/** Media servers Trackseerr can push playlists to; `none` means it only manages the library. */
export type MediaServerType = 'plex' | 'subsonic' | 'jellyfin' | 'none';

export type MediaServerCapabilities = Schema<'MediaServerCapabilities'>;

export type MediaServerStatus = Narrow<Schema<'MediaServerStatus'>, { type: MediaServerType }>;

/** Servers that can be chosen on the Settings page (Plex is configured through environment variables only). */
export type SettableMediaServerType = 'subsonic' | 'jellyfin' | 'none';

export type MediaServerSettings = Narrow<Schema<'MediaServerSettingsResponse'>, { type: MediaServerType | ''; effective_type: MediaServerType }>;

export interface MediaServerSettingsInput {
  type: SettableMediaServerType;
  url: string;
  username: string;
  password: string;
  api_key: string;
}

export type MediaServerTestResult = Schema<'MediaServerTestResponse'>;
