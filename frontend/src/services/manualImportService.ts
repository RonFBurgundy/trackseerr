import { apiRequest } from './apiClient';
import type { AlbumItem } from '@/types/models';
import type {
  CandidateTrack,
  FingerprintResponse,
  ManualImportAlbumHit,
  ManualImportCommitResponse,
  ManualImportItem,
  ManualImportScanItem,
  ManualImportScanRequest,
} from '@/types/manualImport';

const BASE = '/api/library/manual-import';

export async function scanManualImport(
  body: ManualImportScanRequest,
  signal?: AbortSignal
): Promise<ManualImportScanItem[]> {
  const res = await apiRequest<ManualImportScanItem[] | null>(`${BASE}/scan`, { method: 'POST', body, signal });
  return res ?? [];
}

export async function getManualImportAlbumTracks(albumId: string, signal?: AbortSignal): Promise<CandidateTrack[]> {
  const res = await apiRequest<CandidateTrack[] | null>(
    `${BASE}/album-tracks?album_id=${encodeURIComponent(albumId)}`,
    { signal }
  );
  return res ?? [];
}

/** Album search for the row picker; uses the catalog list endpoint (plain array, `query` filter). */
export async function searchManualImportAlbums(q: string, signal?: AbortSignal): Promise<ManualImportAlbumHit[]> {
  const res = await apiRequest<AlbumItem[] | null>(
    `/api/library/albums?query=${encodeURIComponent(q)}&limit=20`,
    { signal }
  );
  return (res ?? []).map((a) => ({
    id: String(a.id),
    title: a.title,
    artist_id: String(a.artist_id),
    artist_name: a.artist_name ?? 'Unknown Artist',
    year: a.year ?? (a.release_date ? Number.parseInt(a.release_date.slice(0, 4), 10) || null : null),
  }));
}

export async function fingerprintManualImportFile(filePath: string, signal?: AbortSignal): Promise<FingerprintResponse> {
  return apiRequest<FingerprintResponse>(`${BASE}/fingerprint`, {
    method: 'POST',
    body: { file_path: filePath },
    signal,
  });
}

export async function commitManualImport(
  items: ManualImportItem[],
  downloadId?: string
): Promise<ManualImportCommitResponse> {
  return apiRequest<ManualImportCommitResponse>(`${BASE}/commit`, {
    method: 'POST',
    body: downloadId ? { items, download_id: downloadId } : { items },
  });
}
