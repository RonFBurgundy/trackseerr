import type { Schema } from '@/types/apiSchema';
import { apiRequest } from './apiClient';

export type MissingTrack = Schema<'MissingTrack'>;
export type MediaTrackHit = Schema<'MediaTrackHit'>;
export type MatchOverride = Schema<'MatchOverride'>;
export type MatchOverrideRequest = Schema<'MatchOverrideRequest'>;
export type MatchCreatedResponse = Schema<'MatchCreatedResponse'>;
export type MatchDeletedResponse = Schema<'MatchDeletedResponse'>;
export type MatchSource = 'library' | 'server';

/**
 * Lists missing tracks from playlists, optionally filtered by playlist_id.
 */
export async function getMissingTracks(playlistId?: string): Promise<MissingTrack[]> {
  const params = playlistId ? `?playlist_id=${encodeURIComponent(playlistId)}` : '';
  const res = await apiRequest<MissingTrack[]>(`/api/missing${params}`);
  return res || [];
}

/**
 * Searches Trackseerr's library or a media server for matching tracks to enable manual correction.
 */
export async function searchMissingTracks(
  query: string,
  limit = 15,
  serverId?: string,
  source: MatchSource = 'library'
): Promise<MediaTrackHit[]> {
  const params = new URLSearchParams({ query, limit: String(limit), source });
  if (source === 'server' && serverId) params.set('server_id', serverId);
  const res = await apiRequest<MediaTrackHit[]>(`/api/missing/search?${params.toString()}`);
  return res || [];
}

/**
 * Records a manual match override and deletes corresponding missing track entries.
 */
export async function createMatchOverride(data: MatchOverrideRequest): Promise<MatchCreatedResponse> {
  return apiRequest<MatchCreatedResponse>('/api/missing/match', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

/**
 * Lists all stored Match Memory overrides.
 */
export async function getMatchOverrides(): Promise<MatchOverride[]> {
  const res = await apiRequest<MatchOverride[]>('/api/missing/matches');
  return res || [];
}

/**
 * Deletes a Match Memory override or cross-server library pin (undo matching).
 */
export async function deleteMatchOverride(
  overrideId: number,
  serverId?: string | null,
  kind?: MatchOverride['kind']
): Promise<MatchDeletedResponse> {
  const params = new URLSearchParams();
  if (kind) params.set('kind', kind);
  if (serverId) params.set('server_id', serverId);
  const qs = params.toString();
  return apiRequest<MatchDeletedResponse>(`/api/missing/match/${overrideId}${qs ? `?${qs}` : ''}`, {
    method: 'DELETE',
  });
}
