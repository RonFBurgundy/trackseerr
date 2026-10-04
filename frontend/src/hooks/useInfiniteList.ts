import { useCallback, useEffect, useRef, useState } from 'react';
import type { ListQuery, ListSortDir, PagedResponse } from '@/types/activity';
import { errorMessage } from '@/services/apiClient';

export type ListKey = string | number;

export interface FetchPageRequest {
  page: number;
  pageSize: number;
  sortKey: string;
  sortDir: ListSortDir;
  filters: Readonly<Record<string, string>>;
  signal: AbortSignal;
}

export interface FetchPageResult<T> {
  items: T[];
  total: number;
  /** Server-reported data source (`native` | `lidarr`), when the endpoint provides it. */
  mode?: string | null;
}

export type FetchPage<T> = (req: FetchPageRequest) => Promise<FetchPageResult<T>>;

export interface UseInfiniteListOptions<T> {
  pageSize?: number;
  sortKey: string;
  sortDir: ListSortDir;
  filters?: Readonly<Record<string, string>>;
  /** Stable identity per item; used to dedupe rows that shift between pages. */
  getKey: (item: T) => ListKey;
  /** When false nothing is fetched (e.g. non-admin). Default true. */
  enabled?: boolean;
}

export interface UseInfiniteListReturn<T> {
  items: T[];
  total: number;
  /** True while a page request is in flight (initial or append). */
  loading: boolean;
  error: string | null;
  hasMore: boolean;
  mode: string | null;
  /** Discards everything and reloads page 1 (shows the loading state). */
  reload: () => void;
  /** Appends the next page; also the retry path after an error. No-op while one is in flight. */
  loadMore: () => void;
  /**
   * Silently re-fetches page 1 and merges it by key into the loaded rows (no flicker, scroll preserved). It never
   * shrinks the loaded window, never blocks `loadMore`, and does not move the page counters.
   */
  refresh: () => Promise<void>;
  /** Optimistically drops items by key (after a successful delete action); they stay hidden for 30s of refreshes. */
  removeItems: (keys: ReadonlySet<ListKey>) => void;
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

function mergeUnique<T>(base: T[], incoming: T[], getKey: (item: T) => ListKey): T[] {
  const seen = new Set<ListKey>(base.map(getKey));
  const out = base.slice();
  for (const item of incoming) {
    const key = getKey(item);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
}

/** Keeps the previous object for rows whose content is unchanged so memoized rows do not re-render. */
function reuseUnchanged<T>(prev: readonly T[], next: readonly T[], getKey: (item: T) => ListKey): T[] {
  const prevByKey = new Map<ListKey, T>(prev.map((p) => [getKey(p), p]));
  return next.map((n) => {
    const old = prevByKey.get(getKey(n));
    return old !== undefined && JSON.stringify(old) === JSON.stringify(n) ? old : n;
  });
}

/** How long a removed key stays hidden from refresh/append merges, covering the server finishing the delete. */
export const TOMBSTONE_TTL_MS = 30_000;

/** Drops expired tombstones in place and returns the keys that are still hidden. */
export function liveTombstones(tombstones: Map<ListKey, number>, now: number): ReadonlySet<ListKey> {
  for (const [key, expiresAt] of tombstones) {
    if (expiresAt <= now) tombstones.delete(key);
  }
  return new Set(tombstones.keys());
}

export interface FirstPageMergeInput<T> {
  /** Rows currently loaded, in display order. */
  prev: readonly T[];
  /** Server total before and after this refresh. */
  prevTotal: number;
  /** Fresh page 1 from the server (tombstoned rows already removed) and the server total. */
  page: readonly T[];
  total: number;
  pageSize: number;
  getKey: (item: T) => ListKey;
}

/**
 * Merges a freshly fetched page 1 into the loaded window without ever shrinking it.
 *
 * - Page 1 rows come first in server order; a row whose content is unchanged keeps its previous object.
 * - Rows beyond the first `pageSize` positions are the tail: kept as they are, except that a row which page 1 now
 *   reports (it moved up) is taken from page 1 only.
 * - A row inside the refreshed range that page 1 no longer reports is either gone or pushed past page 1 by new rows.
 *   Row counts tell them apart: rows lost = prevTotal + newRows - total. It is dropped only when that count covers
 *   every such row (so a plain insert never loses a row that was merely pushed down); in an ambiguous mix of
 *   inserts and deletes it is kept until the next reload instead of risking a wrong drop.
 */
export function mergeFirstPage<T>({ prev, prevTotal, page, total, pageSize, getKey }: FirstPageMergeInput<T>): T[] {
  const freshKeys = new Set<ListKey>(page.map(getKey));
  const prevKeys = new Set<ListKey>(prev.map(getKey));
  const range = prev.slice(0, pageSize);
  const tail = prev.slice(pageSize).filter((item) => !freshKeys.has(getKey(item)));
  const newRows = page.reduce((n, item) => n + (prevKeys.has(getKey(item)) ? 0 : 1), 0);
  const unreported = range.filter((item) => !freshKeys.has(getKey(item)));
  const rowsLost = Math.max(0, prevTotal + newRows - total);
  const keptUnreported = unreported.length > rowsLost ? unreported : [];
  return [...reuseUnchanged(prev, page, getKey), ...keptUnreported, ...tail];
}

/**
 * Lazy-loaded paged list. Pages append as `loadMore` is called (typically by an IntersectionObserver
 * sentinel). Changing sort, filters or page size resets the list; in-flight requests are deduped and
 * aborted on reset and unmount.
 */
export function useInfiniteList<T>(
  fetchPage: FetchPage<T>,
  options: UseInfiniteListOptions<T>
): UseInfiniteListReturn<T> {
  const {
    pageSize = 50,
    sortKey,
    sortDir,
    filters,
    getKey,
    enabled = true,
  } = options;

  const [items, setItems] = useState<T[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState<boolean>(false);
  const [mode, setMode] = useState<string | null>(null);
  const [resetNonce, setResetNonce] = useState<number>(0);

  // Latest callbacks/params live in refs so a new closure identity never resets the list.
  const fetchRef = useRef<FetchPage<T>>(fetchPage);
  const getKeyRef = useRef<(item: T) => ListKey>(getKey);
  fetchRef.current = fetchPage;
  getKeyRef.current = getKey;

  const filtersJson = JSON.stringify(filters ?? {});
  const paramsRef = useRef({ pageSize, sortKey, sortDir, filtersJson });
  paramsRef.current = { pageSize, sortKey, sortDir, filtersJson };

  const controllerRef = useRef<AbortController | null>(null);
  const inFlightRef = useRef<boolean>(false);
  const refreshingRef = useRef<boolean>(false);
  const tombstonesRef = useRef<Map<ListKey, number>>(new Map());
  const totalRef = useRef<number>(0);
  const generationRef = useRef<number>(0);
  const pagesLoadedRef = useRef<number>(0);
  const hasMoreRef = useRef<boolean>(false);
  const itemsRef = useRef<T[]>([]);
  itemsRef.current = items;

  const requestPage = useCallback(
    (page: number, signal: AbortSignal): Promise<FetchPageResult<T>> => {
      const p = paramsRef.current;
      return fetchRef.current({
        page,
        pageSize: p.pageSize,
        sortKey: p.sortKey,
        sortDir: p.sortDir,
        filters: JSON.parse(p.filtersJson) as Record<string, string>,
        signal,
      });
    },
    []
  );

  const loadNext = useCallback(async (): Promise<void> => {
    const controller = controllerRef.current;
    if (!controller || inFlightRef.current) return;
    if (pagesLoadedRef.current > 0 && !hasMoreRef.current) return;
    const generation = generationRef.current;
    const page = pagesLoadedRef.current + 1;
    inFlightRef.current = true;
    setLoading(true);
    setError(null);
    try {
      const res = await requestPage(page, controller.signal);
      if (generation !== generationRef.current) return;
      const hidden = liveTombstones(tombstonesRef.current, Date.now());
      const visible = res.items.filter((item) => !hidden.has(getKeyRef.current(item)));
      pagesLoadedRef.current = page;
      const total = Math.max(0, res.total - (res.items.length - visible.length));
      const more = res.items.length > 0 && page * paramsRef.current.pageSize < res.total;
      hasMoreRef.current = more;
      totalRef.current = total;
      setHasMore(more);
      setTotal(total);
      setMode(res.mode ?? null);
      setItems((prev) => mergeUnique(prev, visible, getKeyRef.current));
    } catch (err: unknown) {
      if (isAbort(err) || generation !== generationRef.current) return;
      setError(errorMessage(err, 'Failed to load list'));
    } finally {
      if (generation === generationRef.current) {
        inFlightRef.current = false;
        setLoading(false);
      }
    }
  }, [requestPage]);

  // Reset + first page on sort/filter/pageSize change, enable toggle, or reload().
  useEffect(() => {
    generationRef.current += 1;
    controllerRef.current?.abort();
    inFlightRef.current = false;
    refreshingRef.current = false;
    pagesLoadedRef.current = 0;
    hasMoreRef.current = false;
    totalRef.current = 0;
    setItems([]);
    setTotal(0);
    setHasMore(false);
    setError(null);
    setLoading(false);
    if (!enabled) {
      controllerRef.current = null;
      return undefined;
    }
    const controller = new AbortController();
    controllerRef.current = controller;
    void loadNext();
    return () => {
      controller.abort();
    };
  }, [sortKey, sortDir, filtersJson, pageSize, enabled, resetNonce, loadNext]);

  const reload = useCallback(() => setResetNonce((n) => n + 1), []);

  const loadMore = useCallback(() => {
    void loadNext();
  }, [loadNext]);

  const refresh = useCallback(async (): Promise<void> => {
    const controller = controllerRef.current;
    // Independent of `inFlightRef`: a refresh never blocks loadMore, and loadMore never blocks a refresh.
    if (!controller || refreshingRef.current || pagesLoadedRef.current === 0) return;
    const generation = generationRef.current;
    refreshingRef.current = true;
    try {
      const res = await requestPage(1, controller.signal);
      if (generation !== generationRef.current) return;
      const hidden = liveTombstones(tombstonesRef.current, Date.now());
      const page = res.items.filter((item) => !hidden.has(getKeyRef.current(item)));
      const total = Math.max(0, res.total - (res.items.length - page.length));
      setMode(res.mode ?? null);
      setError(null);
      // An empty page for a non-empty list is not evidence of anything; only a total of 0 empties the list.
      if (res.items.length === 0 && total > 0) return;
      const prevTotal = totalRef.current;
      totalRef.current = total;
      setTotal(total);
      const more = pagesLoadedRef.current * paramsRef.current.pageSize < total;
      hasMoreRef.current = more;
      setHasMore(more);
      setItems((prev) =>
        total === 0
          ? []
          : mergeFirstPage({
              prev,
              prevTotal,
              page,
              total,
              pageSize: paramsRef.current.pageSize,
              getKey: getKeyRef.current,
            })
      );
    } catch (err: unknown) {
      if (isAbort(err) || generation !== generationRef.current) return;
      setError(errorMessage(err, 'Failed to refresh list'));
    } finally {
      if (generation === generationRef.current) refreshingRef.current = false;
    }
  }, [requestPage]);

  const removeItems = useCallback((keys: ReadonlySet<ListKey>) => {
    if (keys.size === 0) return;
    const expiresAt = Date.now() + TOMBSTONE_TTL_MS;
    for (const key of keys) tombstonesRef.current.set(key, expiresAt);
    const before = itemsRef.current;
    const after = before.filter((item) => !keys.has(getKeyRef.current(item)));
    const removed = before.length - after.length;
    const total = Math.max(0, totalRef.current - removed);
    totalRef.current = total;
    const more = pagesLoadedRef.current > 0 && pagesLoadedRef.current * paramsRef.current.pageSize < total;
    hasMoreRef.current = more;
    setHasMore(more);
    setItems(after);
    setTotal(total);
  }, []);

  return { items, total, loading, error, hasMore, mode, reload, loadMore, refresh, removeItems };
}

/** Adapts a `{mode, total, records}` list endpoint (phase 3 contract) to `FetchPage`. */
export function pagedFetcher<T>(
  endpoint: (q: ListQuery, signal?: AbortSignal) => Promise<PagedResponse<T>>
): FetchPage<T> {
  return async (req) => {
    const res = await endpoint(
      { page: req.page, pageSize: req.pageSize, sortKey: req.sortKey, sortDir: req.sortDir, filters: req.filters },
      req.signal
    );
    return { items: res.records, total: res.total, mode: res.mode };
  };
}
