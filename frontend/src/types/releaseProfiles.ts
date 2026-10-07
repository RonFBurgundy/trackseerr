import type { Narrow, Schema } from './apiSchema';
export type ReleaseProfile = Narrow<Schema<'ReleaseProfileResponse'>, { indexer_ids: string[]; tags: string[] }>;

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
