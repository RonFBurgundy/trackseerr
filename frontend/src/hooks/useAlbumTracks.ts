import { useCallback, useEffect, useState } from 'react';
import type { TrackItem } from '@/types/models';
import { errorMessage } from '@/services/apiClient';
import { getAlbumTracksPaged } from '@/services/libraryService';

export interface UseAlbumTracksReturn {
  tracks: TrackItem[];
  loading: boolean;
  error: string | null;
  /** Applies a monitored change to the loaded rows after the server accepted it. */
  patchMonitored: (trackId: number | string, monitored: boolean) => void;
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

/** Tracks of one album from the paged endpoint (`album_id` filter). Idle while `albumId` is null. */
export function useAlbumTracks(albumId: number | string | null): UseAlbumTracksReturn {
  const [tracks, setTracks] = useState<TrackItem[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setTracks([]);
    setError(null);
    if (albumId === null) {
      setLoading(false);
      return undefined;
    }
    const controller = new AbortController();
    setLoading(true);
    getAlbumTracksPaged(albumId, controller.signal)
      .then((rows) => {
        if (!controller.signal.aborted) setTracks(rows);
      })
      .catch((err: unknown) => {
        if (isAbort(err) || controller.signal.aborted) return;
        setError(errorMessage(err, 'Failed to load album tracks'));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [albumId]);

  const patchMonitored = useCallback((trackId: number | string, monitored: boolean): void => {
    setTracks((prev) => prev.map((t) => (t.id === trackId ? { ...t, monitored } : t)));
  }, []);

  return { tracks, loading, error, patchMonitored };
}
