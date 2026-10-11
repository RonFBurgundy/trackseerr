import type { Schema } from '@/types/apiSchema';
import { apiRequest } from './apiClient';

export type TrackAlias = Schema<'TrackAlias'>;
export type TrackAliasStatus = TrackAlias['status'];
export type TrackSuggestBody = Schema<'TrackSuggestBody'>;
export type TrackAliasSuggestion = Schema<'TrackAliasSuggestion'>;
export type TrackAliasCreateRequest = Schema<'TrackAliasCreateRequest'>;
export type TrackAliasDeletedResponse = Schema<'TrackAliasDeletedResponse'>;

const aliasPath = (id: number): string => `/api/missing/aliases/${encodeURIComponent(String(id))}`;

/** Suggests the catalog song a missing playlist track should map to (auto-approved for admins). */
export async function suggestTrackMatch(playlistId: string, body: TrackSuggestBody): Promise<TrackAliasSuggestion> {
  return apiRequest<TrackAliasSuggestion>(`/api/playlists/${encodeURIComponent(playlistId)}/tracks/suggest`, {
    method: 'POST',
    body,
  });
}

/** Admin: lists track aliases, optionally by status. */
export async function listTrackAliases(status?: TrackAliasStatus): Promise<TrackAlias[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : '';
  const res = await apiRequest<TrackAlias[] | null>(`/api/missing/aliases${query}`);
  return res ?? [];
}

/** Admin: records an alias, or flags a source song as unmatchable. */
export async function createTrackAlias(body: TrackAliasCreateRequest): Promise<TrackAlias> {
  return apiRequest<TrackAlias>('/api/missing/aliases', { method: 'POST', body });
}

export async function approveTrackAlias(id: number): Promise<TrackAlias> {
  return apiRequest<TrackAlias>(`${aliasPath(id)}/approve`, { method: 'POST' });
}

export async function rejectTrackAlias(id: number): Promise<TrackAlias> {
  return apiRequest<TrackAlias>(`${aliasPath(id)}/reject`, { method: 'POST' });
}

export async function deleteTrackAlias(id: number): Promise<TrackAliasDeletedResponse> {
  return apiRequest<TrackAliasDeletedResponse>(aliasPath(id), { method: 'DELETE' });
}
