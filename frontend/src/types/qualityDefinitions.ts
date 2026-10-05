/** Quality definitions: size-per-length bounds in kbps (blank/null = unbounded). */
export interface QualityDefinition {
  quality: string;
  title: string;
  min_kbps: number | null;
  preferred_kbps: number | null;
  max_kbps: number | null;
  default_min_kbps: number | null;
  default_preferred_kbps: number | null;
  default_max_kbps: number | null;
  is_default: boolean;
  updated_at?: string | null;
}

export interface QualityDefinitionInput {
  min_kbps: number | null;
  preferred_kbps: number | null;
  max_kbps: number | null;
}
