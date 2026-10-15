import { useState, useEffect, useCallback, useMemo } from 'react';
import type { RequestItem, UserQuota } from '@/types/models';
import type { Schema } from '@/types/apiSchema';
import {
  getRequests,
  getRequestSources,
  getUserQuota,
  createRequest as apiCreateRequest,
  approveRequest as apiApproveRequest,
  rejectRequest as apiRejectRequest,
  deleteRequest as apiDeleteRequest,
  retryRequest as apiRetryRequest,
} from '@/services/requestService';
import { useVirtualPagedList, type FetchPage, type ListKey, type VirtualPagedList } from './useVirtualPagedList';

export type RequestFilter = 'all' | 'pending' | 'approved' | 'fulfilled' | 'rejected';

/** What a single-item request needs; a DiscoveryItem satisfies it. */
export interface RequestableItem {
  id?: string;
  title: string;
  artist: string;
  album?: string;
  cover_url?: string;
  type?: string;
  preview_url?: string;
  release_date?: string;
}

export interface UseRequestsReturn {
  list: VirtualPagedList<RequestItem>;
  requests: RequestItem[];
  quota: UserQuota | null;
  filter: RequestFilter;
  source: string;
  sources: Schema<'RequestSourceCount'>[];
  isLoading: boolean;
  error: string | null;
  total: number;
  setFilter: (filter: RequestFilter) => void;
  setSource: (source: string) => void;
  submitRequest: (item: RequestableItem) => Promise<void>;
  approve: (id: string) => Promise<void>;
  reject: (id: string, reason?: string) => Promise<void>;
  remove: (id: string) => Promise<void>;
  retry: (id: string) => Promise<Schema<'RetryResult'>>;
  refresh: () => Promise<void>;
}

const getKey = (item: RequestItem): ListKey => item.id;

/** `enabled` must stay false until the user is signed in: every request route needs a session. */
export function useRequests(enabled: boolean = true): UseRequestsReturn {
  const [quota, setQuota] = useState<UserQuota | null>(null);
  const [filter, setFilter] = useState<RequestFilter>('all');
  const [source, setSource] = useState<string>('all');
  const [sources, setSources] = useState<Schema<'RequestSourceCount'>[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);

  const fetchPage = useCallback<FetchPage<RequestItem>>(
    async (req) => {
      const res = await getRequests(
        {
          status: req.filters.status as string | undefined,
          source: req.filters.source as string | undefined,
          page: req.page,
          pageSize: req.pageSize,
        },
        req.signal
      );
      return {
        items: res.requests || [],
        total: res.total ?? res.count ?? 0,
      };
    },
    []
  );

  const filters = useMemo<Record<string, string>>(() => {
    const f: Record<string, string> = {};
    if (filter && filter !== 'all') f.status = filter;
    if (source && source !== 'all') f.source = source;
    return f;
  }, [filter, source]);

  const list = useVirtualPagedList<RequestItem>(fetchPage, {
    sortKey: 'created_at',
    sortDir: 'desc',
    filters,
    getKey,
    enabled,
  });

  const loadSources = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await getRequestSources(signal);
      setSources(res.sources || []);
    } catch {
      // Background count fetch failure shouldn't crash the list
    }
  }, []);

  const loadQuota = useCallback(async () => {
    try {
      const quotaData = await getUserQuota();
      if (quotaData) setQuota(quotaData);
    } catch {
      // Ignore quota fetch error for unauthenticated/exempt
    }
  }, []);

  const refresh = useCallback(async () => {
    setActionError(null);
    const pList = list.total === 0 ? (list.reload(), Promise.resolve()) : list.refresh();
    await Promise.all([
      pList,
      loadSources(),
      loadQuota(),
    ]);
  }, [list, loadSources, loadQuota]);

  useEffect(() => {
    if (!enabled) return;
    void loadSources();
    void loadQuota();
  }, [enabled, loadSources, loadQuota]);

  const submitRequest = useCallback(
    async (item: RequestableItem) => {
      setActionError(null);
      try {
        await apiCreateRequest({
          title: item.title,
          artist: item.artist,
          album: item.album,
          cover_url: item.cover_url,
          item_type: item.type === 'track' ? 'track' : 'album',
          foreign_id: item.id,
          preview_url: item.preview_url,
          release_date: item.release_date,
        });
        await refresh();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Failed to submit request';
        setActionError(msg);
        throw err;
      }
    },
    [refresh]
  );

  const approve = useCallback(
    async (id: string) => {
      setActionError(null);
      try {
        await apiApproveRequest(id);
        if (filter === 'pending') {
          list.removeItems(new Set([id]));
        } else {
          await list.refresh();
        }
        await Promise.all([loadSources(), loadQuota()]);
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Failed to approve request';
        setActionError(msg);
        throw err;
      }
    },
    [filter, list, loadSources, loadQuota]
  );

  const reject = useCallback(
    async (id: string, reason?: string) => {
      setActionError(null);
      try {
        await apiRejectRequest(id, reason);
        if (filter === 'pending') {
          list.removeItems(new Set([id]));
        } else {
          await list.refresh();
        }
        await Promise.all([loadSources(), loadQuota()]);
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Failed to reject request';
        setActionError(msg);
        throw err;
      }
    },
    [filter, list, loadSources, loadQuota]
  );

  const remove = useCallback(
    async (id: string) => {
      setActionError(null);
      try {
        await apiDeleteRequest(id);
        list.removeItems(new Set([id]));
        await Promise.all([loadSources(), loadQuota()]);
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Failed to delete request';
        setActionError(msg);
        throw err;
      }
    },
    [list, loadSources, loadQuota]
  );

  const retry = useCallback(
    async (id: string) => {
      setActionError(null);
      try {
        return await apiRetryRequest(id);
      } finally {
        await Promise.all([list.refresh(), loadSources()]);
      }
    },
    [list, loadSources]
  );

  return {
    list,
    requests: list.getLoadedItems(),
    quota,
    filter,
    source,
    sources,
    isLoading: list.loading,
    error: actionError || list.error,
    total: list.total,
    setFilter,
    setSource,
    submitRequest,
    approve,
    reject,
    remove,
    retry,
    refresh,
  };
}
