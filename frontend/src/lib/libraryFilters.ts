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
