import { apiRequest } from './apiClient';
import type { Schema } from '@/types/apiSchema';

export type ChangelogResponse = Schema<'ChangelogResponse'>;
export type ChangelogRelease = Schema<'ChangelogRelease'>;
export type ChangelogSection = Schema<'ChangelogSection'>;
export type ChangelogUnseenResponse = Schema<'ChangelogUnseenResponse'>;
export type ChangelogSeenResponse = Schema<'ChangelogSeenResponse'>;

/** Fetches changelog releases, build info, and latest release. */
export async function getChangelog(limit?: number): Promise<ChangelogResponse> {
  const qs = limit ? `?limit=${limit}` : '';
  return await apiRequest<ChangelogResponse>(`/api/system/changelog${qs}`);
}

/** Checks whether to show the what's new popup for an admin user. */
export async function getUnseenChangelog(): Promise<ChangelogUnseenResponse> {
  return await apiRequest<ChangelogUnseenResponse>('/api/system/changelog/unseen');
}

/** Marks the current version changelog as seen by this admin. */
export async function markChangelogSeen(): Promise<ChangelogSeenResponse> {
  return await apiRequest<ChangelogSeenResponse>('/api/system/changelog/seen', {
    method: 'POST',
  });
}
