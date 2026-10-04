import { useState, useEffect, useCallback } from 'react';
import type { SystemQueueResponse } from '@/types/models';
import { getSystemQueue } from '@/services/systemService';
import { errorMessage } from '@/services/apiClient';

const POLL_MS = 3000;

export interface UseSystemQueueReturn {
  queue: SystemQueueResponse | null;
  isLoading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

/** Polls the job queue every few seconds while the consuming component is mounted and the tab is visible. */
export function useSystemQueue(): UseSystemQueueReturn {
  const [queue, setQueue] = useState<SystemQueueResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setQueue(await getSystemQueue());
      setError(null);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to load job queue'));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      if (!document.hidden) void refresh();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  return { queue, isLoading, error, refresh };
}
