import { apiRequest } from './apiClient';
import type { Schema } from '@/types/apiSchema';

/** Interactive release search across every enabled indexer (admin, native mode). Slow: 10-60 s. */
export async function searchReleases(
  query: Schema<'InteractiveSearchQuery'>,
  signal?: AbortSignal
): Promise<Schema<'SearchResponse'>> {
  return apiRequest<Schema<'SearchResponse'>>('/api/acquisition/search', { method: 'POST', body: query, signal });
}

/** Sends one chosen release to a download client (admin, native mode; 409 under Lidarr). */
export async function grabRelease(payload: Schema<'ManualGrabPayload'>): Promise<Schema<'GrabResponse'>> {
  return apiRequest<Schema<'GrabResponse'>>('/api/acquisition/grab', { method: 'POST', body: payload });
}
