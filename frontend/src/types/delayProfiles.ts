import type { Narrow, Schema } from './apiSchema';
import type { ReleaseProtocol } from './qualityProfiles';

export type DelayMinutes = Schema<'DelaysModel'>;

export type DelayProfile = Narrow<Schema<'DelayProfileResponse'>, { preferred_protocol: ReleaseProtocol }>;

export interface DelayProfileInput {
  name: string;
  preferred_protocol: ReleaseProtocol;
  delays: DelayMinutes;
  bypass_if_highest_quality: boolean;
  bypass_if_above_score: number | null;
  tags: string[];
}

export type PendingRelease = Schema<'PendingReleaseResponse'>;
