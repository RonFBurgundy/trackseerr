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

export interface ItemHistoryOrigin {
  /** Server-sent kind; typed loosely so a newer backend trigger still renders. */
  trigger: string | null;
  trigger_label: string | null;
  actor_display: string;
  created_at: string;
}

export interface ItemHistoryEvent {
  id: number;
  /** Server-sent event name; typed loosely so a newer backend event still renders. */
  event: string;
  created_at: string;
  track_id: string | number | null;
  album_id: string | number | null;
  artist_id: string | number | null;
  track_title: string;
  album_title: string;
  artist_name: string;
  trigger: string | null;
  trigger_label: string | null;
  actor_display: string;
  message: string;
  details: Record<string, unknown>;
}

export interface ItemHistoryResponse {
  entity: ItemHistoryEntity;
  entity_id: string;
  origin: ItemHistoryOrigin | null;
  events: ItemHistoryEvent[];
  next_before: number | null;
}
