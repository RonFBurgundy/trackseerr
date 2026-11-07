import { useCallback, useMemo } from 'react';
import type { ListQuery, PagedResponse } from '@/types/activity';
import {
  listExclusions,
  createExclusion,
  deleteExclusion,
  type ExclusionCreate,
  type ExclusionCreated,
  type ExclusionItem,
} from '@/services/exclusionsService';
import { errorMessage } from '@/services/apiClient';
import {
  pagedFetcher,
  useVirtualPagedList,
  type VirtualPagedList,
} from '@/hooks/useVirtualPagedList';

async function fetchExclusionsPaged(
  q: ListQuery,
  signal?: AbortSignal
): Promise<PagedResponse<ExclusionItem>> {
  const scope = typeof q.filters?.scope === 'string' && q.filters.scope !== 'all' ? q.filters.scope : undefined;
  const search = typeof q.filters?.search === 'string' && q.filters.search ? q.filters.search : undefined;
  const res = await listExclusions({ page: q.page, pageSize: q.pageSize, scope, search }, signal);
  return {
    mode: 'native',
    page: res.page,
    page_size: res.page_size,
    records: res.items,
    sort_dir: q.sortDir,
    sort_key: q.sortKey,
    total: res.total,
  };
}

export const fetchExclusions = pagedFetcher<ExclusionItem>(fetchExclusionsPaged);
export const getExclusionKey = (item: ExclusionItem): string => item.id;

export interface UseExclusionsOptions {
  scope?: string;
  search?: string;
  onToast?: (msg: string, tone?: 'ok' | 'error') => void;
  enabled?: boolean;
}

export interface UseExclusionsReturn {
  list: VirtualPagedList<ExclusionItem>;
  excludeItem: (payload: ExclusionCreate) => Promise<ExclusionCreated>;
  removeExclusion: (id: string) => Promise<void>;
}

export function useExclusions(options: UseExclusionsOptions = {}): UseExclusionsReturn {
  const { scope = 'all', search = '', onToast, enabled = true } = options;

  const filters = useMemo<Record<string, string>>(() => {
    const f: Record<string, string> = {};
    if (scope && scope !== 'all') f.scope = scope;
    if (search.trim()) f.search = search.trim();
    return f;
  }, [scope, search]);

  const list = useVirtualPagedList<ExclusionItem>(fetchExclusions, {
    sortKey: 'date',
    sortDir: 'desc',
    filters,
    getKey: getExclusionKey,
    enabled,
  });

  const excludeItem = useCallback(
    async (payload: ExclusionCreate): Promise<ExclusionCreated> => {
      try {
        const res = await createExclusion(payload);
        const name = res.item.title || res.item.artist_name || payload.title || payload.artist_name || 'item';
        const msg =
          res.unmonitored > 0
            ? `Excluded "${name}", unmonitored ${res.unmonitored}`
            : `Excluded "${name}"`;
        onToast?.(msg, 'ok');
        return res;
      } catch (err: unknown) {
        const msg = errorMessage(err, 'Failed to exclude item');
        onToast?.(msg, 'error');
        throw err;
      }
    },
    [onToast]
  );

  const removeExclusion = useCallback(
    async (id: string): Promise<void> => {
      try {
        await deleteExclusion(id);
        list.removeItems(new Set([id]));
        onToast?.('Exclusion removed');
      } catch (err: unknown) {
        const msg = errorMessage(err, 'Failed to remove exclusion');
        onToast?.(msg, 'error');
        throw err;
      }
    },
    [list, onToast]
  );

  return {
    list,
    excludeItem,
    removeExclusion,
  };
}
