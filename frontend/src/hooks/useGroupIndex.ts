import { useEffect, useRef, useState } from 'react';
import type { GroupIndexGroup, IndexFetcher, ListSortDir } from '@/types/activity';
import { errorMessage } from '@/services/apiClient';

export interface UseGroupIndexOptions {
  sortKey: string;
  sortDir: ListSortDir;
  filters?: Readonly<Record<string, string>>;
  /** The list's current total; the index is refetched when it changes (rows were added or removed). */
  total: number;
  /** When false nothing is fetched. Default true. */
  enabled?: boolean;
}

export interface UseGroupIndexReturn {
  groups: readonly GroupIndexGroup[];
  loading: boolean;
  /** Index failures are non-fatal (the list still works, the scrubber just hides). */
  error: string | null;
}

const NO_GROUPS: readonly GroupIndexGroup[] = [];

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

/**
 * Group index (`{label, offset, count}[]`) for a list's scrubber. Refetched on sort/filter/total change; a sort or
 * filter change clears the groups at once (the old labels would point at the wrong rows), a total change keeps them
 * until the new index arrives. Skipped while the total is 0.
 */
export function useGroupIndex(fetcher: IndexFetcher, options: UseGroupIndexOptions): UseGroupIndexReturn {
  const { sortKey, sortDir, filters, total, enabled = true } = options;
  const [groups, setGroups] = useState<readonly GroupIndexGroup[]>(NO_GROUPS);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const fetcherRef = useRef<IndexFetcher>(fetcher);
  fetcherRef.current = fetcher;
  const filtersJson = JSON.stringify(filters ?? {});

  useEffect(() => {
    setGroups(NO_GROUPS);
  }, [sortKey, sortDir, filtersJson, enabled]);

  useEffect(() => {
    if (!enabled || total <= 0) {
      if (total <= 0) setGroups(NO_GROUPS);
      setLoading(false);
      return undefined;
    }
    const controller = new AbortController();
    setLoading(true);
    fetcherRef.current(
      { sortKey, sortDir, filters: JSON.parse(filtersJson) as Record<string, string> },
      controller.signal
    )
      .then((res) => {
        if (controller.signal.aborted) return;
        setGroups(res.groups.length > 0 ? res.groups : NO_GROUPS);
        setError(null);
      })
      .catch((err: unknown) => {
        if (isAbort(err) || controller.signal.aborted) return;
        setGroups(NO_GROUPS);
        setError(errorMessage(err, 'Failed to load group index'));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [sortKey, sortDir, filtersJson, total, enabled]);

  return { groups, loading, error };
}
