import { ApiError, apiRequest, getAuthToken } from './apiClient';
import type {
  ItunesCommitRequest,
  ItunesImportStatus,
  ItunesPreview,
} from '@/types/itunesImport';

const BASE = '/api/import/itunes';

/** Uploads the raw `iTunes Library.xml` as the request body (the server streams it under a size cap). */
export async function previewItunesImport(file: File): Promise<ItunesPreview> {
  const headers: Record<string, string> = { Accept: 'application/json', 'Content-Type': 'application/xml' };
  const token = getAuthToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const response = await fetch(`${BASE}/preview`, {
    method: 'POST',
    headers,
    body: file,
    credentials: 'same-origin',
  });
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail;
    throw new ApiError(
      typeof detail === 'string' ? detail : `HTTP Error ${response.status}: ${response.statusText}`,
      response.status
    );
  }
  return data as ItunesPreview;
}

export async function commitItunesImport(importId: string, req: ItunesCommitRequest): Promise<{ job_id: string }> {
  return apiRequest<{ job_id: string }>(`${BASE}/${encodeURIComponent(importId)}/commit`, {
    method: 'POST',
    body: req,
  });
}

export async function getItunesImportStatus(importId: string): Promise<ItunesImportStatus> {
  return apiRequest<ItunesImportStatus>(`${BASE}/${encodeURIComponent(importId)}/status`);
}
