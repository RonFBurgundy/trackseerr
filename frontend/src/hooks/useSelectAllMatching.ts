import { useCallback, useRef, useState } from 'react';
import type { ListQuery, PagedResponse } from '@/types/activity';
import { errorMessage } from '@/services/apiClient';
import type { ListKey } from './useVirtualPagedList';

const PAGE_SIZE = 200;

export interface UseSelectAllMatchingReturn {
  busy: boolean;
  /** Resolves every key matching the filters (all pages), or null after a failure (already toasted). A call during a scan returns the scan in flight. */
  collect: () => Promise<ListKey[] | null>;
}

/**
 * Pages through a list endpoint with the panel's current filters and returns every matching key, so "select all"
 * covers the whole filtered result and not just the rendered or virtualized window.
 */
export function useSelectAllMatching<T>(
  fetchPage: (q: ListQuery, signal?: AbortSignal) => Promise<PagedResponse<T>>,
  getKey: (item: T) => ListKey,
  filters: Readonly<Record<string, string | readonly string[]>>,
  sortKey: string,
  onToast: (msg: string, tone?: 'ok' | 'error') => void
): UseSelectAllMatchingReturn {
  const [busy, setBusy] = useState<boolean>(false);
  const inflight = useRef<Promise<ListKey[] | null> | null>(null);

  const collect = useCallback((): Promise<ListKey[] | null> => {
    if (inflight.current) return inflight.current;
    setBusy(true);
    const run = async (): Promise<ListKey[] | null> => {
      try {
        // A Set keeps offset paging honest when rows are inserted mid-scan and shift into a later page.
        const keys = new Set<ListKey>();
        for (let page = 1; ; page += 1) {
          const res = await fetchPage({ page, pageSize: PAGE_SIZE, sortKey, sortDir: 'asc', filters });
          for (const item of res.records) keys.add(getKey(item));
          if (res.records.length === 0 || keys.size >= res.total) break;
        }
        return Array.from(keys);
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Could not load the full selection'), 'error');
        return null;
      } finally {
        inflight.current = null;
        setBusy(false);
      }
    };
    const promise = run();
    inflight.current = promise;
    return promise;
  }, [fetchPage, getKey, filters, sortKey, onToast]);

  return { busy, collect };
}
