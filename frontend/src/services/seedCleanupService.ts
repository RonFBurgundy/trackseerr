import { apiRequest } from './apiClient';
import type {
  FailedRetryResponse,
  OrphanRemoveRequest,
  OrphanRemoveResponse,
  SeedCleanupStarted,
  SeedCleanupStatus,
  SeedingList,
} from '@/types/seedCleanup';

const BASE = '/api/seed-cleanup';

/** Starts a run; the server answers 409 (ApiError) when one is already running. */
export function runSeedCleanup(): Promise<SeedCleanupStarted> {
  return apiRequest<SeedCleanupStarted>(`${BASE}/run`, { method: 'POST' });
}

export function getSeedCleanupStatus(signal?: AbortSignal): Promise<SeedCleanupStatus> {
  return apiRequest<SeedCleanupStatus>(`${BASE}/status`, { signal });
}

/** Torrents removed from the Queue that still seed until their seed goal is met. */
export function getSeedingList(signal?: AbortSignal): Promise<SeedingList> {
  return apiRequest<SeedingList>(`${BASE}/seeding`, { signal });
}

export function removeOrphanTorrent(findingId: string, body: OrphanRemoveRequest): Promise<OrphanRemoveResponse> {
  return apiRequest<OrphanRemoveResponse>(`${BASE}/orphans/${encodeURIComponent(findingId)}/remove`, { method: 'POST', body });
}

export function retryFailedCleanup(findingId: string): Promise<FailedRetryResponse> {
  return apiRequest<FailedRetryResponse>(`${BASE}/failed/${encodeURIComponent(findingId)}/retry`, { method: 'POST' });
}
