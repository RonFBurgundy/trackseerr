import type { Schema } from './apiSchema';
/** Per-item audit trail (`GET /api/library/{entity}/{id}/history`). */

export type ItemHistoryEntity = 'artist' | 'album' | 'track';

export type ItemEventName =
  | 'requested'
  | 'request_approved'
  | 'request_declined'
  | 'added_to_library'
  | 'monitored'
  | 'unmonitored'
  | 'searched'
  | 'grabbed'
  | 'download_failed'
  | 'blocklisted'
  | 'imported'
  | 'upgraded'
  | 'file_replaced'
  | 'file_deleted'
  | 'file_missing'
  | 'quarantined'
  | 'restored'
  | 'issue_opened'
  | 'issue_resolved'
  | 'health_finding'
  | 'renamed'
  | 'moved'
  | 'removed_from_library';

export type ItemTriggerKind =
  | 'request'
  | 'request_approved'
  | 'wanted'
  | 'rss'
  | 'playlist'
  | 'mix'
  | 'import_list'
  | 'manual'
  | 'issue'
  | 'upgrade'
  | 'retry'
  | 'user'
  | 'scan'
  | 'manual_import'
  | 'seed_cleanup'
  | 'recycle_cleanup'
  | 'system';

export type ItemHistoryOrigin = Schema<'ItemHistoryOrigin'>;

export type ItemHistoryEvent = Schema<'ItemHistoryEvent'>;

export type ItemHistoryResponse = Schema<'ItemHistoryResponse'>;
