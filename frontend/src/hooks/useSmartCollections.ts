import { useCallback, useEffect, useState } from 'react';
import type { Playlist } from '@/types/models';
import { getPlaylists } from '@/services/playlistService';
import { isSmartCollection } from '@/types/smartCollections';

export interface UseSmartCollectionsReturn {
  collections: Playlist[];
  loading: boolean;
  reload: () => Promise<void>;
}

export function useSmartCollections(enabled: boolean = true): UseSmartCollectionsReturn {
  const [collections, setCollections] = useState<Playlist[]>([]);
  const [loading, setLoading] = useState<boolean>(false);

  const reload = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const all = await getPlaylists();
      setCollections(all.filter(isSmartCollection));
    } catch {
      setCollections([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) {
      void reload();
    }
  }, [enabled, reload]);

  return { collections, loading, reload };
}
