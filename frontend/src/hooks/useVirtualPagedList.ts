import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ListQuery, ListSortDir, PagedResponse } from '@/types/activity';
import { errorMessage } from '@/services/apiClient';

export type ListKey = string | number;

export interface FetchPageRequest {
  /** 1-based page number. */
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

export interface UseVirtualPagedListOptions<T> {
  pageSize?: number;
  sortKey: string;
  sortDir: ListSortDir;
  filters?: Readonly<Record<string, string>>;
  /** Stable identity per item; used for tombstones and to keep unchanged row objects across refreshes. */
  getKey: (item: T) => ListKey;
  /** When false nothing is fetched (e.g. non-admin). Default true. */
  enabled?: boolean;
  /** Pages kept in the sparse cache (LRU); pages in the visible range are never evicted. Default 20. */
  maxCachedPages?: number;
}

export type ScrollToIndexFn = (index: number) => void;

export interface VirtualPagedList<T> {
  /** Known total (after tombstones). 0 until the first page arrives. */
  total: number;
  /** True while a user-driven page request (initial load, scroll into an unloaded range) is in flight. */
  loading: boolean;
  /** Last failed load. Rows whose page failed stay placeholders until `retry` or `reload`. */
  error: string | null;
  mode: string | null;
  /** Bumps on every cache change; consumers re-read `getItem` when it changes. */
  version: number;
  /** Bumps whenever the list is reset (sort/filter/pageSize/enabled change, `reload`); views scroll back to the top. */
  generation: number;
  /** The loaded item at `index`, or `undefined` for a placeholder. */
  getItem: (index: number) => T | undefined;
  /** All currently loaded items in index order (selection over loaded rows). */
  getLoadedItems: () => T[];
  /** Declares the visible (+overscan) index range; fetches the missing pages (deduped, abortable, debounced while scrubbing). */
  ensureRange: (startIndex: number, endIndex: number) => void;
  /** Jumps to an absolute item index through the view bound with `bindScroller`. */
  scrollToOffset: (index: number) => void;
  /** Called by the view that owns the virtualizer so `scrollToOffset` can reach it. Returns an unbind function. */
  bindScroller: (fn: ScrollToIndexFn) => () => void;
  /** Silently re-fetches the visible pages in place (polling). Never shows placeholders or flicker. */
  refresh: () => Promise<void>;
  /** Optimistically hides items by key, then re-fetches the visible pages so later rows do not skip. */
  removeItems: (keys: ReadonlySet<ListKey>) => void;
  /** Discards everything and reloads from page 1. */
  reload: () => void;
  /** Re-requests pages that failed, for the current range. */
  retry: () => void;
}

const DEFAULT_PAGE_SIZE = 50;
const DEFAULT_MAX_CACHED_PAGES = 20;
/** Debounce for requesting newly visible pages, so dragging a scrubber does not fetch every page it crosses. */
const RANGE_DEBOUNCE_MS = 70;

/** How long a removed key stays hidden from page results, covering the server finishing the delete. */
export const TOMBSTONE_TTL_MS = 30_000;

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

/** Drops expired tombstones in place and returns the keys that are still hidden. */
export function liveTombstones(tombstones: Map<ListKey, number>, now: number): ReadonlySet<ListKey> {
  for (const [key, expiresAt] of tombstones) {
    if (expiresAt <= now) tombstones.delete(key);
  }
  return new Set(tombstones.keys());
}

/** Keeps the previous object for rows whose content is unchanged so memoized rows do not re-render. */
export function reuseUnchanged<T>(prev: readonly T[], next: readonly T[], getKey: (item: T) => ListKey): T[] {
  const prevByKey = new Map<ListKey, T>(prev.map((p) => [getKey(p), p]));
  return next.map((n) => {
    const old = prevByKey.get(getKey(n));
    return old !== undefined && JSON.stringify(old) === JSON.stringify(n) ? old : n;
  });
}

interface CachedPage<T> {
  items: T[];
}

interface InFlight {
  controller: AbortController;
  token: number;
  silent: boolean;
}

/**
 * Virtualized, sparsely paged list with a known total.
 *
 * Pages are fetched on demand for the range the view reports through `ensureRange`; unloaded indexes read as
 * `undefined` (placeholders). The cache is an LRU of whole pages. A generation counter discards responses that
 * were requested before a sort/filter change, and a per-page token discards superseded requests of the same page.
 */
export function useVirtualPagedList<T>(
  fetchPage: FetchPage<T>,
  options: UseVirtualPagedListOptions<T>
): VirtualPagedList<T> {
  const {
    pageSize = DEFAULT_PAGE_SIZE,
    sortKey,
    sortDir,
    filters,
    getKey,
    enabled = true,
    maxCachedPages = DEFAULT_MAX_CACHED_PAGES,
  } = options;

  const [total, setTotal] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<string | null>(null);
  const [version, setVersion] = useState<number>(0);
  const [generation, setGeneration] = useState<number>(0);
  const [resetNonce, setResetNonce] = useState<number>(0);

  // Latest callbacks/params live in refs so a new closure identity never resets the list.
  const fetchRef = useRef<FetchPage<T>>(fetchPage);
  const getKeyRef = useRef<(item: T) => ListKey>(getKey);
  fetchRef.current = fetchPage;
  getKeyRef.current = getKey;

  const filtersJson = JSON.stringify(filters ?? {});
  const paramsRef = useRef({ pageSize, sortKey, sortDir, filtersJson, maxCachedPages });
  paramsRef.current = { pageSize, sortKey, sortDir, filtersJson, maxCachedPages };

  /** Cache key includes the query so a page can never be read under a different sort/filter. */
  const cacheRef = useRef<Map<string, CachedPage<T>>>(new Map());
  const inflightRef = useRef<Map<number, InFlight>>(new Map());
  const failedRef = useRef<Set<number>>(new Set());
  const tombstonesRef = useRef<Map<ListKey, number>>(new Map());
  const totalRef = useRef<number>(0);
  const generationRef = useRef<number>(0);
  const tokenRef = useRef<number>(0);
  const rangeRef = useRef<{ start: number; end: number } | null>(null);
  const rangeTimerRef = useRef<number | null>(null);
  const userPendingRef = useRef<number>(0);
  const scrollerRef = useRef<ScrollToIndexFn | null>(null);
  const enabledRef = useRef<boolean>(enabled);
  enabledRef.current = enabled;

  const queryKey = useCallback((): string => {
    const p = paramsRef.current;
    return `${p.sortKey}|${p.sortDir}|${p.filtersJson}|${p.pageSize}`;
  }, []);
  const pageCacheKey = useCallback((page: number): string => `${queryKey()}|${page}`, [queryKey]);

  const bump = useCallback((): void => setVersion((v) => v + 1), []);

  const visiblePages = useCallback((): Set<number> => {
    const pages = new Set<number>();
    const range = rangeRef.current;
    if (!range) return pages;
    const ps = paramsRef.current.pageSize;
    for (let p = Math.floor(range.start / ps); p <= Math.floor(range.end / ps); p += 1) pages.add(p);
    return pages;
  }, []);

  const evictLru = useCallback((): void => {
    const cache = cacheRef.current;
    const max = paramsRef.current.maxCachedPages;
    if (cache.size <= max) return;
    const keepPages = visiblePages();
    for (const key of cache.keys()) {
      if (cache.size <= max) break;
      const page = Number(key.slice(key.lastIndexOf('|') + 1));
      if (keepPages.has(page)) continue;
      cache.delete(key);
    }
  }, [visiblePages]);

  const setLoadingFlag = useCallback((): void => {
    setLoading(userPendingRef.current > 0);
  }, []);

  /** Drops every cached page except `keep` (alignment is no longer trustworthy after the total moved). */
  const dropOtherPages = useCallback(
    (keep: ReadonlySet<number>): void => {
      const cache = cacheRef.current;
      for (const key of Array.from(cache.keys())) {
        const page = Number(key.slice(key.lastIndexOf('|') + 1));
        if (!keep.has(page)) cache.delete(key);
      }
    },
    []
  );

  /**
   * Requests one page. `silent` requests (refresh, post-removal) never raise the loading flag or the error.
   * A non-forced request for a page already in flight is deduped; a forced one supersedes it.
   */
  const requestPage = useCallback(
    async (page: number, silent: boolean, force: boolean): Promise<void> => {
      if (!enabledRef.current) return;
      const existing = inflightRef.current.get(page);
      if (existing && !force) return;
      if (existing) existing.controller.abort();
      const generationAtStart = generationRef.current;
      const controller = new AbortController();
      tokenRef.current += 1;
      const token = tokenRef.current;
      inflightRef.current.set(page, { controller, token, silent });
      if (!silent) {
        userPendingRef.current += 1;
        setLoadingFlag();
        setError(null);
      }
      const p = paramsRef.current;
      const cacheKey = pageCacheKey(page);
      try {
        const res = await fetchRef.current({
          page: page + 1,
          pageSize: p.pageSize,
          sortKey: p.sortKey,
          sortDir: p.sortDir,
          filters: JSON.parse(p.filtersJson) as Record<string, string>,
          signal: controller.signal,
        });
        if (generationAtStart !== generationRef.current) return;
        const current = inflightRef.current.get(page);
        if (!current || current.token !== token) return;

        const hidden = liveTombstones(tombstonesRef.current, Date.now());
        const keyOf = getKeyRef.current;
        const visible = res.items.filter((item) => !hidden.has(keyOf(item)));
        const hiddenCount = res.items.length - visible.length;
        const newTotal = Math.max(0, res.total - hiddenCount);

        failedRef.current.delete(page);
        const prevPage = cacheRef.current.get(cacheKey);
        cacheRef.current.delete(cacheKey);
        cacheRef.current.set(cacheKey, {
          items: prevPage ? reuseUnchanged(prevPage.items, visible, keyOf) : visible,
        });

        const totalMoved = newTotal !== totalRef.current;
        if (totalMoved) {
          const wasKnown = totalRef.current > 0;
          totalRef.current = newTotal;
          setTotal(newTotal);
          if (wasKnown) {
            // Rows were inserted or deleted somewhere: every other cached page may be misaligned. Visible pages are
            // re-fetched in place; the rest are dropped and come back when scrolled to.
            const vis = visiblePages();
            const keep = new Set<number>([page]);
            dropOtherPages(keep);
            for (const vp of vis) {
              if (vp !== page && vp * p.pageSize < newTotal) void requestPage(vp, true, true);
            }
          }
        }
        if (res.mode !== undefined) setMode(res.mode ?? null);
        evictLru();
        bump();
      } catch (err: unknown) {
        if (isAbort(err) || generationAtStart !== generationRef.current) return;
        const current = inflightRef.current.get(page);
        if (!current || current.token !== token) return;
        if (!silent) {
          failedRef.current.add(page);
          setError(errorMessage(err, 'Failed to load list'));
        }
      } finally {
        const current = inflightRef.current.get(page);
        if (current && current.token === token) inflightRef.current.delete(page);
        if (!silent && generationAtStart === generationRef.current) {
          userPendingRef.current = Math.max(0, userPendingRef.current - 1);
          setLoadingFlag();
        }
      }
    },
    [bump, dropOtherPages, evictLru, pageCacheKey, setLoadingFlag, visiblePages]
  );

  // Reset + first page on sort/filter/pageSize change, enable toggle, or reload().
  useEffect(() => {
    generationRef.current += 1;
    for (const f of inflightRef.current.values()) f.controller.abort();
    inflightRef.current.clear();
    failedRef.current.clear();
    cacheRef.current.clear();
    userPendingRef.current = 0;
    rangeRef.current = null;
    if (rangeTimerRef.current !== null) {
      window.clearTimeout(rangeTimerRef.current);
      rangeTimerRef.current = null;
    }
    totalRef.current = 0;
    setTotal(0);
    setError(null);
    setLoading(false);
    setGeneration((g) => g + 1);
    bump();
    if (!enabled) return undefined;
    void requestPage(0, false, true);
    return () => {
      generationRef.current += 1;
      for (const f of inflightRef.current.values()) f.controller.abort();
      inflightRef.current.clear();
    };
  }, [sortKey, sortDir, filtersJson, pageSize, enabled, resetNonce, requestPage, bump]);

  useEffect(
    () => () => {
      if (rangeTimerRef.current !== null) window.clearTimeout(rangeTimerRef.current);
    },
    []
  );

  const getItem = useCallback(
    (index: number): T | undefined => {
      if (index < 0 || index >= totalRef.current) return undefined;
      const ps = paramsRef.current.pageSize;
      const page = cacheRef.current.get(pageCacheKey(Math.floor(index / ps)));
      return page?.items[index % ps];
    },
    [pageCacheKey]
  );

  const getLoadedItems = useCallback((): T[] => {
    const pages: Array<[number, CachedPage<T>]> = [];
    for (const [key, page] of cacheRef.current) {
      pages.push([Number(key.slice(key.lastIndexOf('|') + 1)), page]);
    }
    pages.sort((a, b) => a[0] - b[0]);
    return pages.flatMap(([, page]) => page.items);
  }, []);

  const fetchMissing = useCallback(
    (start: number, end: number): void => {
      const ps = paramsRef.current.pageSize;
      const totalNow = totalRef.current;
      if (totalNow <= 0) return;
      const lastIndex = Math.min(end, totalNow - 1);
      const first = Math.floor(Math.max(0, start) / ps);
      const last = Math.floor(lastIndex / ps);
      // Abort stale in-flight requests the user has scrolled away from (scrubbing across many pages).
      for (const [page, f] of inflightRef.current) {
        if (!f.silent && (page < first - 1 || page > last + 1)) {
          // The aborted request settles through its own `finally`, which releases its loading count.
          f.controller.abort();
          inflightRef.current.delete(page);
        }
      }
      for (let page = first; page <= last; page += 1) {
        const key = pageCacheKey(page);
        const cached = cacheRef.current.get(key);
        if (cached) {
          // Touch for LRU recency.
          cacheRef.current.delete(key);
          cacheRef.current.set(key, cached);
          continue;
        }
        if (failedRef.current.has(page) || inflightRef.current.has(page)) continue;
        void requestPage(page, false, false);
      }
    },
    [pageCacheKey, requestPage, setLoadingFlag]
  );

  const ensureRange = useCallback(
    (startIndex: number, endIndex: number): void => {
      if (!enabledRef.current) return;
      rangeRef.current = { start: Math.max(0, startIndex), end: Math.max(startIndex, endIndex) };
      const ps = paramsRef.current.pageSize;
      const first = Math.floor(rangeRef.current.start / ps);
      const last = Math.floor(Math.min(rangeRef.current.end, Math.max(0, totalRef.current - 1)) / ps);
      let missing = false;
      for (let page = first; page <= last; page += 1) {
        if (!cacheRef.current.has(pageCacheKey(page)) && !failedRef.current.has(page)) {
          missing = true;
          break;
        }
      }
      if (!missing) {
        fetchMissing(rangeRef.current.start, rangeRef.current.end);
        return;
      }
      if (rangeTimerRef.current !== null) window.clearTimeout(rangeTimerRef.current);
      rangeTimerRef.current = window.setTimeout(() => {
        rangeTimerRef.current = null;
        const range = rangeRef.current;
        if (range) fetchMissing(range.start, range.end);
      }, RANGE_DEBOUNCE_MS);
    },
    [fetchMissing, pageCacheKey]
  );

  const refresh = useCallback(async (): Promise<void> => {
    if (!enabledRef.current || totalRef.current <= 0) return;
    const vis = visiblePages();
    if (vis.size === 0) vis.add(0);
    const ps = paramsRef.current.pageSize;
    const jobs: Array<Promise<void>> = [];
    for (const page of vis) {
      if (page * ps >= totalRef.current) continue;
      // A user-driven request for the page is already fetching fresh data; do not double up.
      const existing = inflightRef.current.get(page);
      if (existing && !existing.silent) continue;
      jobs.push(requestPage(page, true, true));
    }
    await Promise.all(jobs);
  }, [requestPage, visiblePages]);

  const removeItems = useCallback(
    (keys: ReadonlySet<ListKey>): void => {
      if (keys.size === 0) return;
      const expiresAt = Date.now() + TOMBSTONE_TTL_MS;
      for (const key of keys) tombstonesRef.current.set(key, expiresAt);
      const keyOf = getKeyRef.current;
      let removed = 0;
      let firstAffected = Number.POSITIVE_INFINITY;
      for (const [cacheKey, page] of cacheRef.current) {
        const after = page.items.filter((item) => !keys.has(keyOf(item)));
        if (after.length !== page.items.length) {
          removed += page.items.length - after.length;
          firstAffected = Math.min(firstAffected, Number(cacheKey.slice(cacheKey.lastIndexOf('|') + 1)));
          cacheRef.current.set(cacheKey, { items: after });
        }
      }
      const nextTotal = Math.max(0, totalRef.current - removed);
      totalRef.current = nextTotal;
      setTotal(nextTotal);
      // Every row after the removed one moved up by one: pages from the first affected page onward are misaligned.
      // Visible ones stay on screen (stale) while they are re-fetched in place; the rest are dropped.
      const vis = visiblePages();
      for (const cacheKey of Array.from(cacheRef.current.keys())) {
        const page = Number(cacheKey.slice(cacheKey.lastIndexOf('|') + 1));
        if (page >= firstAffected && !vis.has(page)) cacheRef.current.delete(cacheKey);
      }
      bump();
      if (removed === 0) {
        // Not loaded locally, so nothing shifted visibly; just reconcile with the server.
        void refresh();
        return;
      }
      const ps = paramsRef.current.pageSize;
      for (const page of vis) {
        if (page >= firstAffected && page * ps < nextTotal) void requestPage(page, true, true);
      }
    },
    [bump, refresh, requestPage, visiblePages]
  );

  const reload = useCallback((): void => setResetNonce((n) => n + 1), []);

  const retry = useCallback((): void => {
    failedRef.current.clear();
    setError(null);
    if (totalRef.current === 0) {
      void requestPage(0, false, true);
      return;
    }
    const range = rangeRef.current;
    if (range) fetchMissing(range.start, range.end);
  }, [fetchMissing, requestPage]);

  const bindScroller = useCallback((fn: ScrollToIndexFn): (() => void) => {
    scrollerRef.current = fn;
    return () => {
      if (scrollerRef.current === fn) scrollerRef.current = null;
    };
  }, []);

  const scrollToOffset = useCallback((index: number): void => {
    const clamped = Math.max(0, Math.min(index, Math.max(0, totalRef.current - 1)));
    scrollerRef.current?.(clamped);
  }, []);

  return useMemo(
    () => ({
      total,
      loading,
      error,
      mode,
      version,
      generation,
      getItem,
      getLoadedItems,
      ensureRange,
      scrollToOffset,
      bindScroller,
      refresh,
      removeItems,
      reload,
      retry,
    }),
    [
      total,
      loading,
      error,
      mode,
      version,
      generation,
      getItem,
      getLoadedItems,
      ensureRange,
      scrollToOffset,
      bindScroller,
      refresh,
      removeItems,
      reload,
      retry,
    ]
  );
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
