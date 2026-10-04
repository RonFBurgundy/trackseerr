import { useCallback, useEffect, useState } from 'react';
import type { AlbumItem, ArtistItem } from '@/types/models';
import type { MonitorOption } from '@/types/monitoring';
import { errorMessage } from '@/services/apiClient';
import { getArtistDetail, refreshArtist, setArtistMonitoringPreset } from '@/services/libraryService';

export type ArtistDetailData = ArtistItem & { albums?: AlbumItem[] };
export type MonitorPreset = MonitorOption;

export interface UseArtistDetailReturn {
  artist: ArtistDetailData | null;
  loading: boolean;
  refreshing: boolean;
  refreshDiscography: () => Promise<void>;
  applyPreset: (preset: MonitorPreset) => Promise<void>;
  patchAlbumMonitored: (albumId: number | string, monitored: boolean) => void;
  patchArtistMonitored: (monitored: boolean) => void;
}

/** Artist detail (with its releases) from `/api/library/artists/{id}`; works in native and Lidarr mode. */
export function useArtistDetail(
  artistId: number | string,
  onToast: (msg: string, tone?: 'ok' | 'error') => void,
  onChanged: () => void
): UseArtistDetailReturn {
  const [artist, setArtist] = useState<ArtistDetailData | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [refreshing, setRefreshing] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    setArtist(null);
    setLoading(true);
    getArtistDetail(artistId)
      .then((data) => {
        if (!cancelled) setArtist(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) onToast(errorMessage(err, 'Failed to load artist details'), 'error');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [artistId, onToast]);

  const refreshDiscography = useCallback(async (): Promise<void> => {
    setRefreshing(true);
    try {
      await refreshArtist(artistId);
      setArtist(await getArtistDetail(artistId));
      onChanged();
      onToast('Discography refreshed');
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to refresh discography'), 'error');
    } finally {
      setRefreshing(false);
    }
  }, [artistId, onChanged, onToast]);

  const applyPreset = useCallback(
    async (preset: MonitorPreset): Promise<void> => {
      try {
        await setArtistMonitoringPreset(artistId, preset);
        setArtist(await getArtistDetail(artistId));
        onChanged();
        onToast(`Master monitoring set to '${preset.replace('_', ' ')}'`);
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to apply monitoring preset'), 'error');
      }
    },
    [artistId, onChanged, onToast]
  );

  const patchAlbumMonitored = useCallback((albumId: number | string, monitored: boolean): void => {
    setArtist((prev) =>
      prev ? { ...prev, albums: prev.albums?.map((a) => (a.id === albumId ? { ...a, monitored } : a)) } : prev
    );
  }, []);

  const patchArtistMonitored = useCallback((monitored: boolean): void => {
    setArtist((prev) => (prev ? { ...prev, monitored } : prev));
  }, []);

  return { artist, loading, refreshing, refreshDiscography, applyPreset, patchAlbumMonitored, patchArtistMonitored };
}
