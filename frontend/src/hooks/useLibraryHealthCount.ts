import { useCallback, useEffect, useRef, useState } from 'react';
import { getLibraryHealthCount } from '@/services/libraryHealthService';
import { usePolling } from './usePolling';

/** Never poll faster than this; the count is a nav badge, not a live feed. */
export const LIBRARY_HEALTH_COUNT_INTERVAL_MS = 60_000;

export interface UseLibraryHealthCountReturn {
  count: number;
  /** Re-reads the count now (call after actions that change findings). */
  refresh: () => Promise<void>;
}

/** Admin-only badge count of library-health findings, refreshed every 60 s and on demand. */
export function useLibraryHealthCount(enabled: boolean): UseLibraryHealthCountReturn {
  const [count, setCount] = useState<number>(0);
  const controllerRef = useRef<AbortController | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      const res = await getLibraryHealthCount(controller.signal);
      if (!controller.signal.aborted) setCount(res.count);
    } catch (err: unknown) {
      // A failed badge refresh keeps the last known count; aborts are expected.
      if (!(err instanceof DOMException && err.name === 'AbortError')) {
        console.warn('Library health count refresh failed', err);
      }
    }
  }, [enabled]);

  useEffect(() => {
    if (!enabled) {
      setCount(0);
      return undefined;
    }
    void refresh();
    return () => controllerRef.current?.abort();
  }, [enabled, refresh]);

  usePolling(() => void refresh(), LIBRARY_HEALTH_COUNT_INTERVAL_MS, enabled);

  return { count, refresh };
}
