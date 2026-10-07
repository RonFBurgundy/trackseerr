import type { Narrow, Schema } from './apiSchema';
import type { ListMonitorMode } from './importLists';

export type ItunesPlaylistPreview = Schema<'ItunesPreviewPlaylist'>;

export type ItunesPathMapping = Schema<'PathMapping'>;

export type ItunesSuggestedMapping = Schema<'ItunesSuggestedMapping'>;

export type ItunesPreview = Schema<'ItunesPreviewResponse'>;

export interface ItunesCommitRequest {
  playlists: string[] | 'all';
  path_mappings: ItunesPathMapping[];
  monitor_mode: ListMonitorMode;
  name_prefix: string | null;
  include_folders: boolean;
  import_play_stats: boolean;
}

export type ItunesJobState = 'idle' | 'queued' | 'running' | 'completed' | 'failed';

export type ItunesPlaylistResult = Narrow<Schema<'ItunesJobPlaylist'>, { state: 'pending' | 'done' | 'failed' }>;

export type ItunesImportStatus = Narrow<Schema<'ItunesImportStatus'>, { state: ItunesJobState; playlists: ItunesPlaylistResult[] }>;
