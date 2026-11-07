import { apiRequest } from './apiClient';
import type { Schema } from '@/types/apiSchema';

export type ExclusionItem = Schema<'ExclusionItem'>;
export type ExclusionPage = Schema<'ExclusionPage'>;
export type ExclusionCreate = Schema<'ExclusionCreate'>;
export type ExclusionCreated = Schema<'ExclusionCreated'>;
export type ExclusionCheckResponse = Schema<'ExclusionCheckResponse'>;

export interface ListExclusionsQuery {
  page?: number;
  pageSize?: number;
  scope?: string | null;
  search?: string | null;
}

export interface CheckExclusionQuery {
  artist?: string | null;
  title?: string | null;
  album?: string | null;
  artist_mbid?: string | null;
  album_mbid?: string | null;
  recording_mbid?: string | null;
}

export function listExclusions(
  q?: ListExclusionsQuery,
  signal?: AbortSignal
): Promise<ExclusionPage> {
  const params = new URLSearchParams();
  if (q?.page !== undefined) params.set('page', String(q.page));
  if (q?.pageSize !== undefined) params.set('page_size', String(q.pageSize));
  if (q?.scope) params.set('scope', q.scope);
  if (q?.search) params.set('search', q.search);
  const qs = params.toString();
  return apiRequest<ExclusionPage>(`/api/exclusions${qs ? `?${qs}` : ''}`, { signal });
}

export function createExclusion(body: ExclusionCreate): Promise<ExclusionCreated> {
  return apiRequest<ExclusionCreated>('/api/exclusions', {
    method: 'POST',
    body,
  });
}

export function deleteExclusion(id: string): Promise<void> {
  return apiRequest<void>(`/api/exclusions/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
}

export function checkExclusion(
  q: CheckExclusionQuery,
  signal?: AbortSignal
): Promise<ExclusionCheckResponse> {
  const params = new URLSearchParams();
  if (q.artist) params.set('artist', q.artist);
  if (q.title) params.set('title', q.title);
  if (q.album) params.set('album', q.album);
  if (q.artist_mbid) params.set('artist_mbid', q.artist_mbid);
  if (q.album_mbid) params.set('album_mbid', q.album_mbid);
  if (q.recording_mbid) params.set('recording_mbid', q.recording_mbid);
  const qs = params.toString();
  return apiRequest<ExclusionCheckResponse>(`/api/exclusions/check${qs ? `?${qs}` : ''}`, { signal });
}
