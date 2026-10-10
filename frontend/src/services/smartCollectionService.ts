import type { Schema } from '@/types/apiSchema';
import { apiRequest } from './apiClient';

export async function previewSmartCollection(
  rules: Schema<'SmartRulesBody'>,
  signal?: AbortSignal
): Promise<Schema<'SmartPreviewResponse'>> {
  return apiRequest<Schema<'SmartPreviewResponse'>>('/api/smart-collections/preview', {
    method: 'POST',
    body: rules,
    signal,
  });
}

export async function createSmartCollection(
  payload: Schema<'SmartCollectionCreateRequest'>
): Promise<Schema<'PlaylistImportResponse'>> {
  return apiRequest<Schema<'PlaylistImportResponse'>>('/api/smart-collections', {
    method: 'POST',
    body: payload,
  });
}

export async function getSmartCollection(
  id: string
): Promise<Schema<'SmartCollectionRecord'>> {
  return apiRequest<Schema<'SmartCollectionRecord'>>(`/api/smart-collections/${id}`);
}

export async function updateSmartCollection(
  id: string,
  payload: Schema<'SmartCollectionUpdateRequest'>
): Promise<Schema<'SmartCollectionRecord'>> {
  return apiRequest<Schema<'SmartCollectionRecord'>>(`/api/smart-collections/${id}`, {
    method: 'PUT',
    body: payload,
  });
}

export async function syncSmartCollection(
  id: string
): Promise<Schema<'PlaylistImportResponse'>> {
  return apiRequest<Schema<'PlaylistImportResponse'>>(`/api/smart-collections/${id}/sync`, {
    method: 'POST',
  });
}
