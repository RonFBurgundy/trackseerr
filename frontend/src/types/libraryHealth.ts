import type { Narrow, Schema } from './apiSchema';
export type LibraryHealthKind = 'server_unindexed' | 'server_stale' | 'weak_match' | 'orphan_torrent' | 'cleanup_failed';

export type LibraryHealthFinding = Narrow<Schema<'LibraryHealthFinding'>, { kind: LibraryHealthKind; detail?: Record<string, unknown> | null }>;

export type LibraryHealthGroup = Narrow<Schema<'LibraryHealthGroup'>, { kind: LibraryHealthKind }>;

export type LibraryHealthRun = Schema<'LibraryHealthRun'>;

export type LibraryHealthMapping = Schema<'LibraryHealthMapping'>;

export type LibraryHealthServer = Schema<'LibraryHealthServer'>;

export type LibraryHealthResponse = Narrow<Schema<'LibraryHealthResponse'>, { findings: LibraryHealthFinding[]; groups: LibraryHealthGroup[] }>;

export type LibraryHealthCountResponse = Schema<'LibraryHealthCount'>;

export type LibraryHealthStarted = Schema<'LibraryHealthStarted'>;

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

export type LibraryHealthWeeklyResponse = Schema<'LibraryHealthWeekly'>;
