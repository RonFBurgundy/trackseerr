export interface SeedCleanupStats {
  evaluated: number;
  removed: number;
  deleted_files: number;
  orphans: number;
  failures: number;
}

export interface SeedCleanupRun {
  started_at: string;
  finished_at: string | null;
  stats: SeedCleanupStats;
  error: string | null;
}

export interface SeedCleanupStatus {
  running: boolean;
  last_run: SeedCleanupRun | null;
}

export interface SeedCleanupStarted {
  started: boolean;
}

export interface OrphanRemoveRequest {
  delete_files: boolean;
}

export interface OrphanRemoveResponse {
  removed: boolean;
  delete_files: boolean;
}

export interface FailedRetryResponse {
  retried: boolean;
  removed: boolean;
  status: string;
  attempts: number;
  error: string | null;
}

/** The parts of an orphan_torrent finding's `detail` the panel renders. */
export interface OrphanTorrentInfo {
  name: string;
  size: number | null;
  ratio: number | null;
  seedingSeconds: number | null;
  path: string;
}

/** The parts of a cleanup_failed finding's `detail` the panel renders. */
export interface CleanupFailedInfo {
  title: string;
  error: string | null;
  attempts: number | null;
}
