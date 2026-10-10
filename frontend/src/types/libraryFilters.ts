export interface LibraryFilters {
  genres: string[];
  excludeGenres: string[];
  countries: string[];
  yearFrom: number | null;
  yearTo: number | null;
  albumTypes: string[];
  artistTypes: string[];
  membersMin: number | null;
  membersMax: number | null;
  formedFrom: number | null;
  formedTo: number | null;
  popularityMin: number | null;
  popularityMax: number | null;
  tagIds: number[];
}
