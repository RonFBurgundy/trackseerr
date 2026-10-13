import type { Schema } from '@/types/apiSchema';
import { apiRequest } from './apiClient';

export type RenamePreviewItem = Schema<'RenamePreviewItem'>;
export type RenamePreviewRequest = Schema<'RenamePreviewRequest'>;
export type RenameApplyRequest = Schema<'RenameApplyRequest'>;
export type RenameApplyResponse = Schema<'RenameApplyResponse'>;
export type RenameVerification = Schema<'RenameVerification'>;
export type RenameVerifyStatus = Schema<'RenameVerifyStatus'>;

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

/**
 * Starts an AcoustID verification run over the given library files.
 */
export async function startRenameVerify(fileIds: string[]): Promise<RenameVerifyStatus> {
  return apiRequest<RenameVerifyStatus>('/api/library/rename/verify', {
    method: 'POST',
    body: JSON.stringify({ file_ids: fileIds }),
  });
}

/**
 * Returns the progress of the current (or last) AcoustID verification run.
 */
export async function getRenameVerifyStatus(): Promise<RenameVerifyStatus> {
  return apiRequest<RenameVerifyStatus>('/api/library/rename/verify/status');
}
