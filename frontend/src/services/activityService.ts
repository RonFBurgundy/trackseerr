import { apiRequest } from './apiClient';
import type {
  ActivityActionResult,
  ActivityBlocklistRecord,
  ActivityHistoryRecord,
  ActivityQueueRecord,
  ListQuery,
  PagedResponse,
  QueueRemoveOptions,
  WantedCutoffRecord,
  WantedRecord,
  WantedSearchRequest,
  WantedSearchResponse,
} from '@/types/activity';

function listUrl(path: string, q: ListQuery): string {
  const params = new URLSearchParams({
    page: String(q.page),
    page_size: String(q.pageSize),
    sort_key: q.sortKey,
    sort_dir: q.sortDir,
  });
  for (const [key, value] of Object.entries(q.filters ?? {})) {
    if (value) params.set(key, value);
  }
  return `${path}?${params.toString()}`;
}

export function getActivityQueue(
  q: ListQuery,
  signal?: AbortSignal
): Promise<PagedResponse<ActivityQueueRecord>> {
  return apiRequest<PagedResponse<ActivityQueueRecord>>(listUrl('/api/activity/queue', q), { signal });
}

export function getActivityHistory(
  q: ListQuery,
  signal?: AbortSignal
): Promise<PagedResponse<ActivityHistoryRecord>> {
  return apiRequest<PagedResponse<ActivityHistoryRecord>>(listUrl('/api/activity/history', q), { signal });
}

export function getActivityBlocklist(
  q: ListQuery,
  signal?: AbortSignal
): Promise<PagedResponse<ActivityBlocklistRecord>> {
  return apiRequest<PagedResponse<ActivityBlocklistRecord>>(listUrl('/api/activity/blocklist', q), { signal });
}

export function removeActivityQueueItem(id: string | number, opts: QueueRemoveOptions): Promise<ActivityActionResult> {
  const params = new URLSearchParams({
    remove_from_client: String(opts.removeFromClient),
    blocklist: String(opts.blocklist),
  });
  return apiRequest<ActivityActionResult>(`/api/activity/queue/${encodeURIComponent(String(id))}?${params.toString()}`, {
    method: 'DELETE',
  });
}

export function retryActivityQueueItem(id: string | number): Promise<ActivityActionResult> {
  return apiRequest<ActivityActionResult>(`/api/activity/queue/${encodeURIComponent(String(id))}/retry`, {
    method: 'POST',
  });
}

export function markHistoryFailed(id: string | number): Promise<ActivityActionResult> {
  return apiRequest<ActivityActionResult>(`/api/activity/history/${encodeURIComponent(String(id))}/failed`, {
    method: 'POST',
  });
}

export function removeBlocklistItem(id: string | number): Promise<ActivityActionResult> {
  return apiRequest<ActivityActionResult>(`/api/activity/blocklist/${encodeURIComponent(String(id))}`, {
    method: 'DELETE',
  });
}

export function getWantedMissing(
  q: ListQuery,
  signal?: AbortSignal
): Promise<PagedResponse<WantedRecord>> {
  return apiRequest<PagedResponse<WantedRecord>>(listUrl('/api/wanted/missing', q), { signal });
}

export function getWantedCutoff(
  q: ListQuery,
  signal?: AbortSignal
): Promise<PagedResponse<WantedCutoffRecord>> {
  return apiRequest<PagedResponse<WantedCutoffRecord>>(listUrl('/api/wanted/cutoff', q), { signal });
}

export function searchWanted(body: WantedSearchRequest): Promise<WantedSearchResponse> {
  return apiRequest<WantedSearchResponse>('/api/wanted/search', { method: 'POST', body });
}
