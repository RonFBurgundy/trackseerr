/** Term-based release profiles: required (OR within a profile) and ignored (any match rejects) terms. */
export interface ReleaseProfile {
  id: number;
  name: string;
  enabled: boolean;
  required: string[];
  ignored: string[];
  indexer_ids: string[];
  tags: string[];
  quality_profile_ids: string[];
}

export interface ReleaseProfileInput {
  name: string;
  enabled: boolean;
  required: string[];
  ignored: string[];
  indexer_ids: string[];
  tags: string[];
  quality_profile_ids: string[];
}

/** Name of the profile the backend seeds; the UI marks it as our suggestion. */
export const SEEDED_RELEASE_PROFILE_NAME = 'Reject bad sources';
