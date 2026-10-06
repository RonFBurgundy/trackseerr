import { useCallback, useRef, useState } from 'react';
import type { DiscoveryItem } from '@/types/models';
import { getDiscoveryAlbumDetail } from '@/services/discoveryService';

export interface DiscoveryAlbumTrack {
  id: string;
  title: string;
  duration_ms?: number;
  preview_url?: string;
}

export interface UseDiscoveryAlbumReturn {
  album: DiscoveryItem | null;
  tracks: DiscoveryAlbumTrack[];
  isLoading: boolean;
  open: (item: DiscoveryItem) => Promise<void>;
  close: () => void;
}

function isTrackArray(value: unknown): value is DiscoveryAlbumTrack[] {
  return (
    Array.isArray(value) &&
    value.every((t) => typeof t === 'object' && t !== null && 'id' in t && 'title' in t)
  );
}

/** Album tracklist modal state: which album is open and its lazily loaded tracks. */
export function useDiscoveryAlbum(): UseDiscoveryAlbumReturn {
  const [album, setAlbum] = useState<DiscoveryItem | null>(null);
  const [tracks, setTracks] = useState<DiscoveryAlbumTrack[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const generation = useRef<number>(0);

  const open = useCallback(async (item: DiscoveryItem): Promise<void> => {
    const ticket = ++generation.current;
    setAlbum(item);
    setTracks([]);
    setIsLoading(true);
    try {
      const data = await getDiscoveryAlbumDetail(item.id);
      if (ticket === generation.current) setTracks(isTrackArray(data.tracks) ? data.tracks : []);
    } catch (err: unknown) {
      // The modal still shows the album header; the empty tracklist message covers the failure.
      console.warn('Failed to load album tracklist', err);
    } finally {
      if (ticket === generation.current) setIsLoading(false);
    }
  }, []);

  const close = useCallback((): void => {
    generation.current += 1;
    setAlbum(null);
    setTracks([]);
    setIsLoading(false);
  }, []);

  return { album, tracks, isLoading, open, close };
}
