import { useState, useEffect, useCallback } from 'react';
import type { LidarrOptions } from '@/types/models';
import { getLidarrOptions } from '@/services/settingsService';
import { errorMessage } from '@/services/apiClient';

export interface UseLidarrOptionsReturn {
  options: LidarrOptions | null;
  isLoading: boolean;
  /** Server message (e.g. redacted 502 text) when Lidarr could not be queried. */
  error: string | null;
  refresh: () => Promise<void>;
}

/** Live Lidarr dropdown data. Only fetches while `enabled` (admin + a Lidarr URL is configured). */
export function useLidarrOptions(enabled: boolean): UseLidarrOptionsReturn {
  const [options, setOptions] = useState<LidarrOptions | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!enabled) return;
    setIsLoading(true);
    setError(null);
    try {
      setOptions(await getLidarrOptions());
    } catch (err: unknown) {
      setOptions(null);
      setError(errorMessage(err, 'Could not load options from Lidarr'));
    } finally {
      setIsLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { options, isLoading, error, refresh };
}
