import { useState, useEffect } from 'react';
import type { FeaturedChart } from '@/types/models';
import { getFeaturedCharts } from '@/services/playlistService';
import { errorMessage } from '@/services/apiClient';

export interface UseFeaturedChartsReturn {
  charts: FeaturedChart[];
  isLoading: boolean;
  error: string | null;
}

let sessionCachedCharts: FeaturedChart[] | null = null;

export function useFeaturedCharts(enabled: boolean): UseFeaturedChartsReturn {
  const [charts, setCharts] = useState<FeaturedChart[]>(() => sessionCachedCharts ?? []);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled || sessionCachedCharts !== null) {
      if (sessionCachedCharts !== null && charts.length === 0 && sessionCachedCharts.length > 0) {
        setCharts(sessionCachedCharts);
      }
      return;
    }

    let isMounted = true;
    setIsLoading(true);
    setError(null);

    getFeaturedCharts()
      .then((data) => {
        sessionCachedCharts = data;
        if (isMounted) {
          setCharts(data);
        }
      })
      .catch((err: unknown) => {
        if (isMounted) {
          setError(errorMessage(err, 'Failed to load featured charts'));
        }
      })
      .finally(() => {
        if (isMounted) {
          setIsLoading(false);
        }
      });

    return () => {
      isMounted = false;
    };
  }, [enabled, charts.length]);

  return { charts, isLoading, error };
}
