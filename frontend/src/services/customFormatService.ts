import { ApiError, apiRequest } from './apiClient';
import type { CustomFormat, CustomFormatImportError, CustomFormatImportResult, CustomFormatInput } from '@/types/customFormats';

const BASE = '/api/settings/custom-formats';

export async function listCustomFormats(): Promise<CustomFormat[]> {
  return (await apiRequest<CustomFormat[] | null>(BASE)) ?? [];
}

export async function createCustomFormat(input: CustomFormatInput): Promise<CustomFormat> {
  return apiRequest<CustomFormat>(BASE, { method: 'POST', body: input });
}

export async function updateCustomFormat(id: number, input: CustomFormatInput): Promise<CustomFormat> {
  return apiRequest<CustomFormat>(`${BASE}/${id}`, { method: 'PUT', body: input });
}

export async function deleteCustomFormat(id: number): Promise<void> {
  await apiRequest<unknown>(`${BASE}/${id}`, { method: 'DELETE' });
}

/** Accepts one Lidarr/Servarr format object or a list of them (already parsed JSON). */
export async function importCustomFormats(payload: unknown): Promise<CustomFormatImportResult> {
  try {
    return await apiRequest<CustomFormatImportResult>(`${BASE}/import`, { method: 'POST', body: payload });
  } catch (err: unknown) {
    // When nothing could be imported the backend answers 400 {message, errors:[...]}; surface the per-entry errors.
    if (err instanceof ApiError && isImportFailure(err.detail)) return { imported: [], errors: err.detail.errors };
    throw err;
  }
}

function isImportFailure(detail: unknown): detail is { errors: CustomFormatImportError[] } {
  return typeof detail === 'object' && detail !== null && 'errors' in detail && Array.isArray(detail.errors);
}

/** Lidarr-schema JSON for one format. */
export async function exportCustomFormat(id: number): Promise<unknown> {
  return apiRequest<unknown>(`${BASE}/${id}/export`);
}
