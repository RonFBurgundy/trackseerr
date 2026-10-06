import { apiRequest } from './apiClient';
import type { ItemHistoryEntity, ItemHistoryResponse } from '@/types/itemHistory';

export interface ItemHistoryQuery {
  limit?: number;
  /** Keyset cursor: return events older than this event id. */
  before?: number | null;
}

/** One page of an artist/album/track's audit trail, newest first. 404 (ApiError) when the item is unknown. */
export function getItemHistory(
  entity: ItemHistoryEntity,
  entityId: string,
  query: ItemHistoryQuery = {},
  signal?: AbortSignal
): Promise<ItemHistoryResponse> {
  const params = new URLSearchParams();
  if (query.limit !== undefined) params.set('limit', String(query.limit));
  if (query.before !== undefined && query.before !== null) params.set('before', String(query.before));
  const qs = params.toString();
  return apiRequest<ItemHistoryResponse>(
    `/api/library/${entity}/${encodeURIComponent(entityId)}/history${qs ? `?${qs}` : ''}`,
    { signal }
  );
}
