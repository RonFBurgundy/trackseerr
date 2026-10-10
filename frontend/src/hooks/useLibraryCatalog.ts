import { useCallback, useMemo, useState } from 'react';
import type { IndexFetcher, ListSortDir } from '@/types/activity';
import { useVirtualPagedList, type FetchPage, type ListKey, type VirtualPagedList } from './useVirtualPagedList';
import { useGroupIndex, type UseGroupIndexReturn } from './useGroupIndex';

export interface LibrarySortOption {
  /** Server `sort_key`. */
  key: string;
  label: string;
  /** Direction a fresh selection of this key starts in. */
  defaultDir: ListSortDir;
}

export interface UseLibraryCatalogOptions<T> {
  fetchPage: FetchPage<T>;
  fetchIndex: IndexFetcher;
  getKey: (item: T) => ListKey;
  sortOptions: ReadonlyArray<LibrarySortOption>;
  /** Server-side search (`q`). */
  query: string;
  monitoredOnly: boolean;
  enabled?: boolean;
  /** Extra fixed filters, e.g. `artist_id`. */
  extraFilters?: Readonly<Record<string, string | readonly string[]>>;
}

export interface UseLibraryCatalogReturn<T> {
  list: VirtualPagedList<T>;
  index: UseGroupIndexReturn;
  sortKey: string;
  sortDir: ListSortDir;
  /** Same key flips the direction when `dir` is omitted; a new key starts at its default direction. */
  changeSort: (key: string, dir?: ListSortDir) => void;
}

/** Sort state + paged list + scrubber index for one library tab, sharing a single filter set. */
export function useLibraryCatalog<T>(options: UseLibraryCatalogOptions<T>): UseLibraryCatalogReturn<T> {
  const { fetchPage, fetchIndex, getKey, sortOptions, query, monitoredOnly, enabled = true, extraFilters } = options;
  const [sort, setSort] = useState<{ key: string; dir: ListSortDir }>({
    key: sortOptions[0].key,
    dir: sortOptions[0].defaultDir,
  });
  const extraJson = JSON.stringify(extraFilters ?? {});
  const filters = useMemo<Record<string, string | readonly string[]>>(
    () => ({
      ...(JSON.parse(extraJson) as Record<string, string | readonly string[]>),
      q: query.trim(),
      monitored_only: monitoredOnly ? 'true' : '',
    }),
    [extraJson, query, monitoredOnly]
  );

  const list = useVirtualPagedList<T>(fetchPage, { sortKey: sort.key, sortDir: sort.dir, filters, getKey, enabled });
  const index = useGroupIndex(fetchIndex, {
    sortKey: sort.key,
    sortDir: sort.dir,
    filters,
    total: list.total,
    enabled,
  });

  const changeSort = useCallback(
    (key: string, dir?: ListSortDir): void => {
      setSort((prev) => {
        if (dir !== undefined) return { key, dir };
        if (key === prev.key) return { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' };
        const opt = sortOptions.find((o) => o.key === key);
        return { key, dir: opt?.defaultDir ?? 'asc' };
      });
    },
    [sortOptions]
  );

  return { list, index, sortKey: sort.key, sortDir: sort.dir, changeSort };
}
