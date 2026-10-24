import { apiRequest } from './apiClient';
import type { SlskdChallenge, SlskdChallengeList, SlskdChallengeReplyRequest } from '@/types/slskdChallenges';

const BASE = '/api/slskd-challenges';

/** Open "human check" messages from Soulseek sharers waiting for an admin reply. */
export function getSlskdChallenges(signal?: AbortSignal): Promise<SlskdChallengeList> {
  return apiRequest<SlskdChallengeList>(`${BASE}?status=open`, { signal });
}

export function replyToSlskdChallenge(id: string, text: string): Promise<SlskdChallenge> {
  const body: SlskdChallengeReplyRequest = { text };
  return apiRequest<SlskdChallenge>(`${BASE}/${encodeURIComponent(id)}/reply`, { method: 'POST', body });
}

export function dismissSlskdChallenge(id: string): Promise<SlskdChallenge> {
  return apiRequest<SlskdChallenge>(`${BASE}/${encodeURIComponent(id)}/dismiss`, { method: 'POST' });
}
