import type { Schema } from './apiSchema';
export type SeedCleanupStats = Schema<'SeedCleanupStats'>;

export type SeedCleanupRun = Schema<'SeedCleanupLastRun'>;

export type SeedCleanupStatus = Schema<'SeedCleanupStatus'>;

export type SeedCleanupStarted = Schema<'SeedCleanupStarted'>;

export interface OrphanRemoveRequest {
  delete_files: boolean;
}

export type OrphanRemoveResponse = Schema<'RemoveOrphanResponse'>;

export type FailedRetryResponse = Schema<'RetryFailedResponse'>;

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
