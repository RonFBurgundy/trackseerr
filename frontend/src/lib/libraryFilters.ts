import type { LibraryFilters } from '@/types/libraryFilters';
import type { Schema } from '@/types/apiSchema';

export const EMPTY_LIBRARY_FILTERS: LibraryFilters = {
  genres: [],
  excludeGenres: [],
  countries: [],
  yearFrom: null,
  yearTo: null,
  albumTypes: [],
  artistTypes: [],
  membersMin: null,
  membersMax: null,
  formedFrom: null,
  formedTo: null,
  popularityMin: null,
  popularityMax: null,
  tagIds: [],
};

export function libraryFiltersToQuery(f: LibraryFilters): Record<string, string | string[]> {
  const query: Record<string, string | string[]> = {};
  if (f.genres.length > 0) query.genre = [...f.genres];
  if (f.excludeGenres.length > 0) query.exclude_genre = [...f.excludeGenres];
  if (f.countries.length > 0) query.country = [...f.countries];
  if (f.yearFrom !== null) query.year_from = String(f.yearFrom);
  if (f.yearTo !== null) query.year_to = String(f.yearTo);
  if (f.albumTypes.length > 0) query.album_type = [...f.albumTypes];
  if (f.artistTypes.length > 0) query.artist_type = [...f.artistTypes];
  if (f.membersMin !== null) query.members_min = String(f.membersMin);
  if (f.membersMax !== null) query.members_max = String(f.membersMax);
  if (f.formedFrom !== null) query.formed_from = String(f.formedFrom);
  if (f.formedTo !== null) query.formed_to = String(f.formedTo);
  if (f.popularityMin !== null) query.popularity_min = String(f.popularityMin);
  if (f.popularityMax !== null) query.popularity_max = String(f.popularityMax);
  if (f.tagIds.length > 0) query.tag = f.tagIds.map(String);
  return query;
}

export function countLibraryFilters(f: LibraryFilters): number {
  let count = 0;
  count += f.genres.length;
  count += f.excludeGenres.length;
  count += f.countries.length;
  if (f.yearFrom !== null || f.yearTo !== null) count += 1;
  count += f.albumTypes.length;
  count += f.artistTypes.length;
  if (f.membersMin !== null || f.membersMax !== null) count += 1;
  if (f.formedFrom !== null || f.formedTo !== null) count += 1;
  if (f.popularityMin !== null || f.popularityMax !== null) count += 1;
  count += f.tagIds.length;
  return count;
}

export function isLibraryFiltersEmpty(f: LibraryFilters): boolean {
  return countLibraryFilters(f) === 0;
}

export function libraryFiltersToRules(
  f: LibraryFilters,
  sort: Schema<'SmartRulesBody'>['sort'] = 'random',
  limit: number = 100
): Schema<'SmartRulesBody'> {
  return {
    genres: [...f.genres],
    exclude_genres: [...f.excludeGenres],
    countries: [...f.countries],
    year_from: f.yearFrom,
    year_to: f.yearTo,
    album_types: [...f.albumTypes],
    artist_types: [...f.artistTypes],
    members_min: f.membersMin,
    members_max: f.membersMax,
    formed_from: f.formedFrom,
    formed_to: f.formedTo,
    popularity_min: f.popularityMin,
    popularity_max: f.popularityMax,
    tag_ids: [...f.tagIds],
    sort,
    limit,
  };
}

export function rulesToLibraryFilters(r: Schema<'SmartRulesBody'>): {
  filters: LibraryFilters;
  sort: Schema<'SmartRulesBody'>['sort'];
  limit: number;
} {
  return {
    filters: {
      genres: r.genres ? [...r.genres] : [],
      excludeGenres: r.exclude_genres ? [...r.exclude_genres] : [],
      countries: r.countries ? [...r.countries] : [],
      yearFrom: r.year_from ?? null,
      yearTo: r.year_to ?? null,
      albumTypes: r.album_types ? [...r.album_types] : [],
      artistTypes: r.artist_types ? [...r.artist_types] : [],
      membersMin: r.members_min ?? null,
      membersMax: r.members_max ?? null,
      formedFrom: r.formed_from ?? null,
      formedTo: r.formed_to ?? null,
      popularityMin: r.popularity_min ?? null,
      popularityMax: r.popularity_max ?? null,
      tagIds: r.tag_ids ? [...r.tag_ids] : [],
    },
    sort: r.sort ?? 'random',
    limit: r.limit ?? 100,
  };
}

export interface LibraryFilterChip {
  key: string;
  label: string;
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

export function libraryFilterChips(
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
