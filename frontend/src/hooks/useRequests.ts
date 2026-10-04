import { useState, useEffect, useCallback } from 'react';
import type { RequestItem, UserQuota, DiscoveryItem } from '@/types/models';
import {
  getRequests,
  getUserQuota,
  createRequest as apiCreateRequest,
  approveRequest as apiApproveRequest,
  rejectRequest as apiRejectRequest,
  deleteRequest as apiDeleteRequest,
} from '@/services/requestService';

export type RequestFilter = 'all' | 'pending' | 'approved' | 'fulfilled' | 'rejected';

export interface UseRequestsReturn {
  requests: RequestItem[];
  quota: UserQuota | null;
  filter: RequestFilter;
  isLoading: boolean;
  error: string | null;
  setFilter: (filter: RequestFilter) => void;
  submitRequest: (item: DiscoveryItem | { title: string; artist: string; album?: string; cover_url?: string; type?: string }) => Promise<void>;
  approve: (id: number) => Promise<void>;
  reject: (id: number, reason?: string) => Promise<void>;
  remove: (id: number) => Promise<void>;
  refresh: () => Promise<void>;
}

export function useRequests(): UseRequestsReturn {
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
    refresh();
  }, [refresh]);

  const submitRequest = useCallback(
    async (item: DiscoveryItem | { title: string; artist: string; album?: string; cover_url?: string; type?: string }) => {
      setError(null);
      try {
        await apiCreateRequest({
          title: item.title,
          artist: item.artist,
          album: item.album,
          cover_url: item.cover_url,
          type: item.type || 'album',
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
    async (id: number) => {
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
    async (id: number, reason?: string) => {
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
    async (id: number) => {
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
    refresh,
  };
}
