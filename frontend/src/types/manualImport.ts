import type { Narrow, Schema } from './apiSchema';

/** Commit payload row; generated from the OpenAPI schema. */
export type ManualImportItem = Schema<'ManualImportItem'>;

export type ManualImportScope =
  | { kind: 'download'; downloadId: string; title: string }
  | { kind: 'album'; albumId: string; title: string; issueId?: string }
  | { kind: 'files'; filePaths: string[]; title: string }
  | { kind: 'folder' };

export type MatchStrength = 'strong' | 'weak' | 'none';

export type ManualImportTags = Schema<'ManualImportTags'>;

export type CandidateTrack = Schema<'ManualImportCandidateTrack'>;

export type ManualImportScanItem = Narrow<Schema<'ManualImportScanItem'>, { match_strength: MatchStrength }>;

export interface ManualImportScanRequest {
  folder_path?: string;
  download_id?: string;
  album_id?: string;
  file_paths?: string[];
}

export interface ManualImportAlbumHit {
  id: string;
  title: string;
  artist_id: string;
  artist_name: string;
  year: number | null;
}

export type FingerprintMatch = Schema<'FingerprintMatch'>;

export type FingerprintLibraryTrack = Schema<'FingerprintLibraryTrack'>;

export type FingerprintResponse =
  | { success: true; fingerprint: FingerprintMatch; library_track: FingerprintLibraryTrack | null }
  | { success: false; message: string };

export type ManualImportResult = Narrow<Schema<'ManualImportResult'>, { status: 'imported' | 'failed' }>;

export type ManualImportCommitResponse = Narrow<Schema<'ManualImportCommitResponse'>, { results: ManualImportResult[] }>;

/** Per-row fingerprint state. */
export type IdentifyState =
  | { phase: 'idle' }
  | { phase: 'running' }
  | { phase: 'done'; message: string }
  | { phase: 'failed'; message: string };

export interface ManualImportRow {
  item: ManualImportScanItem;
  checked: boolean;
  selectedTrackId: string | null;
  candidates: CandidateTrack[];
  identify: IdentifyState;
  loadingTracks: boolean;
  result: ManualImportResult | null;
}
