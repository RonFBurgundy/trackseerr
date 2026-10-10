import { useCallback, useMemo, useState } from 'react';
import type { LibraryFilters } from '@/types/libraryFilters';
import {
  EMPTY_LIBRARY_FILTERS,
  countLibraryFilters,
  libraryFiltersToQuery,
} from '@/lib/libraryFilters';

export interface LibraryFilterChip {
  key: string;
  label: string;
}

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

const ARTIST_TYPE_LABELS: Record<string, string> = {
  person: 'Solo',
  group: 'Group',
  orchestra: 'Orchestra',
  choir: 'Choir',
  character: 'Character',
  other: 'Other',
};

function getCountryLabel(code: string): string {
  try {
    const displayNames = new Intl.DisplayNames(['en'], { type: 'region' });
    return displayNames.of(code.toUpperCase()) || code;
  } catch {
    return code;
  }
}

function getArtistTypeLabel(type: string): string {
  return ARTIST_TYPE_LABELS[type.toLowerCase()] ?? (type.charAt(0).toUpperCase() + type.slice(1));
}

function getAlbumTypeLabel(type: string): string {
  return type.charAt(0).toUpperCase() + type.slice(1);
}

function formatFans(fans: number): string {
  if (fans >= 1_000_000) {
    const m = fans / 1_000_000;
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`;
  }
  if (fans >= 1_000) {
    const k = fans / 1_000;
    return `${Number.isInteger(k) ? k : k.toFixed(1)}k`;
  }
  return String(fans);
}

function computeChips(
  filters: LibraryFilters,
  tags?: ReadonlyArray<{ id: number; label: string }>
): LibraryFilterChip[] {
  const chips: LibraryFilterChip[] = [];

  for (const genre of filters.genres) {
    chips.push({ key: `genre:${genre}`, label: `Genre: ${genre}` });
  }

  for (const genre of filters.excludeGenres) {
    chips.push({ key: `exclude_genre:${genre}`, label: `Not: ${genre}` });
  }

  if (filters.yearFrom !== null || filters.yearTo !== null) {
    let label = '';
    if (filters.yearFrom !== null && filters.yearTo !== null) {
      if (filters.yearFrom % 10 === 0 && filters.yearTo === filters.yearFrom + 9) {
        label = `${filters.yearFrom}s`;
      } else if (
        filters.yearFrom % 10 === 0 &&
        (filters.yearTo + 1) % 10 === 0 &&
        filters.yearTo > filters.yearFrom
      ) {
        label = `${filters.yearFrom}s–${filters.yearTo - 9}s`;
      } else if (filters.yearFrom === filters.yearTo) {
        label = `${filters.yearFrom}`;
      } else {
        label = `${filters.yearFrom}–${filters.yearTo}`;
      }
    } else if (filters.yearFrom !== null) {
      label = `From ${filters.yearFrom}`;
    } else {
      label = `Until ${filters.yearTo}`;
    }
    chips.push({ key: 'year', label });
  }

  for (const country of filters.countries) {
    chips.push({ key: `country:${country}`, label: getCountryLabel(country) });
  }

  for (const albumType of filters.albumTypes) {
    chips.push({ key: `album_type:${albumType}`, label: getAlbumTypeLabel(albumType) });
  }

  for (const artistType of filters.artistTypes) {
    chips.push({ key: `artist_type:${artistType}`, label: getArtistTypeLabel(artistType) });
  }

  if (filters.membersMin !== null || filters.membersMax !== null) {
    let label = '';
    if (filters.membersMin !== null && filters.membersMax !== null) {
      if (filters.membersMin === filters.membersMax) {
        label = `${filters.membersMin} member${filters.membersMin === 1 ? '' : 's'}`;
      } else {
        label = `${filters.membersMin}–${filters.membersMax} members`;
      }
    } else if (filters.membersMin !== null) {
      label = `${filters.membersMin}+ members`;
    } else {
      label = `≤${filters.membersMax} members`;
    }
    chips.push({ key: 'members', label });
  }

  if (filters.formedFrom !== null || filters.formedTo !== null) {
    let label = '';
    if (filters.formedFrom !== null && filters.formedTo !== null) {
      if (filters.formedFrom === filters.formedTo) {
        label = `Formed ${filters.formedFrom}`;
      } else {
        label = `Formed ${filters.formedFrom}–${filters.formedTo}`;
      }
    } else if (filters.formedFrom !== null) {
      label = `Formed from ${filters.formedFrom}`;
    } else {
      label = `Formed until ${filters.formedTo}`;
    }
    chips.push({ key: 'formed', label });
  }

  if (filters.popularityMin !== null || filters.popularityMax !== null) {
    let label = '';
    if (filters.popularityMin === null && filters.popularityMax === 10000) {
      label = 'Hidden gems';
    } else if (filters.popularityMin === 1000000 && filters.popularityMax === null) {
      label = '1M+ fans';
    } else if (filters.popularityMin === 100000 && filters.popularityMax === null) {
      label = '100k+ fans';
    } else if (filters.popularityMin === 10000 && filters.popularityMax === null) {
      label = '10k+ fans';
    } else if (filters.popularityMin !== null && filters.popularityMax !== null) {
      label = `${formatFans(filters.popularityMin)}–${formatFans(filters.popularityMax)} fans`;
    } else if (filters.popularityMin !== null) {
      label = `${formatFans(filters.popularityMin)}+ fans`;
    } else {
      label = `≤${formatFans(filters.popularityMax!)} fans`;
    }
    chips.push({ key: 'popularity', label });
  }

  const tagsById = new Map<number, string>();
  if (tags) {
    for (const t of tags) {
      tagsById.set(t.id, t.label);
    }
  }

  for (const tagId of filters.tagIds) {
    const tagLabel = tagsById.get(tagId);
    chips.push({
      key: `tag:${tagId}`,
      label: tagLabel ? `Tag: ${tagLabel}` : `Tag: #${tagId}`,
    });
  }

  return chips;
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
  const chips = useMemo(() => computeChips(filters, tags), [filters, tags]);
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
