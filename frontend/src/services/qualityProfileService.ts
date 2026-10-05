import { apiRequest } from './apiClient';
import type {
  FormatScore,
  QualityEntry,
  QualityProfile,
  QualityProfileInput,
  ReleaseEvaluation,
  ReleaseEvaluationRequest,
} from '@/types/qualityProfiles';

const BASE = '/api/settings/quality-profiles';

/** Wire shape of one stored item; legacy rows may lack `type`. */
interface RawEntry {
  type?: string | null;
  quality?: string | null;
  name?: string | null;
  allowed?: boolean;
  items?: string[];
}

interface RawProfile extends Omit<QualityProfile, 'items' | 'format_items'> {
  items: RawEntry[];
  format_items?: FormatScore[] | null;
}

function toEntry(raw: RawEntry): QualityEntry | null {
  const allowed = raw.allowed !== false;
  if (raw.type === 'group') return { type: 'group', name: raw.name ?? '', allowed, items: raw.items ?? [] };
  if (raw.quality) return { type: 'quality', quality: raw.quality, allowed };
  return null;
}

function toProfile(raw: RawProfile): QualityProfile {
  const items: QualityEntry[] = [];
  for (const r of raw.items ?? []) {
    const entry = toEntry(r);
    if (entry) items.push(entry);
  }
  return { ...raw, items, format_items: raw.format_items ?? [] };
}

export async function listQualityProfiles(): Promise<QualityProfile[]> {
  const res = await apiRequest<RawProfile[] | null>(BASE);
  return (res ?? []).map(toProfile);
}

export async function saveQualityProfile(input: QualityProfileInput): Promise<QualityProfile> {
  return toProfile(await apiRequest<RawProfile>(BASE, { method: 'POST', body: input }));
}

export async function copyQualityProfile(id: string): Promise<QualityProfile> {
  return toProfile(await apiRequest<RawProfile>(`${BASE}/${encodeURIComponent(id)}/copy`, { method: 'POST', body: {} }));
}

export async function setDefaultQualityProfile(id: string): Promise<QualityProfile> {
  return toProfile(await apiRequest<RawProfile>(`${BASE}/${encodeURIComponent(id)}/default`, { method: 'POST' }));
}

export async function deleteQualityProfile(id: string): Promise<void> {
  await apiRequest<unknown>(`${BASE}/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

interface RawEvaluation {
  evaluation: { is_acceptable: boolean; rejection_reasons: string[]; parsed_quality: string };
  breakdown: ReleaseEvaluation['breakdown'];
}

export async function evaluateRelease(request: ReleaseEvaluationRequest): Promise<ReleaseEvaluation> {
  const res = await apiRequest<RawEvaluation>(`${BASE}/evaluate`, { method: 'POST', body: request });
  return {
    is_acceptable: res.evaluation.is_acceptable,
    rejection_reasons: res.evaluation.rejection_reasons,
    parsed_quality: res.evaluation.parsed_quality,
    breakdown: res.breakdown,
  };
}
