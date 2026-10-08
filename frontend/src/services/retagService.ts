import type { Schema } from '@/types/apiSchema';
import { apiRequest } from './apiClient';

export type RetagPreviewItem = Schema<'RetagPreviewItem'>;
export type RetagFieldDiff = Schema<'RetagFieldDiff'>;
export type RetagPreviewRequest = Schema<'RetagPreviewRequest'>;
export type RetagApplyRequest = Schema<'RetagApplyRequest'>;
export type RetagApplyResponse = Schema<'RetagApplyResponse'>;
export type RetagFileResult = Schema<'RetagFileResult'>;

/**
 * Previews proposed tag changes based on library metadata.
 */
export async function getRetagPreview(request?: RetagPreviewRequest): Promise<RetagPreviewItem[]> {
  const res = await apiRequest<RetagPreviewItem[]>('/api/library/retag/preview', {
    method: 'POST',
    body: JSON.stringify(request ?? {}),
  });
  return res || [];
}

/**
 * Applies batch retag to specified library files.
 */
export async function applyRetag(request: RetagApplyRequest): Promise<RetagApplyResponse> {
  return apiRequest<RetagApplyResponse>('/api/library/retag/apply', {
    method: 'POST',
    body: JSON.stringify(request),
  });
}
