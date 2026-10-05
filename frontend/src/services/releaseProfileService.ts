import { apiRequest } from './apiClient';
import type { ReleaseProfile, ReleaseProfileInput } from '@/types/releaseProfiles';

const BASE = '/api/settings/release-profiles';

export async function listReleaseProfiles(): Promise<ReleaseProfile[]> {
  return (await apiRequest<ReleaseProfile[] | null>(BASE)) ?? [];
}

export async function createReleaseProfile(input: ReleaseProfileInput): Promise<ReleaseProfile> {
  return apiRequest<ReleaseProfile>(BASE, { method: 'POST', body: input });
}

export async function updateReleaseProfile(id: number, input: ReleaseProfileInput): Promise<ReleaseProfile> {
  return apiRequest<ReleaseProfile>(`${BASE}/${id}`, { method: 'PUT', body: input });
}

export async function deleteReleaseProfile(id: number): Promise<void> {
  await apiRequest<unknown>(`${BASE}/${id}`, { method: 'DELETE' });
}
