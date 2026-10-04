import { apiRequest } from './apiClient';
import { buildIndexUrl, buildListUrl } from './listUrl';
import type {
  ActivityActionResult,
  ActivityBlocklistRecord,
  ActivityHistoryRecord,
  ActivityQueueRecord,
  GroupIndexResponse,
  IndexQuery,
  ListQuery,
  PagedResponse,
  QueueRemoveOptions,
  WantedCutoffRecord,
  WantedListName,
  WantedRecord,
  WantedSearchRequest,
  WantedSearchResponse,
} from '@/types/activity';

export function getActivityQueue(
  q: ListQuery,
  signal?: AbortSignal
): Promise<PagedResponse<ActivityQueueRecord>> {
  return apiRequest<PagedResponse<ActivityQueueRecord>>(buildListUrl('/api/activity/queue', q), { signal });
}

export function getActivityHistory(
  q: ListQuery,
  signal?: AbortSignal
): Promise<PagedResponse<ActivityHistoryRecord>> {
  return apiRequest<PagedResponse<ActivityHistoryRecord>>(buildListUrl('/api/activity/history', q), { signal });
}

export function getActivityBlocklist(
  q: ListQuery,
  signal?: AbortSignal
): Promise<PagedResponse<ActivityBlocklistRecord>> {
  return apiRequest<PagedResponse<ActivityBlocklistRecord>>(buildListUrl('/api/activity/blocklist', q), { signal });
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
  return apiRequest<PagedResponse<WantedRecord>>(buildListUrl('/api/wanted/missing', q), { signal });
}

export function getWantedCutoff(
  q: ListQuery,
  signal?: AbortSignal
): Promise<PagedResponse<WantedCutoffRecord>> {
  return apiRequest<PagedResponse<WantedCutoffRecord>>(buildListUrl('/api/wanted/cutoff', q), { signal });
}

export function searchWanted(body: WantedSearchRequest): Promise<WantedSearchResponse> {
  return apiRequest<WantedSearchResponse>('/api/wanted/search', { method: 'POST', body });
}

/** Group index for History (date groups). Same sort and `event` filter as the list. */
export function getActivityHistoryIndex(q: IndexQuery, signal?: AbortSignal): Promise<GroupIndexResponse> {
  return apiRequest<GroupIndexResponse>(buildIndexUrl('/api/activity/history', q), { signal });
}

/** Group index for a Wanted list. Native mode only; Lidarr mode answers `groups: []`. */
export function getWantedIndex(
  list: WantedListName,
  q: IndexQuery,
  signal?: AbortSignal
): Promise<GroupIndexResponse> {
  return apiRequest<GroupIndexResponse>(buildIndexUrl(`/api/wanted/${list}`, q), { signal });
}
