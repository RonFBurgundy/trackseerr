import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, errorMessage } from '@/services/apiClient';
import { grabRelease, searchReleases } from '@/services/acquisitionService';
import type {
  IndexerSearchError,
  ManualSearchFilter,
  ManualSearchSort,
  ManualSearchTarget,
  SearchRelease,
} from '@/types/manualSearch';

export interface UseManualSearchReturn {
  /** Filtered and sorted view of the releases. */
  results: SearchRelease[];
  /** Everything the server returned, unfiltered. */
  total: number;
  profileName: string;
  indexerErrors: IndexerSearchError[];
  loading: boolean;
  error: string | null;
  filter: ManualSearchFilter;
  setFilter: (f: ManualSearchFilter) => void;
  sort: ManualSearchSort;
  setSort: (s: ManualSearchSort) => void;
  grab: (release: SearchRelease) => Promise<void>;
  busyIds: ReadonlySet<string>;
  grabbedIds: ReadonlySet<string>;
  rerun: () => void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Reads the optional per-indexer failure list defensively; entries that are not `{source, error}` are dropped. */
function readIndexerErrors(body: unknown): IndexerSearchError[] {
  if (!isRecord(body)) return [];
  const raw = body.indexer_errors;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item): IndexerSearchError[] =>
    isRecord(item) && typeof item.source === 'string' && typeof item.error === 'string'
      ? [{ source: item.source, error: item.error }]
      : []
  );
}

function publishedMs(r: SearchRelease): number {
  if (!r.publish_date) return 0;
  const t = Date.parse(r.publish_date);
  return Number.isNaN(t) ? 0 : t;
}

const SORTERS: Record<Exclude<ManualSearchSort, 'default'>, (a: SearchRelease, b: SearchRelease) => number> = {
  score: (a, b) => b.score - a.score,
  size: (a, b) => b.size_bytes - a.size_bytes,
  seeders: (a, b) => (b.seeders ?? -1) - (a.seeders ?? -1),
  age: (a, b) => publishedMs(b) - publishedMs(a),
};

/**
 * Interactive release search for one target. Runs when the target becomes non-null (aborting on close, target change
 * or unmount), and exposes filtering, sorting, re-run and per-release grabbing. `onGrabbed` fires after each success.
 */
export function useManualSearch(
  target: ManualSearchTarget | null,
  onToast: (msg: string, tone?: 'ok' | 'error') => void,
  onGrabbed?: () => void
): UseManualSearchReturn {
  const [releases, setReleases] = useState<SearchRelease[]>([]);
  const [profileName, setProfileName] = useState<string>('');
  const [indexerErrors, setIndexerErrors] = useState<IndexerSearchError[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<ManualSearchFilter>('all');
  const [sort, setSort] = useState<ManualSearchSort>('default');
  const [busyIds, setBusyIds] = useState<ReadonlySet<string>>(new Set());
  const [grabbedIds, setGrabbedIds] = useState<ReadonlySet<string>>(new Set());
  const [runId, setRunId] = useState<number>(0);
  const onGrabbedRef = useRef(onGrabbed);
  onGrabbedRef.current = onGrabbed;

  useEffect(() => {
    setReleases([]);
    setProfileName('');
    setIndexerErrors([]);
    setError(null);
    setBusyIds(new Set());
    setGrabbedIds(new Set());
    setFilter('all');
    setSort('default');
    if (!target) {
      setLoading(false);
      return undefined;
    }
    const controller = new AbortController();
    setLoading(true);
    searchReleases(
      {
        artist: target.artist,
        album: target.album ?? null,
        title: target.title ?? null,
        item_type: target.itemType,
        album_id: target.albumId != null ? String(target.albumId) : null,
        track_id: target.trackId != null ? String(target.trackId) : null,
      },
      controller.signal
    )
      .then((res) => {
        if (controller.signal.aborted) return;
        setReleases(res.results);
        setProfileName(res.profile_name);
        setIndexerErrors(readIndexerErrors(res));
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(errorMessage(err, 'Search failed'));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [target, runId]);

  const results = useMemo<SearchRelease[]>(() => {
    const filtered =
      filter === 'all' ? releases : releases.filter((r) => (filter === 'accepted' ? r.is_acceptable : !r.is_acceptable));
    if (sort === 'default') return filtered;
    return [...filtered].sort(SORTERS[sort]);
  }, [releases, filter, sort]);

  const grab = useCallback(
    async (release: SearchRelease): Promise<void> => {
      if (!target) return;
      setBusyIds((prev) => new Set(prev).add(release.id));
      try {
        const res = await grabRelease({
          release,
          artist: target.artist,
          title: target.title ?? target.album ?? '',
          album: target.album ?? null,
          item_type: target.itemType,
          album_id: target.albumId != null ? String(target.albumId) : null,
          track_id: target.trackId != null ? String(target.trackId) : null,
        });
        setGrabbedIds((prev) => new Set(prev).add(release.id));
        onToast(`Sent to ${res.client || 'download client'}`);
        onGrabbedRef.current?.();
      } catch (err: unknown) {
        const fallback = err instanceof ApiError && err.status === 409 ? 'Manual grabs need native mode' : 'Grab failed';
        onToast(errorMessage(err, fallback), 'error');
      } finally {
        setBusyIds((prev) => {
          const next = new Set(prev);
          next.delete(release.id);
          return next;
        });
      }
    },
    [target, onToast]
  );

  const rerun = useCallback(() => setRunId((n) => n + 1), []);

  return {
    results,
    total: releases.length,
    profileName,
    indexerErrors,
    loading,
    error,
    filter,
    setFilter,
    sort,
    setSort,
    grab,
    busyIds,
    grabbedIds,
    rerun,
  };
}
