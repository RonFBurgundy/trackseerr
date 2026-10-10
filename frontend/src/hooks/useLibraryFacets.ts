import { useCallback, useEffect, useRef, useState } from 'react';
import type { Schema } from '@/types';
import { getLibraryFacets } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';

export interface UseLibraryFacetsReturn {
  facets: Schema<'LibraryFacetsResponse'> | null;
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
}

export function useLibraryFacets(enabled: boolean): UseLibraryFacetsReturn {
  const [facets, setFacets] = useState<Schema<'LibraryFacetsResponse'> | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef<number>(0);

  const reload = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    const mine = ++requestId.current;
    setLoading(true);
    try {
      const data = await getLibraryFacets();
      if (mine !== requestId.current) return;
      setFacets(data);
      setError(null);
    } catch (err: unknown) {
      if (mine !== requestId.current) return;
      setError(errorMessage(err, 'Failed to load library facets'));
    } finally {
      if (mine === requestId.current) setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    if (enabled) {
      void reload();
    } else {
      setFacets(null);
      setError(null);
      setLoading(false);
    }
  }, [enabled, reload]);

  return { facets, loading, error, reload };
}
