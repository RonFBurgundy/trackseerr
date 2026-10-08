import type { Schema } from '@/types/apiSchema';
import { apiRequest } from './apiClient';

export type RenamePreviewItem = Schema<'RenamePreviewItem'>;
export type RenamePreviewRequest = Schema<'RenamePreviewRequest'>;
export type RenameApplyRequest = Schema<'RenameApplyRequest'>;
export type RenameApplyResponse = Schema<'RenameApplyResponse'>;

/**
 * Previews proposed file path changes based on token naming templates.
 */
export async function getRenamePreview(request?: RenamePreviewRequest): Promise<RenamePreviewItem[]> {
  const res = await apiRequest<RenamePreviewItem[]>('/api/library/rename/preview', {
    method: 'POST',
    body: JSON.stringify(request ?? {}),
  });
  return res || [];
}

/**
 * Applies batch renaming to specified library files.
 */
export async function applyRename(request: RenameApplyRequest): Promise<RenameApplyResponse> {
  return apiRequest<RenameApplyResponse>('/api/library/rename/apply', {
    method: 'POST',
    body: JSON.stringify(request),
  });
}
