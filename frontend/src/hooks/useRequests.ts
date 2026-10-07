import { useState, useEffect, useCallback } from 'react';
import type { RequestItem, UserQuota } from '@/types/models';
import type { Schema } from '@/types/apiSchema';
import {
  getRequests,
  getUserQuota,
  createRequest as apiCreateRequest,
  approveRequest as apiApproveRequest,
  rejectRequest as apiRejectRequest,
  deleteRequest as apiDeleteRequest,
  retryRequest as apiRetryRequest,
} from '@/services/requestService';

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
  requests: RequestItem[];
  quota: UserQuota | null;
  filter: RequestFilter;
  isLoading: boolean;
  error: string | null;
  setFilter: (filter: RequestFilter) => void;
  submitRequest: (item: RequestableItem) => Promise<void>;
  approve: (id: string) => Promise<void>;
  reject: (id: string, reason?: string) => Promise<void>;
  remove: (id: string) => Promise<void>;
  retry: (id: string) => Promise<Schema<'RetryResult'>>;
  refresh: () => Promise<void>;
}

/** `enabled` must stay false until the user is signed in: every request route needs a session. */
export function useRequests(enabled: boolean = true): UseRequestsReturn {
  const [requests, setRequests] = useState<RequestItem[]>([]);
  const [quota, setQuota] = useState<UserQuota | null>(null);
  const [filter, setFilter] = useState<RequestFilter>('all');
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const [reqData, quotaData] = await Promise.all([
        getRequests(filter),
        getUserQuota().catch(() => null),
      ]);
      setRequests(reqData);
      if (quotaData) setQuota(quotaData);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to load requests';
      setError(msg);
    } finally {
      setIsLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    if (!enabled) return;
    void refresh();
  }, [enabled, refresh]);

  const submitRequest = useCallback(
    async (item: RequestableItem) => {
      setError(null);
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
        setError(msg);
        throw err;
      }
    },
    [refresh]
  );

  const approve = useCallback(
    async (id: string) => {
      setError(null);
      try {
        await apiApproveRequest(id);
        await refresh();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Failed to approve request';
        setError(msg);
        throw err;
      }
    },
    [refresh]
  );

  const reject = useCallback(
    async (id: string, reason?: string) => {
      setError(null);
      try {
        await apiRejectRequest(id, reason);
        await refresh();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Failed to reject request';
        setError(msg);
        throw err;
      }
    },
    [refresh]
  );

  const remove = useCallback(
    async (id: string) => {
      setError(null);
      try {
        await apiDeleteRequest(id);
        await refresh();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : 'Failed to delete request';
        setError(msg);
        throw err;
      }
    },
    [refresh]
  );

  const retry = useCallback(
    async (id: string) => {
      setError(null);
      try {
        return await apiRetryRequest(id);
      } finally {
        await refresh();
      }
    },
    [refresh]
  );

  return {
    requests,
    quota,
    filter,
    isLoading,
    error,
    setFilter,
    submitRequest,
    approve,
    reject,
    remove,
    retry,
    refresh,
  };
}
