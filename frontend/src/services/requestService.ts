import { apiRequest } from './apiClient';
import type { Schema } from '@/types/apiSchema';
import type { QuotaTypeKind, QuotaTypeStatus, RequestItem, UserQuota } from '@/types/models';
import type { DiscographyBatchPayload } from '@/types/account';

export async function getRequests(status?: string): Promise<RequestItem[]> {
  const url = status && status !== 'all' ? `/api/requests?status=${encodeURIComponent(status)}` : '/api/requests';
  const res = await apiRequest<Schema<'RequestListResponse'>>(url);
  return res.requests || [];
}

export interface CreateRequestPayload {
  title: string;
  artist: string;
  album?: string;
  source?: string;
  source_id?: string;
  cover_url?: string;
  /** Sent as `item_type`; the server defaults to album when omitted. */
  item_type?: 'album' | 'track';
  foreign_id?: string;
  preview_url?: string;
  release_date?: string;
}

export async function createRequest(payload: CreateRequestPayload): Promise<RequestItem> {
  return apiRequest<RequestItem>('/api/requests', {
    method: 'POST',
    body: payload,
  });
}

export async function approveRequest(requestId: string): Promise<Schema<'RequestRecord'>> {
  return apiRequest<Schema<'RequestRecord'>>(`/api/requests/${requestId}/approve`, {
    method: 'POST',
  });
}

export async function rejectRequest(requestId: string, reason?: string): Promise<Schema<'RequestRecord'>> {
  return apiRequest<Schema<'RequestRecord'>>(`/api/requests/${requestId}/reject`, {
    method: 'POST',
    body: reason ? { reason } : {},
  });
}

/** Admin-only POST /api/requests/{id}/retry: re-sends the request now and clears its retry schedule. */
export async function retryRequest(requestId: string): Promise<Schema<'RetryResult'>> {
  return apiRequest<Schema<'RetryResult'>>(`/api/requests/${requestId}/retry`, {
    method: 'POST',
  });
}

export async function deleteRequest(requestId: string): Promise<void> {
  return apiRequest<void>(`/api/requests/${requestId}`, {
    method: 'DELETE',
  });
}

const QUOTA_KINDS: readonly QuotaTypeKind[] = ['tracks', 'albums', 'discographies'];

/** Per-type quota snapshot from /api/users/me. Null limits mean unlimited (admin exemption). */
export async function getUserQuota(): Promise<UserQuota | null> {
  const res = await apiRequest<Schema<'CurrentUserProfile'>>('/api/users/me');
  const snap = res.quotas;
  if (!snap) return null;

  const types: QuotaTypeStatus[] = [];
  for (const kind of QUOTA_KINDS) {
    const limit = snap[kind];
    if (limit === null || limit === undefined) continue;
    const used = snap.used[kind];
    types.push({ kind, used, limit, remaining: Math.max(0, limit - used) });
  }
  return { unlimited: types.length === 0, period_days: snap.window_days, types };
}

/** Maximum number of albums the backend accepts in one batch. */
export const MAX_BATCH_ITEMS = 50;

/** POST /api/requests/batch with kind=discography: consumes one discography quota unit. */
export async function createDiscographyRequest(
  payload: DiscographyBatchPayload
): Promise<Schema<'BatchCreatedResponse'>> {
  return apiRequest<Schema<'BatchCreatedResponse'>>('/api/requests/batch', {
    method: 'POST',
    body: { ...payload, requests: payload.requests.slice(0, MAX_BATCH_ITEMS) },
  });
}
