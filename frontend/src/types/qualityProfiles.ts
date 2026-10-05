/** Native quality profiles (v2): an ordered list of qualities/groups (top = best) plus custom-format scoring. */
export interface QualityEntryQuality {
  type: 'quality';
  quality: string;
  allowed: boolean;
}

export interface QualityEntryGroup {
  type: 'group';
  name: string;
  allowed: boolean;
  items: string[];
}

export type QualityEntry = QualityEntryQuality | QualityEntryGroup;

export interface FormatScore {
  format_id: number;
  score: number;
}

export interface QualityProfile {
  id: string;
  name: string;
  cutoff: string;
  items: QualityEntry[];
  upgrade_allowed: boolean;
  min_format_score: number;
  cutoff_format_score: number;
  min_upgrade_format_score: number;
  format_items: FormatScore[];
  is_default: boolean;
}

/** Body of POST /api/settings/quality-profiles (upsert; omit `id` to create). */
export interface QualityProfileInput {
  id?: string;
  name: string;
  cutoff: string;
  items: QualityEntry[];
  upgrade_allowed: boolean;
  min_format_score: number;
  cutoff_format_score: number;
  min_upgrade_format_score: number;
  format_items: FormatScore[];
  is_default: boolean;
}

/** Defaults the backend applies to a new profile (min score -100 keeps Vinyl/Mono/Censored penalties soft). */
export const DEFAULT_MIN_FORMAT_SCORE = -100;

export type ReleaseProtocol = 'usenet' | 'torrent' | 'soulseek';
export const RELEASE_PROTOCOLS: readonly ReleaseProtocol[] = ['usenet', 'torrent', 'soulseek'];

export interface ReleaseEvaluationRequest {
  title: string;
  profile_id: string;
  size_bytes?: number;
  protocol?: ReleaseProtocol;
}

export interface BreakdownFormat {
  id: number;
  name: string;
  score: number;
}

export interface BreakdownKbps {
  checked: boolean;
  measured: number | null;
  estimated: boolean;
  duration_seconds: number | null;
  min: number | null;
  preferred: number | null;
  max: number | null;
  skipped_reason: string | null;
}

export interface BreakdownReleaseProfile {
  id: number;
  name: string;
  result: string;
  detail: string;
}

export interface BreakdownRejection {
  code: string;
  message: string;
}

export interface DecisionBreakdown {
  title: string;
  release_group: string | null;
  protocol: string | null;
  source: string | null;
  quality: string;
  tier: number | null;
  tier_name: string | null;
  quality_allowed: boolean;
  quality_cutoff_met: boolean;
  matched_formats: BreakdownFormat[];
  format_score: number;
  total_score: number;
  min_format_score: number;
  cutoff_format_score: number;
  kbps: BreakdownKbps;
  release_profiles: BreakdownReleaseProfile[];
  rejections: BreakdownRejection[];
  notes: string[];
}

export interface ReleaseEvaluation {
  is_acceptable: boolean;
  rejection_reasons: string[];
  parsed_quality: string;
  breakdown: DecisionBreakdown | null;
}
