import type { ListMonitorMode } from './importLists';

export interface ItunesPlaylistPreview {
  key: string;
  name: string;
  is_smart: boolean;
  item_count: number;
}

export interface ItunesPathMapping {
  from: string;
  to: string;
}

export interface ItunesSuggestedMapping extends ItunesPathMapping {
  sample_matches: number;
}

export interface ItunesPreview {
  import_id: string;
  track_count: number;
  playlists: ItunesPlaylistPreview[];
  suggested_mappings: ItunesSuggestedMapping[];
  skipped: { builtin: number; folders: number; empty: number };
}

export interface ItunesCommitRequest {
  playlists: string[] | 'all';
  path_mappings: ItunesPathMapping[];
  monitor_mode: ListMonitorMode;
  name_prefix: string | null;
  include_folders: boolean;
  import_play_stats: boolean;
}

export type ItunesJobState = 'idle' | 'queued' | 'running' | 'completed' | 'failed';

export interface ItunesPlaylistResult {
  key: string;
  name: string;
  state: 'pending' | 'done' | 'failed';
  matched: number;
  missing: number;
  created_playlist_id: string | null;
  error?: string | null;
}

export interface ItunesImportStatus {
  state: ItunesJobState;
  job_id: string | null;
  total: number;
  done: number;
  error: string | null;
  play_stats: { requested: boolean; applied: boolean; note?: string } | null;
  playlists: ItunesPlaylistResult[];
}
