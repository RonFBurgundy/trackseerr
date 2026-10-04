import { useState, useEffect, useCallback } from 'react';
import type { LidarrDefaults } from '@/types/models';
import { getLidarrDefaults } from '@/services/settingsService';
import { errorMessage } from '@/services/apiClient';

export interface UseLidarrDefaultsReturn {
  defaults: LidarrDefaults | null;
  isLoading: boolean;
  /** Server message (e.g. redacted 502 text) when Lidarr could not be queried. */
  error: string | null;
  refresh: () => Promise<void>;
}

/** Root-folder defaults Lidarr itself reports (read-only). Only fetches while `enabled` (a Lidarr URL is saved). */
export function useLidarrDefaults(enabled: boolean): UseLidarrDefaultsReturn {
  const [defaults, setDefaults] = useState<LidarrDefaults | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!enabled) return;
    setIsLoading(true);
    setError(null);
    try {
      setDefaults(await getLidarrDefaults());
    } catch (err: unknown) {
      setDefaults(null);
      setError(errorMessage(err, 'Could not load defaults from Lidarr'));
    } finally {
      setIsLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { defaults, isLoading, error, refresh };
}
