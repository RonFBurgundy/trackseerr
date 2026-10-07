import { useCallback, useEffect, useMemo, useState } from 'react';
import type { AlbumItem, TrackItem } from '@/types/models';
import { searchArtistTracks } from '@/services/libraryService';
import { useDebouncedValue } from './useDebouncedValue';

const norm = (s: string | null | undefined): string => (s ?? '').toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '');

/** Every whitespace-separated word of `query` appears somewhere in `text` (order-free "contains"). */
export function textMatches(query: string, text: string | null | undefined): boolean {
  const hay = norm(text);
  return norm(query).split(/\s+/).filter((w) => w.length > 0).every((w) => hay.includes(w));
}

export interface UseDiscographyFilterReturn {
  text: string;
  setText: (value: string) => void;
  clear: () => void;
  /** A non-empty filter is applied. */
  active: boolean;
  /** Track lookup in flight. */
  searching: boolean;
  /** Albums that match by their own title, or by a matching track. */
  filterAlbums: (albums: AlbumItem[]) => AlbumItem[];
  /** The filter text when this album matched through its tracks (so its card expands and narrows), else undefined. */
  trackFilterFor: (albumId: number | string) => string | undefined;
}

/**
 * Narrows one artist's discography by text. Album titles match locally; track titles are looked up through the paged
 * tracks endpoint (`artist_id` + `q`) because tracks are not loaded until an album is opened.
 */
export function useDiscographyFilter(artistId: number | string): UseDiscographyFilterReturn {
  const [text, setText] = useState<string>('');
  const query = useDebouncedValue(text.trim(), 250);
  const [trackAlbums, setTrackAlbums] = useState<ReadonlySet<string>>(() => new Set<string>());
  const [searching, setSearching] = useState<boolean>(false);

  useEffect(() => {
    setText('');
  }, [artistId]);

  useEffect(() => {
    if (query.length === 0) {
      setTrackAlbums(new Set<string>());
      setSearching(false);
      return undefined;
    }
    const controller = new AbortController();
    setSearching(true);
    searchArtistTracks(artistId, query, controller.signal)
      .then((rows: TrackItem[]) => {
        if (controller.signal.aborted) return;
        const ids = new Set<string>();
        for (const t of rows) {
          // The server's `q` is broader than a title match (it also hits album and artist names): keep title hits only.
          if (t.album_id && textMatches(query, t.title)) ids.add(String(t.album_id));
        }
        setTrackAlbums(ids);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) return;
        console.warn('Discography track filter failed; matching album titles only', err);
        setTrackAlbums(new Set<string>());
      })
      .finally(() => {
        if (!controller.signal.aborted) setSearching(false);
      });
    return () => controller.abort();
  }, [artistId, query]);

  const active = text.trim().length > 0;
  const clear = useCallback((): void => setText(''), []);

  const filterAlbums = useCallback(
    (albums: AlbumItem[]): AlbumItem[] =>
      active ? albums.filter((a) => textMatches(query, a.title) || trackAlbums.has(String(a.id))) : albums,
    [active, query, trackAlbums]
  );

  const trackFilterFor = useCallback(
    (albumId: number | string): string | undefined => (active && trackAlbums.has(String(albumId)) ? query : undefined),
    [active, query, trackAlbums]
  );

  return useMemo(
    () => ({ text, setText, clear, active, searching, filterAlbums, trackFilterFor }),
    [text, clear, active, searching, filterAlbums, trackFilterFor]
  );
}
