import type { Schema } from '@/types/apiSchema';
import { apiRequest } from './apiClient';

export type MissingTrack = Schema<'MissingTrack'>;
export type MediaTrackHit = Schema<'MediaTrackHit'>;
export type MatchOverride = Schema<'MatchOverride'>;
export type MatchOverrideRequest = Schema<'MatchOverrideRequest'>;
export type MatchCreatedResponse = Schema<'MatchCreatedResponse'>;
export type MatchDeletedResponse = Schema<'MatchDeletedResponse'>;

/**
 * Lists missing tracks from playlists, optionally filtered by playlist_id.
 */
export async function getMissingTracks(playlistId?: string): Promise<MissingTrack[]> {
  const params = playlistId ? `?playlist_id=${encodeURIComponent(playlistId)}` : '';
  const res = await apiRequest<MissingTrack[]>(`/api/missing${params}`);
  return res || [];
}

/**
 * Searches the media server library for matching tracks to enable manual correction.
 */
export async function searchMissingTracks(query: string, limit = 15): Promise<MediaTrackHit[]> {
  const params = new URLSearchParams({ query, limit: String(limit) });
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
 * Deletes a Match Memory override (undo matching).
 */
export async function deleteMatchOverride(overrideId: number): Promise<MatchDeletedResponse> {
  return apiRequest<MatchDeletedResponse>(`/api/missing/match/${overrideId}`, {
    method: 'DELETE',
  });
}
