import { useCallback, useEffect, useRef, useState } from 'react';
import type { SystemActivityRecent, SystemActivityRunning } from '@/types/models';
import { getSystemActivity } from '@/services/systemService';
import { errorMessage } from '@/services/apiClient';
import { usePolling } from './usePolling';

const ACTIVITY_POLL_MS = 5000;

export interface UseSystemActivityReturn {
  running: SystemActivityRunning[];
  recent: SystemActivityRecent[];
  error: string | null;
  refresh: () => Promise<void>;
}

/**
 * Background-task activity for the brand logo. Call once (in the header) and pass the result down so desktop and
 * mobile logos share one poll. When `enabled` is false (non-admins) nothing is requested.
 */
export function useSystemActivity(enabled: boolean): UseSystemActivityReturn {
  const [running, setRunning] = useState<SystemActivityRunning[]>([]);
  const [recent, setRecent] = useState<SystemActivityRecent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef<boolean>(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    try {
      const next = await getSystemActivity();
      if (!mounted.current) return;
      setRunning(next.running);
      setRecent(next.recent);
      setError(null);
    } catch (err: unknown) {
      if (mounted.current) setError(errorMessage(err, 'Failed to load task activity'));
    }
  }, [enabled]);

  useEffect(() => {
    if (!enabled) {
      setRunning([]);
      setRecent([]);
      return;
    }
    void refresh();
  }, [enabled, refresh]);
  usePolling(() => void refresh(), ACTIVITY_POLL_MS, enabled);

  return { running, recent, error, refresh };
}
