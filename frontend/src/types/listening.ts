import type { Schema } from './apiSchema';

/** Playlists built from a user's own Last.fm / ListenBrainz listening. */
export type ListeningSources = Schema<'ListeningSourcesResponse'>;
export type ListeningProviderSources = Schema<'ListeningProviderSources'>;
export type ListeningSourceItem = Schema<'ListeningSourceItem'>;

export type ListeningProvider = 'lastfm' | 'listenbrainz';

export const LISTENING_PROVIDERS: readonly ListeningProvider[] = ['lastfm', 'listenbrainz'];

export const LISTENING_PROVIDER_LABELS: Record<ListeningProvider, string> = {
  lastfm: 'Last.fm',
  listenbrainz: 'ListenBrainz',
};

/** Permission bit that allows automatic acquisition from playlists (admins hold every permission). */
export const PERMISSION_AUTO_REQUEST_PLAYLISTS = 128;

export const AUTO_REQUEST_DENIED_REASON =
  'Requires an admin or the "Auto-request playlist tracks" permission. Missing tracks are listed only.';

export function isListeningService(service: string): service is ListeningProvider {
  return service === 'lastfm' || service === 'listenbrainz';
}

export interface ListeningPlaylistPayload {
  provider: ListeningProvider;
  kind: string;
  ref: string;
  keep_in_sync: boolean;
  auto_request: boolean;
}
