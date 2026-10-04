import { useEffect, useState } from 'react';
import type { QualityProfile } from '@/types/models';
import { getQualityProfiles } from '@/services/settingsService';
import { errorMessage } from '@/services/apiClient';

export interface UseQualityProfilesReturn {
  profiles: QualityProfile[];
  loading: boolean;
}

/** Quality profile list, fetched once while `enabled` first turns true. */
export function useQualityProfiles(
  enabled: boolean,
  onError: (msg: string, tone: 'error') => void
): UseQualityProfilesReturn {
  const [profiles, setProfiles] = useState<QualityProfile[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [loaded, setLoaded] = useState<boolean>(false);

  useEffect(() => {
    if (!enabled || loaded) return;
    let cancelled = false;
    setLoading(true);
    getQualityProfiles()
      .then((list) => {
        if (cancelled) return;
        setProfiles(list);
        setLoaded(true);
      })
      .catch((err: unknown) => {
        if (!cancelled) onError(errorMessage(err, 'Failed to load quality profiles'), 'error');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, loaded, onError]);

  return { profiles, loading };
}
