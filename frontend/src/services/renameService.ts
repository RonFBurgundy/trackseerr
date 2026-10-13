import type { Schema } from '@/types/apiSchema';
import { ApiError, apiRequest, errorMessage } from './apiClient';

export type RenamePreviewItem = Schema<'RenamePreviewItem'>;
export type RenamePreviewRequest = Schema<'RenamePreviewRequest'>;
export type RenameApplyRequest = Schema<'RenameApplyRequest'>;
export type RenameApplyResponse = Schema<'RenameApplyResponse'>;
export type RenameVerification = Schema<'RenameVerification'>;
export type RenameVerifyStatus = Schema<'RenameVerifyStatus'>;
export type RenameRelinkRequest = Schema<'RenameRelinkRequest'>;
export type RenameRelinkResponse = Schema<'RenameRelinkResponse'>;
export type RenameRelinkFile = Schema<'RenameRelinkFile'>;
export type RenameRelinkConflictResponse = Schema<'RenameRelinkConflictResponse'>;

export type RelinkOutcome =
  | { kind: 'ok'; data: RenameRelinkResponse }
  | { kind: 'conflict'; data: RenameRelinkConflictResponse }
  | { kind: 'error'; message: string };

function isRelinkFile(value: unknown): value is RenameRelinkFile {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { id?: unknown }).id === 'string' &&
    typeof (value as { file_path?: unknown }).file_path === 'string'
  );
}

function asConflictBody(body: unknown): RenameRelinkConflictResponse | null {
  if (typeof body !== 'object' || body === null) return null;
  const { detail, existing, incoming } = body as {
    detail?: unknown;
    existing?: unknown;
    incoming?: unknown;
  };
  if (typeof detail !== 'string' || !isRelinkFile(existing) || !isRelinkFile(incoming)) return null;
  return { detail, existing, incoming };
}

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

/**
 * Links a file to the track its audio really is. A 409 (the target track already has a file) is returned as a
 * `conflict` outcome carrying both files so the caller can ask which to keep.
 */
export async function relinkFile(request: RenameRelinkRequest): Promise<RelinkOutcome> {
  try {
    const data = await apiRequest<RenameRelinkResponse>('/api/library/rename/relink', {
      method: 'POST',
      body: JSON.stringify(request),
    });
    return { kind: 'ok', data };
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      const conflict = asConflictBody(err.body);
      if (conflict) return { kind: 'conflict', data: conflict };
    }
    return { kind: 'error', message: errorMessage(err, 'Relink failed') };
  }
}
