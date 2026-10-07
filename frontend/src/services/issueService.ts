import type { Schema } from '@/types/apiSchema';
import { apiRequest } from './apiClient';
import type {
  CreateIssuePayload,
  Issue,
  IssueActionResponse,
  IssueComment,
  IssueListFilters,
  IssueOpenCount,
  IssueStatus,
} from '@/types/models';

const base = (id: string): string => `/api/issues/${encodeURIComponent(id)}`;

/** Lists the caller's issues (admins see all server-side). */
export async function getIssues(filters: IssueListFilters = {}, signal?: AbortSignal): Promise<Issue[]> {
  const qs = new URLSearchParams();
  if (filters.status) qs.set('status', filters.status);
  if (filters.media_title) qs.set('media_title', filters.media_title);
  if (filters.artist) qs.set('artist', filters.artist);
  const query = qs.toString();
  const res = await apiRequest<Issue[] | null>(query ? `/api/issues?${query}` : '/api/issues', { signal });
  return res ?? [];
}

export async function getIssue(id: string): Promise<Issue> {
  return apiRequest<Issue>(base(id));
}

export async function createIssue(payload: CreateIssuePayload): Promise<Issue> {
  return apiRequest<Issue>('/api/issues', { method: 'POST', body: payload });
}

export async function getUnreadIssueCount(signal?: AbortSignal): Promise<number> {
  const res = await apiRequest<Schema<'IssueCount'>>('/api/issues/unread-count', { signal });
  return res.count;
}

/** Admin, core tier only. */
export async function getOpenIssueCount(signal?: AbortSignal): Promise<IssueOpenCount> {
  return apiRequest<IssueOpenCount>('/api/issues/open-count', { signal });
}

export async function markIssueSeen(id: string): Promise<void> {
  await apiRequest<unknown>(`${base(id)}/seen`, { method: 'POST' });
}

/** Reporter transition: reopen a resolved / won't-fix issue, or close an open / in-progress one. */
export async function setIssueStatus(id: string, status: IssueStatus): Promise<Issue> {
  return apiRequest<Issue>(`${base(id)}/status`, { method: 'POST', body: { status } });
}

export async function getIssueComments(id: string): Promise<IssueComment[]> {
  const res = await apiRequest<IssueComment[] | null>(`${base(id)}/comments`);
  return res ?? [];
}

export async function addIssueComment(id: string, body: string): Promise<IssueComment> {
  return apiRequest<IssueComment>(`${base(id)}/comments`, { method: 'POST', body: { body } });
}

/** Admin: change status and/or details. */
export async function updateIssue(id: string, patch: { status?: IssueStatus; problem_details?: string }): Promise<Issue> {
  return apiRequest<Issue>(base(id), { method: 'PUT', body: patch });
}

export async function deleteIssue(id: string): Promise<void> {
  await apiRequest<unknown>(base(id), { method: 'DELETE' });
}

/** Admin fix action; rematch returns the scope for the Manual Import modal. */
export async function runIssueAction(id: string, action: string): Promise<IssueActionResponse> {
  return apiRequest<IssueActionResponse>(`${base(id)}/actions/${encodeURIComponent(action)}`, { method: 'POST' });
}

/** Window event fired after anything changes an issue, so nav badge counts re-read without prop plumbing. */
export const ISSUES_CHANGED_EVENT = 'trackseerr:issues-changed';

export function notifyIssuesChanged(): void {
  window.dispatchEvent(new Event(ISSUES_CHANGED_EVENT));
}
