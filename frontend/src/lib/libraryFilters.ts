import type { LibraryFilters } from '@/types/libraryFilters';

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
