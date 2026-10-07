import type { Narrow, Schema } from './apiSchema';
export type QualityDefinition = Narrow<Schema<'QualityDefinitionResponse'>, { min_kbps: number | null; preferred_kbps: number | null; max_kbps: number | null; default_min_kbps: number | null; default_preferred_kbps: number | null; default_max_kbps: number | null }>;

export interface QualityDefinitionInput {
  min_kbps: number | null;
  preferred_kbps: number | null;
  max_kbps: number | null;
}
