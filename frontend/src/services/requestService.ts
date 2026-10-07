import { apiRequest } from './apiClient';
import type { Schema } from '@/types/apiSchema';
import type { RequestItem, UserQuota } from '@/types/models';
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

export async function deleteRequest(requestId: string): Promise<void> {
  return apiRequest<void>(`/api/requests/${requestId}`, {
    method: 'DELETE',
  });
}

export async function getUserQuota(): Promise<UserQuota> {
  const res = await apiRequest<Schema<'CurrentUserProfile'>>('/api/users/me');

  return {
    remaining: res.remaining_quota ?? 10,
    limit: res.quota_limit ?? 10,
    period_days: res.rolling_days ?? 7,
  };
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
