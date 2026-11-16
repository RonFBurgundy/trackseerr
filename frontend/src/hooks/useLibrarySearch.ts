import { useCallback, useEffect, useMemo, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { errorMessage } from '@/services/apiClient';
import { searchLibrary } from '@/services/libraryService';
import type { LibrarySearchResponse } from '@/types/models';

export const LIBRARY_SEARCH_MIN_CHARS = 2;

export type LibrarySearchItem =
  | { kind: 'suggestion'; text: string }
  | { kind: 'artist'; id: string; name: string; albumCount: number }
  | { kind: 'album'; id: string; title: string; artistName: string; year: number | null }
  | { kind: 'track'; id: string; title: string; artistName: string; albumId: string; albumTitle: string };

export interface UseLibrarySearchReturn {
  results: LibrarySearchResponse | null;
  loading: boolean;
  error: string | null;
  /** Whether the dropdown should be showing (focus + enough characters + enabled). */
  open: boolean;
  setOpen: (open: boolean) => void;
  activeIndex: number;
  setActiveIndex: (i: number) => void;
  /** Suggestion row (if any), then artists, albums, tracks: the arrow-key order. */
  flatItems: LibrarySearchItem[];
  onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => void;
}

export function buildFlatItems(results: LibrarySearchResponse | null): LibrarySearchItem[] {
  if (!results) return [];
  const items: LibrarySearchItem[] = [];
  if (results.suggestion) items.push({ kind: 'suggestion', text: results.suggestion });
  for (const a of results.artists) items.push({ kind: 'artist', id: a.id, name: a.name, albumCount: a.album_count });
  for (const a of results.albums) {
    items.push({ kind: 'album', id: a.id, title: a.title, artistName: a.artist_name, year: a.year ?? null });
  }
  for (const t of results.tracks) {
    items.push({
      kind: 'track',
      id: t.id,
      title: t.title,
      artistName: t.artist_name,
      albumId: t.album_id,
      albumTitle: t.album_title,
    });
  }
  return items;
}

/**
 * Library-wide search. `query` is the already-debounced text; `inputLength` is the live trimmed input length used
 * to hide the dropdown the moment the box is cleared. `onPick` fires for Enter on the active row.
 */
export function useLibrarySearch(
  query: string,
  enabled: boolean,
  onPick: (item: LibrarySearchItem) => void,
  inputLength: number = query.trim().length
): UseLibrarySearchReturn {
  const trimmed = query.trim();
  const active = enabled && trimmed.length >= LIBRARY_SEARCH_MIN_CHARS;
  const [results, setResults] = useState<LibrarySearchResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<boolean>(false);
  const [activeIndex, setActiveIndex] = useState<number>(0);

  useEffect(() => {
    if (!active) {
      setResults(null);
      setLoading(false);
      setError(null);
      return undefined;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    searchLibrary(trimmed, 6, controller.signal)
      .then((res) => {
        if (controller.signal.aborted) return;
        setResults(res);
        setActiveIndex(0);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        console.error('Library search failed', err);
        setError(errorMessage(err, 'Search failed'));
        setResults(null);
        setLoading(false);
      });
    return () => controller.abort();
  }, [active, trimmed]);

  const flatItems = useMemo(() => buildFlatItems(results), [results]);
  const visible = open && enabled && inputLength >= LIBRARY_SEARCH_MIN_CHARS;

  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>): void => {
      if (!visible) {
        if (e.key === 'ArrowDown' && enabled && inputLength >= LIBRARY_SEARCH_MIN_CHARS) {
          e.preventDefault();
          setOpen(true);
        }
        return;
      }
      if (e.key === 'ArrowDown' && flatItems.length > 0) {
        e.preventDefault();
        setActiveIndex((i) => (i + 1) % flatItems.length);
      } else if (e.key === 'ArrowUp' && flatItems.length > 0) {
        e.preventDefault();
        setActiveIndex((i) => (i - 1 + flatItems.length) % flatItems.length);
      } else if (e.key === 'Enter') {
        const hit = flatItems[activeIndex];
        if (hit) {
          e.preventDefault();
          onPick(hit);
        }
      } else if (e.key === 'Escape') {
        // First Escape closes the panel; the box keeps its text until the next one.
        e.preventDefault();
        e.stopPropagation();
        setOpen(false);
      }
    },
    [visible, enabled, inputLength, flatItems, activeIndex, onPick]
  );

  return { results, loading, error, open: visible, setOpen, activeIndex, setActiveIndex, flatItems, onKeyDown };
}
