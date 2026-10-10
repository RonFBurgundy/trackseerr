import { useCallback, useMemo, useState } from 'react';
import type { LibraryFilters } from '@/types/libraryFilters';
import {
  EMPTY_LIBRARY_FILTERS,
  countLibraryFilters,
  libraryFiltersToQuery,
  libraryFilterChips,
  type LibraryFilterChip,
} from '@/lib/libraryFilters';

export type { LibraryFilterChip };

export interface UseLibraryFiltersReturn {
  filters: LibraryFilters;
  setFilters: React.Dispatch<React.SetStateAction<LibraryFilters>>;
  patch: (partial: Partial<LibraryFilters>) => void;
  removeChip: (chipKey: string) => void;
  clear: () => void;
  activeCount: number;
  query: Record<string, string | string[]>;
  chips: LibraryFilterChip[];
}

export function useLibraryFilters(
  tags?: ReadonlyArray<{ id: number; label: string }>,
  initialFilters: LibraryFilters = EMPTY_LIBRARY_FILTERS
): UseLibraryFiltersReturn {
  const [filters, setFilters] = useState<LibraryFilters>(initialFilters);

  const patch = useCallback((partial: Partial<LibraryFilters>): void => {
    setFilters((prev) => ({ ...prev, ...partial }));
  }, []);

  const removeChip = useCallback((chipKey: string): void => {
    setFilters((prev) => {
      if (chipKey.startsWith('genre:')) {
        const g = chipKey.slice('genre:'.length);
        return { ...prev, genres: prev.genres.filter((x) => x !== g) };
      }
      if (chipKey.startsWith('exclude_genre:')) {
        const g = chipKey.slice('exclude_genre:'.length);
        return { ...prev, excludeGenres: prev.excludeGenres.filter((x) => x !== g) };
      }
      if (chipKey === 'year') {
        return { ...prev, yearFrom: null, yearTo: null };
      }
      if (chipKey.startsWith('country:')) {
        const c = chipKey.slice('country:'.length);
        return { ...prev, countries: prev.countries.filter((x) => x !== c) };
      }
      if (chipKey.startsWith('album_type:')) {
        const t = chipKey.slice('album_type:'.length);
        return { ...prev, albumTypes: prev.albumTypes.filter((x) => x !== t) };
      }
      if (chipKey.startsWith('artist_type:')) {
        const t = chipKey.slice('artist_type:'.length);
        return { ...prev, artistTypes: prev.artistTypes.filter((x) => x !== t) };
      }
      if (chipKey === 'members') {
        return { ...prev, membersMin: null, membersMax: null };
      }
      if (chipKey === 'formed') {
        return { ...prev, formedFrom: null, formedTo: null };
      }
      if (chipKey === 'popularity') {
        return { ...prev, popularityMin: null, popularityMax: null };
      }
      if (chipKey.startsWith('tag:')) {
        const id = parseInt(chipKey.slice('tag:'.length), 10);
        return { ...prev, tagIds: prev.tagIds.filter((x) => x !== id) };
      }
      return prev;
    });
  }, []);

  const clear = useCallback((): void => {
    setFilters(EMPTY_LIBRARY_FILTERS);
  }, []);

  const query = useMemo(() => libraryFiltersToQuery(filters), [filters]);
  const chips = useMemo(() => libraryFilterChips(filters, tags), [filters, tags]);
  const activeCount = useMemo(() => countLibraryFilters(filters), [filters]);

  return {
    filters,
    setFilters,
    patch,
    removeChip,
    clear,
    activeCount,
    query,
    chips,
  };
}
