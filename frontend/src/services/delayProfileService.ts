import { apiRequest } from './apiClient';
import type { DelayProfile, DelayProfileInput, PendingRelease } from '@/types/delayProfiles';

const BASE = '/api/settings/delay-profiles';
const PENDING = '/api/acquisition/pending';

export async function listDelayProfiles(): Promise<DelayProfile[]> {
  return (await apiRequest<DelayProfile[] | null>(BASE)) ?? [];
}

export async function createDelayProfile(input: DelayProfileInput): Promise<DelayProfile> {
  return apiRequest<DelayProfile>(BASE, { method: 'POST', body: input });
}

export async function updateDelayProfile(id: number, input: DelayProfileInput): Promise<DelayProfile> {
  return apiRequest<DelayProfile>(`${BASE}/${id}`, { method: 'PUT', body: input });
}

export async function deleteDelayProfile(id: number): Promise<void> {
  await apiRequest<unknown>(`${BASE}/${id}`, { method: 'DELETE' });
}

/** `ids` is the full new order of the non-default profiles (the default stays pinned last by the server). */
export async function reorderDelayProfiles(ids: number[]): Promise<void> {
  await apiRequest<unknown>(`${BASE}/reorder`, { method: 'POST', body: { ids } });
}

export async function listPendingReleases(): Promise<PendingRelease[]> {
  return (await apiRequest<PendingRelease[] | null>(PENDING)) ?? [];
}

export async function dropPendingRelease(id: number): Promise<void> {
  await apiRequest<unknown>(`${PENDING}/${id}`, { method: 'DELETE' });
}

export async function grabPendingRelease(id: number): Promise<void> {
  await apiRequest<unknown>(`${PENDING}/${id}/grab`, { method: 'POST' });
}
