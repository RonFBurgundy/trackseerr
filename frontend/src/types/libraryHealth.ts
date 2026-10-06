export type LibraryHealthKind = 'server_unindexed' | 'server_stale' | 'weak_match' | 'orphan_torrent' | 'cleanup_failed';

export interface LibraryHealthFinding {
  id: string;
  kind: LibraryHealthKind;
  /** Known values are listed in `CAUSE_LABELS`; unknown strings are rendered gracefully. */
  cause: string;
  group_key: string;
  path: string;
  detail: Record<string, unknown> | null;
  first_seen: string;
  last_seen: string;
}

export interface LibraryHealthGroup {
  group_key: string;
  kind: LibraryHealthKind;
  cause: string;
  count: number;
  sample_path: string;
  suggestion: string;
}

export interface LibraryHealthRun {
  id: string;
  started_at: string;
  finished_at: string | null;
  server_kind: string | null;
  disk_files: number | null;
  server_files: number | null;
  unindexed: number | null;
  stale: number | null;
  error: string | null;
}

export interface LibraryHealthMapping {
  server_prefix: string;
  local_prefix: string;
  auto: boolean;
}

export interface LibraryHealthServer {
  kind: string;
  file_paths: boolean;
}

export interface LibraryHealthResponse {
  findings: LibraryHealthFinding[];
  groups: LibraryHealthGroup[];
  last_run: LibraryHealthRun | null;
  running: boolean;
  mapping: LibraryHealthMapping | null;
  server: LibraryHealthServer | null;
  weekly: boolean;
  count: number;
}

export interface LibraryHealthCountResponse {
  count: number;
}

export interface LibraryHealthStarted {
  started: boolean;
}

export type LibraryHealthDismissScope = 'file' | 'folder';

export interface LibraryHealthDismissRequest {
  path: string;
  scope: LibraryHealthDismissScope;
}

export interface LibraryHealthMappingRequest {
  server_prefix: string;
  local_prefix: string;
}

/** The parts of a weak_match finding's `detail` the panel renders. */
export interface WeakMatchDetail {
  track_id: string | null;
  title: string | null;
  source_name: string | null;
  strength: string | null;
}

export interface LibraryHealthWeeklyResponse {
  weekly: boolean;
}
