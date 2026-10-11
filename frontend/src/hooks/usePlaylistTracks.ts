import { useCallback, useEffect, useRef, useState } from 'react';
import type { Schema } from '@/types/apiSchema';
import { getPlaylistTracks } from '@/services/playlistService';
import { errorMessage } from '@/services/apiClient';

export interface UsePlaylistTracksReturn {
  data: Schema<'PlaylistTracksResponse'> | null;
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
}

/** Loads a playlist's tracklist whenever `playlistId` becomes non-null; responses for a previous id are dropped. */
export function usePlaylistTracks(playlistId: string | null): UsePlaylistTracksReturn {
  const [data, setData] = useState<Schema<'PlaylistTracksResponse'> | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const latestId = useRef<string | null>(null);

  const load = useCallback(async (id: string | null): Promise<void> => {
    latestId.current = id;
    if (id === null) {
      setData(null);
      setError(null);
      setLoading(false);
      return;
    }
    setData(null);
    setLoading(true);
    setError(null);
    try {
      const res = await getPlaylistTracks(id);
      if (latestId.current !== id) return;
      setData(res);
    } catch (err: unknown) {
      if (latestId.current !== id) return;
      setError(errorMessage(err, 'Failed to load tracks'));
    } finally {
      if (latestId.current === id) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(playlistId);
  }, [playlistId, load]);

  const reload = useCallback((): Promise<void> => load(playlistId), [load, playlistId]);

  return { data, loading, error, reload };
}
