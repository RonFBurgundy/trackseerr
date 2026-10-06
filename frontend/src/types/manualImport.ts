import type { Schema } from './apiSchema';

/** Commit payload row; generated from the OpenAPI schema. */
export type ManualImportItem = Schema<'ManualImportItem'>;

export type ManualImportScope =
  | { kind: 'download'; downloadId: string; title: string }
  | { kind: 'album'; albumId: string; title: string; issueId?: string }
  | { kind: 'files'; filePaths: string[]; title: string }
  | { kind: 'folder' };

export type MatchStrength = 'strong' | 'weak' | 'none';

export interface ManualImportTags {
  title: string | null;
  artist: string | null;
  album: string | null;
  year: number | null;
  track_number: number | null;
  disc_number: number | null;
  codec: string | null;
  [key: string]: string | number | boolean | null | undefined;
}

export interface CandidateTrack {
  id: string;
  title: string;
  track_number: number | null;
  disc_number: number | null;
  album_id: string;
  album_title: string;
  artist_id: string;
  artist_name: string;
  has_file: boolean;
}

export interface ManualImportScanItem {
  file_path: string;
  filename: string;
  size_bytes: number;
  tags: ManualImportTags;
  matched_artist_id: string | null;
  matched_artist_name: string | null;
  matched_album_id: string | null;
  matched_album_title: string | null;
  matched_track_id: string | null;
  matched_track_title: string | null;
  confidence: number;
  match_strength: MatchStrength;
  suggested_track_id: string | null;
  candidate_tracks: CandidateTrack[];
}

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

export interface FingerprintMatch {
  score: number;
  recording_id: string | null;
  title: string | null;
  artist: string | null;
}

export interface FingerprintLibraryTrack {
  id: string;
  title: string;
  album_id: string;
  artist: string | null;
}

export type FingerprintResponse =
  | { success: true; fingerprint: FingerprintMatch; library_track: FingerprintLibraryTrack | null }
  | { success: false; message: string };

export interface ManualImportResult {
  source_path?: string;
  status: 'imported' | 'failed';
  error?: string;
}

export interface ManualImportCommitResponse {
  imported_count: number;
  failed_count: number;
  results: ManualImportResult[];
  download_cleared: boolean;
}

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
