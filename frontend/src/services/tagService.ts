import { apiRequest } from './apiClient';
import type { Schema } from '@/types';

const BASE = '/api/tags';

export async function listTags(): Promise<Schema<'TagOut'>[]> {
  return (await apiRequest<Schema<'TagOut'>[] | null>(BASE)) ?? [];
}

/** 409 (ApiError) when the label already exists, 422 when it is invalid. */
export async function createTag(label: string): Promise<Schema<'TagOut'>> {
  const body: Schema<'TagPayload'> = { label };
  return apiRequest<Schema<'TagOut'>>(BASE, { method: 'POST', body });
}

export async function renameTag(id: number, label: string): Promise<Schema<'TagOut'>> {
  const body: Schema<'TagPayload'> = { label };
  return apiRequest<Schema<'TagOut'>>(`${BASE}/${id}`, { method: 'PUT', body });
}

export async function deleteTag(id: number): Promise<void> {
  await apiRequest<unknown>(`${BASE}/${id}`, { method: 'DELETE' });
}

export async function getTagUsage(id: number): Promise<Schema<'TagUsage'>> {
  return apiRequest<Schema<'TagUsage'>>(`${BASE}/${id}/usage`);
}

/** Replaces an artist's tags with `tagIds` (native library only). */
export async function setArtistTags(artistId: number | string, tagIds: number[]): Promise<Schema<'ArtistTagsResponse'>> {
  const body: Schema<'ArtistTagsRequest'> = { tags: tagIds };
  return apiRequest<Schema<'ArtistTagsResponse'>>(`/api/library/artists/${encodeURIComponent(String(artistId))}/tags`, {
    method: 'PUT',
    body,
  });
}
