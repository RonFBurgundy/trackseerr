import { useCallback, useEffect, useState } from 'react';
import type { PlexAccountStatusResponse } from '@/types/mediaServer';
import { errorMessage } from '@/services/apiClient';
import { getPlexAccountStatus } from '@/services/mediaServerService';

export interface UsePlexAccountStatusReturn {
  status: PlexAccountStatusResponse | null;
  isLoading: boolean;
  /** Load failure, or null. */
  error: string | null;
  reload: () => Promise<void>;
}

/** Loads the stored Plex owner account status for the Media Server settings panel. */
export function usePlexAccountStatus(enabled = true): UsePlexAccountStatusReturn {
  const [status, setStatus] = useState<PlexAccountStatusResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(enabled);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const next = await getPlexAccountStatus();
      setStatus(next);
      setError(null);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not load Plex account status'));
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setIsLoading(true);
    getPlexAccountStatus()
      .then((next) => {
        if (cancelled) return;
        setStatus(next);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorMessage(err, 'Could not load Plex account status'));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  return { status, isLoading, error, reload };
}
