import { apiRequest } from './apiClient';
import type { QualityDefinition, QualityDefinitionInput } from '@/types/qualityDefinitions';

const BASE = '/api/settings/quality-definitions';

export async function listQualityDefinitions(): Promise<QualityDefinition[]> {
  return (await apiRequest<QualityDefinition[] | null>(BASE)) ?? [];
}

export async function updateQualityDefinition(quality: string, input: QualityDefinitionInput): Promise<QualityDefinition> {
  return apiRequest<QualityDefinition>(`${BASE}/${encodeURIComponent(quality)}`, { method: 'PUT', body: input });
}

export async function resetQualityDefinition(quality: string): Promise<QualityDefinition> {
  return apiRequest<QualityDefinition>(`${BASE}/${encodeURIComponent(quality)}/reset`, { method: 'POST' });
}

export async function resetAllQualityDefinitions(): Promise<QualityDefinition[]> {
  return (await apiRequest<QualityDefinition[] | null>(`${BASE}/reset`, { method: 'POST' })) ?? [];
}
