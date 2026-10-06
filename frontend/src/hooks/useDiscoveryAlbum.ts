import { useCallback, useRef, useState } from 'react';
import type { DiscoveryItem, DiscoveryStatus } from '@/types/models';
import { getDiscoveryAlbumDetail } from '@/services/discoveryService';

export interface DiscoveryAlbumTrack {
  id: string;
  title: string;
  duration_seconds?: number;
  track_number?: number;
  disc_number?: number;
  preview_url?: string;
  status?: DiscoveryStatus;
  album_discovery_id?: string;
}

export interface UseDiscoveryAlbumReturn {
  album: DiscoveryItem | null;
  tracks: DiscoveryAlbumTrack[];
  isLoading: boolean;
  /** Open an album, or the album a track belongs to (via `album_discovery_id`; a track id is never sent as an album id). */
  open: (item: DiscoveryItem) => Promise<void>;
  close: () => void;
}

function isTrackArray(value: unknown): value is DiscoveryAlbumTrack[] {
  return (
    Array.isArray(value) &&
    value.every((t) => typeof t === 'object' && t !== null && 'id' in t && 'title' in t)
  );
}

function isTrackItem(item: DiscoveryItem): boolean {
  return item.type === 'track' || item.id.includes(':track:');
}

/** The album to load for `item`: itself, or for a track its parent album built from the track's fields. */
function resolveAlbumTarget(item: DiscoveryItem): DiscoveryItem | null {
  if (!isTrackItem(item)) return item;
  if (!item.album_discovery_id) return null;
  return {
    id: item.album_discovery_id,
    title: item.album || item.title,
    artist: item.artist,
    cover_url: item.cover_url,
    release_date: item.release_date,
    type: 'album',
    artist_discovery_id: item.artist_discovery_id,
  };
}

function textField(data: Record<string, unknown>, key: string): string | undefined {
  const value = data[key];
  return typeof value === 'string' && value ? value : undefined;
}

/** Fill the header from the loaded album (a track-derived header only knows the track's fields). */
function mergeAlbumHeader(prev: DiscoveryItem, data: Record<string, unknown>): DiscoveryItem {
  const status = textField(data, 'status');
  return {
    ...prev,
    title: textField(data, 'title') ?? prev.title,
    cover_url: textField(data, 'cover_url') ?? prev.cover_url,
    release_date: textField(data, 'release_date') ?? prev.release_date,
    artist_discovery_id: textField(data, 'artist_discovery_id') ?? prev.artist_discovery_id,
    status: (status as DiscoveryStatus | undefined) ?? prev.status,
  };
}

/** Album tracklist modal state: which album is open and its lazily loaded tracks. */
export function useDiscoveryAlbum(): UseDiscoveryAlbumReturn {
  const [album, setAlbum] = useState<DiscoveryItem | null>(null);
  const [tracks, setTracks] = useState<DiscoveryAlbumTrack[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const generation = useRef<number>(0);

  const open = useCallback(async (item: DiscoveryItem): Promise<void> => {
    const target = resolveAlbumTarget(item);
    if (!target) {
      console.warn('Cannot open album for a track without album_discovery_id', item.id);
      return;
    }
    const ticket = ++generation.current;
    setAlbum(target);
    setTracks([]);
    setIsLoading(true);
    try {
      const data = await getDiscoveryAlbumDetail(target.id);
      if (ticket === generation.current) {
        setTracks(isTrackArray(data.tracks) ? data.tracks : []);
        setAlbum((prev) => (prev ? mergeAlbumHeader(prev, data) : prev));
      }
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
