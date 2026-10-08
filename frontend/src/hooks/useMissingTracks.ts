import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  getMissingTracks,
  searchMissingTracks,
  createMatchOverride,
  getMatchOverrides,
  deleteMatchOverride as apiDeleteMatchOverride,
  type MissingTrack,
  type MediaTrackHit,
  type MatchOverride,
} from '@/services/missingService';
import { errorMessage } from '@/services/apiClient';

export interface UseMissingTracksOptions {
  isAdmin?: boolean;
}

export interface UseMissingTracksReturn {
  missingTracks: MissingTrack[];
  missingCountByPlaylist: Record<string, number>;
  loading: boolean;
  error: string | null;
  overrides: MatchOverride[];
  loadingOverrides: boolean;
  overridesError: string | null;
  reload: () => Promise<void>;
  searchTracks: (query: string) => Promise<MediaTrackHit[]>;
  matchTrack: (track: MissingTrack, hit: MediaTrackHit) => Promise<void>;
  loadOverrides: () => Promise<void>;
  deleteOverride: (overrideId: number) => Promise<void>;
}

export function useMissingTracks({ isAdmin = false }: UseMissingTracksOptions): UseMissingTracksReturn {
  const [missingTracks, setMissingTracks] = useState<MissingTrack[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const [overrides, setOverrides] = useState<MatchOverride[]>([]);
  const [loadingOverrides, setLoadingOverrides] = useState<boolean>(false);
  const [overridesError, setOverridesError] = useState<string | null>(null);

  const reload = useCallback(async (): Promise<void> => {
    if (!isAdmin) {
      setMissingTracks([]);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await getMissingTracks();
      setMissingTracks(data);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to load missing tracks'));
    } finally {
      setLoading(false);
    }
  }, [isAdmin]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const loadOverrides = useCallback(async (): Promise<void> => {
    if (!isAdmin) return;
    setLoadingOverrides(true);
    setOverridesError(null);
    try {
      const data = await getMatchOverrides();
      setOverrides(data);
    } catch (err: unknown) {
      setOverridesError(errorMessage(err, 'Failed to load match overrides'));
    } finally {
      setLoadingOverrides(false);
    }
  }, [isAdmin]);

  const missingCountByPlaylist = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const track of missingTracks) {
      const pid = track.playlist_id;
      counts[pid] = (counts[pid] || 0) + 1;
    }
    return counts;
  }, [missingTracks]);

  const searchTracks = useCallback(
    async (query: string): Promise<MediaTrackHit[]> => {
      if (!query.trim()) return [];
      return searchMissingTracks(query.trim());
    },
    []
  );

  const matchTrack = useCallback(
    async (track: MissingTrack, hit: MediaTrackHit): Promise<void> => {
      await createMatchOverride({
        source_title: track.title,
        source_artist: track.artist,
        plex_rating_key: hit.rating_key,
        plex_title: hit.title,
        plex_artist: hit.artist,
      });

      // The server deletes missing tracks matching title and artist.
      // Update local state immediately so row disappears and count decreases.
      const matchTitle = track.title.trim().toLowerCase();
      const matchArtist = track.artist.trim().toLowerCase();
      setMissingTracks((prev) =>
        prev.filter((t) => {
          if (t.id === track.id) return false;
          return !(t.title.trim().toLowerCase() === matchTitle && t.artist.trim().toLowerCase() === matchArtist);
        })
      );
    },
    []
  );

  const deleteOverride = useCallback(
    async (overrideId: number): Promise<void> => {
      await apiDeleteMatchOverride(overrideId);
      setOverrides((prev) => prev.filter((o) => o.id !== overrideId));
      // Re-fetch missing tracks in case deleting the override re-exposed missing items
      void reload();
    },
    [reload]
  );

  return {
    missingTracks,
    missingCountByPlaylist,
    loading,
    error,
    overrides,
    loadingOverrides,
    overridesError,
    reload,
    searchTracks,
    matchTrack,
    loadOverrides,
    deleteOverride,
  };
}
