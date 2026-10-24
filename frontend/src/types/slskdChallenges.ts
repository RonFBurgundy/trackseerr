import type { Schema } from './apiSchema';

export type SlskdChallenge = Schema<'SlskdChallenge'>;

export type SlskdChallengeList = Schema<'SlskdChallengeList'>;

export interface SlskdChallengeReplyRequest {
  text: string;
}
