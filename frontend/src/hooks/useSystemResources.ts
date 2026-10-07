import { useCallback, useEffect, useRef, useState } from 'react';
import type { SystemResources } from '@/types/models';
import { getSystemResources } from '@/services/systemService';
import { errorMessage } from '@/services/apiClient';
import { usePolling } from './usePolling';

const RESOURCES_POLL_MS = 5000;

export interface UseSystemResourcesReturn {
  resources: SystemResources | null;
  error: string | null;
}

/** Process CPU / memory / thread / uptime gauge, polled while mounted and the tab is visible. */
export function useSystemResources(): UseSystemResourcesReturn {
  const [resources, setResources] = useState<SystemResources | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef<boolean>(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async (): Promise<void> => {
    try {
      const next = await getSystemResources();
      if (!mounted.current) return;
      setResources(next);
      setError(null);
    } catch (err: unknown) {
      if (mounted.current) setError(errorMessage(err, 'Failed to load process resources'));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  usePolling(() => void load(), RESOURCES_POLL_MS);

  return { resources, error };
}
