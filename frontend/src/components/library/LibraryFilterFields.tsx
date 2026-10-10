import React, { useMemo, useState } from 'react';
import type { Schema } from '@/types';
import type { LibraryFilters } from '@/types/libraryFilters';
import { TagPicker } from '@/components/ui/TagPicker';
import { inputClass } from '@/components/ui/FormField';

export interface LibraryFilterFieldsProps {
  filters: LibraryFilters;
  onChange: (next: LibraryFilters) => void;
  facets: Schema<'LibraryFacetsResponse'> | null;
  tags?: readonly Schema<'TagOut'>[];
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

export const LibraryFilterFields: React.FC<LibraryFilterFieldsProps> = ({
  filters,
  onChange,
  facets,
  tags,
}) => {
  const [genreSearch, setGenreSearch] = useState<string>('');

  const genres = useMemo(
    () => (facets?.genres ?? []).filter((g) => g.count > 0),
    [facets?.genres]
  );
  const visibleGenres = useMemo(() => {
    const q = genreSearch.trim().toLowerCase();
    if (!q) return genres;
    return genres.filter((g) => g.value.toLowerCase().includes(q));
  }, [genres, genreSearch]);

  const decades = useMemo(
    () => (facets?.decades ?? []).filter((d) => d.count > 0),
    [facets?.decades]
  );

  const countries = useMemo(
    () => (facets?.countries ?? []).filter((c) => c.count > 0),
    [facets?.countries]
  );

  const albumTypes = useMemo(
    () => (facets?.album_types ?? []).filter((t) => t.count > 0),
    [facets?.album_types]
  );

  const artistTypes = useMemo(
    () => (facets?.artist_types ?? []).filter((t) => t.count > 0),
    [facets?.artist_types]
  );

  const showEra =
    decades.length > 0 || facets?.year_min != null || facets?.year_max != null;

  const showArtist =
    artistTypes.length > 0 ||
    (facets?.members_max != null && facets.members_max > 0) ||
    facets?.formed_min != null ||
    facets?.formed_max != null;

  const showPopularity =
    facets?.popularity_max != null && facets.popularity_max > 0;

  const showTags = Boolean(tags && tags.length > 0);

  return (
    <div className="space-y-5">
      {/* 1. Genre Section */}
      {genres.length > 0 && (
        <section aria-labelledby="filter-heading-genre" className="space-y-2">
          <div id="filter-heading-genre" className="text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] font-semibold">
            Genre
          </div>
          <div>
            <label htmlFor="library_filter_genre_search" className="sr-only">
              Search genres
            </label>
            <input
              id="library_filter_genre_search"
              name="genre_search"
              type="text"
              autoComplete="off"
              value={genreSearch}
              onChange={(e) => setGenreSearch(e.target.value)}
              placeholder="Filter genres..."
              className={`${inputClass} mb-2 text-xs`}
            />
          </div>
          <div
            role="group"
            aria-label="Genre filters"
            className="flex flex-wrap gap-1.5 max-h-40 overflow-y-auto pr-1"
          >
            {visibleGenres.map((g) => {
              const isIncluded = filters.genres.includes(g.value);
              const isExcluded = filters.excludeGenres.includes(g.value);
              const chipClass = isIncluded
                ? 'border-[var(--accent-amber)]/60 bg-[var(--accent-amber)]/10 text-[var(--accent-amber)]'
                : isExcluded
                ? 'line-through border-red-500/40 bg-red-950/20 text-red-400'
                : 'border-[var(--border-default)] bg-[var(--bg-surface-elevated)] text-[var(--text-secondary)] hover:text-white';

              return (
                <button
                  key={g.value}
                  type="button"
                  aria-pressed={isIncluded ? 'true' : isExcluded ? 'mixed' : 'false'}
                  onClick={() => {
                    if (isIncluded) {
                      onChange({
                        ...filters,
                        genres: filters.genres.filter((x) => x !== g.value),
                        excludeGenres: [...filters.excludeGenres, g.value],
                      });
                    } else if (isExcluded) {
                      onChange({
                        ...filters,
                        excludeGenres: filters.excludeGenres.filter((x) => x !== g.value),
                      });
                    } else {
                      onChange({
                        ...filters,
                        genres: [...filters.genres, g.value],
                      });
                    }
                  }}
                  className={`relative inline-flex h-7 items-center gap-1 rounded-[3px] border px-2 text-xs font-mono transition-colors focus-visible:outline-none focus-visible:border-[var(--accent-amber)] ${chipClass}`}
                >
                  <span className="truncate">{g.value}</span>
                  <span className="text-[10px] opacity-60">({g.count})</span>
                </button>
              );
            })}
            {visibleGenres.length === 0 && (
              <span className="text-xs font-mono text-[var(--text-muted)] py-1">
                No matching genres.
              </span>
            )}
          </div>
        </section>
      )}

      {/* 2. Era Section */}
      {showEra && (
        <section aria-labelledby="filter-heading-era" className="space-y-2">
          <div id="filter-heading-era" className="text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] font-semibold">
            Era
          </div>
          {decades.length > 0 && (
            <div role="group" aria-label="Decade filters" className="flex flex-wrap gap-1.5">
              {decades.map((d) => {
                const decFrom = d.value;
                const decTo = d.value + 9;
                const selected =
                  filters.yearFrom !== null &&
                  filters.yearTo !== null &&
                  filters.yearFrom <= decFrom &&
                  filters.yearTo >= decTo;

                return (
                  <button
                    key={d.value}
                    type="button"
                    aria-pressed={selected}
                    onClick={() => {
                      const curFrom = filters.yearFrom;
                      const curTo = filters.yearTo;
                      if (curFrom === decFrom && curTo === decTo) {
                        onChange({ ...filters, yearFrom: null, yearTo: null });
                        return;
                      }
                      if (curFrom === null || curTo === null) {
                        onChange({ ...filters, yearFrom: decFrom, yearTo: decTo });
                        return;
                      }
                      if (decTo === curFrom - 1 || decFrom === curFrom - 10) {
                        onChange({ ...filters, yearFrom: decFrom, yearTo: curTo });
                      } else if (decFrom === curTo + 1) {
                        onChange({ ...filters, yearFrom: curFrom, yearTo: decTo });
                      } else if (decFrom === curFrom && curTo > decTo) {
                        onChange({ ...filters, yearFrom: decFrom + 10, yearTo: curTo });
                      } else if (decTo === curTo && curFrom < decFrom) {
                        onChange({ ...filters, yearFrom: curFrom, yearTo: decTo - 10 });
                      } else {
                        onChange({ ...filters, yearFrom: decFrom, yearTo: decTo });
                      }
                    }}
                    className={`relative inline-flex h-7 items-center gap-1 rounded-[3px] border px-2 text-xs font-mono transition-colors focus-visible:outline-none focus-visible:border-[var(--accent-amber)] ${
                      selected
                        ? 'border-[var(--accent-amber)]/60 bg-[var(--accent-amber)]/10 text-[var(--accent-amber)]'
                        : 'border-[var(--border-default)] bg-[var(--bg-surface-elevated)] text-[var(--text-secondary)] hover:text-white'
                    }`}
                  >
                    <span>{`${d.value}s`}</span>
                    <span className="text-[10px] opacity-60">({d.count})</span>
                  </button>
                );
              })}
            </div>
          )}
          <div className="grid grid-cols-2 gap-3 pt-1">
            <div>
              <label
                htmlFor="library_filter_year_from"
                className="block text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] mb-1"
              >
                Year from
              </label>
              <input
                id="library_filter_year_from"
                name="year_from"
                type="number"
                min={facets?.year_min ?? undefined}
                max={facets?.year_max ?? undefined}
                placeholder={facets?.year_min != null ? String(facets.year_min) : ''}
                value={filters.yearFrom ?? ''}
                onChange={(e) => {
                  const val = e.target.value.trim() ? parseInt(e.target.value, 10) : null;
                  onChange({ ...filters, yearFrom: Number.isNaN(val) ? null : val });
                }}
                className={inputClass}
              />
            </div>
            <div>
              <label
                htmlFor="library_filter_year_to"
                className="block text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] mb-1"
              >
                Year to
              </label>
              <input
                id="library_filter_year_to"
                name="year_to"
                type="number"
                min={facets?.year_min ?? undefined}
                max={facets?.year_max ?? undefined}
                placeholder={facets?.year_max != null ? String(facets.year_max) : ''}
                value={filters.yearTo ?? ''}
                onChange={(e) => {
                  const val = e.target.value.trim() ? parseInt(e.target.value, 10) : null;
                  onChange({ ...filters, yearTo: Number.isNaN(val) ? null : val });
                }}
                className={inputClass}
              />
            </div>
          </div>
        </section>
      )}

      {/* 3. Country Section */}
      {countries.length > 0 && (
        <section aria-labelledby="filter-heading-country" className="space-y-2">
          <div id="filter-heading-country" className="text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] font-semibold">
            Country
          </div>
          <div
            role="group"
            aria-label="Country filters"
            className="flex flex-wrap gap-1.5 max-h-36 overflow-y-auto pr-1"
          >
            {countries.map((c) => {
              const selected = filters.countries.includes(c.value);
              return (
                <button
                  key={c.value}
                  type="button"
                  aria-pressed={selected}
                  onClick={() => {
                    onChange({
                      ...filters,
                      countries: selected
                        ? filters.countries.filter((x) => x !== c.value)
                        : [...filters.countries, c.value],
                    });
                  }}
                  className={`relative inline-flex h-7 items-center gap-1 rounded-[3px] border px-2 text-xs font-mono transition-colors focus-visible:outline-none focus-visible:border-[var(--accent-amber)] ${
                    selected
                      ? 'border-[var(--accent-amber)]/60 bg-[var(--accent-amber)]/10 text-[var(--accent-amber)]'
                      : 'border-[var(--border-default)] bg-[var(--bg-surface-elevated)] text-[var(--text-secondary)] hover:text-white'
                  }`}
                >
                  <span>{getCountryLabel(c.value)}</span>
                  <span className="text-[10px] opacity-60">({c.count})</span>
                </button>
              );
            })}
          </div>
        </section>
      )}

      {/* 4. Release Type Section */}
      {albumTypes.length > 0 && (
        <section aria-labelledby="filter-heading-album-type" className="space-y-2">
          <div id="filter-heading-album-type" className="text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] font-semibold">
            Release Type
          </div>
          <div role="group" aria-label="Release type filters" className="flex flex-wrap gap-1.5">
            {albumTypes.map((t) => {
              const selected = filters.albumTypes.includes(t.value);
              return (
                <button
                  key={t.value}
                  type="button"
                  aria-pressed={selected}
                  onClick={() => {
                    onChange({
                      ...filters,
                      albumTypes: selected
                        ? filters.albumTypes.filter((x) => x !== t.value)
                        : [...filters.albumTypes, t.value],
                    });
                  }}
                  className={`relative inline-flex h-7 items-center gap-1 rounded-[3px] border px-2 text-xs font-mono transition-colors focus-visible:outline-none focus-visible:border-[var(--accent-amber)] ${
                    selected
                      ? 'border-[var(--accent-amber)]/60 bg-[var(--accent-amber)]/10 text-[var(--accent-amber)]'
                      : 'border-[var(--border-default)] bg-[var(--bg-surface-elevated)] text-[var(--text-secondary)] hover:text-white'
                  }`}
                >
                  <span>{getAlbumTypeLabel(t.value)}</span>
                  <span className="text-[10px] opacity-60">({t.count})</span>
                </button>
              );
            })}
          </div>
        </section>
      )}

      {/* 5. Artist Section */}
      {showArtist && (
        <section aria-labelledby="filter-heading-artist" className="space-y-2">
          <div id="filter-heading-artist" className="text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] font-semibold">
            Artist
          </div>
          {artistTypes.length > 0 && (
            <div role="group" aria-label="Artist type filters" className="flex flex-wrap gap-1.5 mb-3">
              {artistTypes.map((t) => {
                const selected = filters.artistTypes.includes(t.value);
                return (
                  <button
                    key={t.value}
                    type="button"
                    aria-pressed={selected}
                    onClick={() => {
                      onChange({
                        ...filters,
                        artistTypes: selected
                          ? filters.artistTypes.filter((x) => x !== t.value)
                          : [...filters.artistTypes, t.value],
                      });
                    }}
                    className={`relative inline-flex h-7 items-center gap-1 rounded-[3px] border px-2 text-xs font-mono transition-colors focus-visible:outline-none focus-visible:border-[var(--accent-amber)] ${
                      selected
                        ? 'border-[var(--accent-amber)]/60 bg-[var(--accent-amber)]/10 text-[var(--accent-amber)]'
                        : 'border-[var(--border-default)] bg-[var(--bg-surface-elevated)] text-[var(--text-secondary)] hover:text-white'
                    }`}
                  >
                    <span>{getArtistTypeLabel(t.value)}</span>
                    <span className="text-[10px] opacity-60">({t.count})</span>
                  </button>
                );
              })}
            </div>
          )}

          {facets?.members_max != null && facets.members_max > 0 && (
            <div className="grid grid-cols-2 gap-3 mb-3">
              <div>
                <label
                  htmlFor="library_filter_members_min"
                  className="block text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] mb-1"
                >
                  Min members
                </label>
                <input
                  id="library_filter_members_min"
                  name="members_min"
                  type="number"
                  min={1}
                  max={facets.members_max}
                  placeholder="1"
                  value={filters.membersMin ?? ''}
                  onChange={(e) => {
                    const val = e.target.value.trim() ? parseInt(e.target.value, 10) : null;
                    onChange({ ...filters, membersMin: Number.isNaN(val) ? null : val });
                  }}
                  className={inputClass}
                />
              </div>
              <div>
                <label
                  htmlFor="library_filter_members_max"
                  className="block text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] mb-1"
                >
                  Max members
                </label>
                <input
                  id="library_filter_members_max"
                  name="members_max"
                  type="number"
                  min={1}
                  max={facets.members_max}
                  placeholder={String(facets.members_max)}
                  value={filters.membersMax ?? ''}
                  onChange={(e) => {
                    const val = e.target.value.trim() ? parseInt(e.target.value, 10) : null;
                    onChange({ ...filters, membersMax: Number.isNaN(val) ? null : val });
                  }}
                  className={inputClass}
                />
              </div>
            </div>
          )}

          {(facets?.formed_min != null || facets?.formed_max != null) && (
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label
                  htmlFor="library_filter_formed_from"
                  className="block text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] mb-1"
                >
                  Formed from
                </label>
                <input
                  id="library_filter_formed_from"
                  name="formed_from"
                  type="number"
                  min={facets?.formed_min ?? undefined}
                  max={facets?.formed_max ?? undefined}
                  placeholder={facets?.formed_min != null ? String(facets.formed_min) : ''}
                  value={filters.formedFrom ?? ''}
                  onChange={(e) => {
                    const val = e.target.value.trim() ? parseInt(e.target.value, 10) : null;
                    onChange({ ...filters, formedFrom: Number.isNaN(val) ? null : val });
                  }}
                  className={inputClass}
                />
              </div>
              <div>
                <label
                  htmlFor="library_filter_formed_to"
                  className="block text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] mb-1"
                >
                  Formed to
                </label>
                <input
                  id="library_filter_formed_to"
                  name="formed_to"
                  type="number"
                  min={facets?.formed_min ?? undefined}
                  max={facets?.formed_max ?? undefined}
                  placeholder={facets?.formed_max != null ? String(facets.formed_max) : ''}
                  value={filters.formedTo ?? ''}
                  onChange={(e) => {
                    const val = e.target.value.trim() ? parseInt(e.target.value, 10) : null;
                    onChange({ ...filters, formedTo: Number.isNaN(val) ? null : val });
                  }}
                  className={inputClass}
                />
              </div>
            </div>
          )}
        </section>
      )}

      {/* 6. Popularity Section */}
      {showPopularity && (
        <section aria-labelledby="filter-heading-popularity" className="space-y-2">
          <div id="filter-heading-popularity" className="text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] font-semibold">
            Popularity
          </div>
          <div
            role="group"
            aria-label="Popularity preset filters"
            className="tape-transport-bay inline-flex flex-wrap gap-1 p-1"
          >
            {[
              {
                label: 'Any',
                min: null,
                max: null,
                isMatch: filters.popularityMin === null && filters.popularityMax === null,
              },
              {
                label: '10k+',
                min: 10000,
                max: null,
                isMatch: filters.popularityMin === 10000 && filters.popularityMax === null,
              },
              {
                label: '100k+',
                min: 100000,
                max: null,
                isMatch: filters.popularityMin === 100000 && filters.popularityMax === null,
              },
              {
                label: '1M+',
                min: 1000000,
                max: null,
                isMatch: filters.popularityMin === 1000000 && filters.popularityMax === null,
              },
              {
                label: 'Hidden gems',
                min: null,
                max: 10000,
                isMatch: filters.popularityMin === null && filters.popularityMax === 10000,
              },
            ].map((preset) => (
              <button
                key={preset.label}
                type="button"
                aria-pressed={preset.isMatch}
                onClick={() =>
                  onChange({
                    ...filters,
                    popularityMin: preset.min,
                    popularityMax: preset.max,
                  })
                }
                className={`tape-deck-btn px-2.5 py-1 text-xs font-mono uppercase tracking-wider rounded-[3px] transition-colors ${
                  preset.isMatch
                    ? 'engaged border-[var(--accent-amber)] text-[var(--accent-amber)]'
                    : 'text-[var(--text-secondary)] hover:text-white'
                }`}
              >
                {preset.label}
              </button>
            ))}
          </div>
        </section>
      )}

      {/* 7. Tags Section */}
      {showTags && tags && (
        <section aria-labelledby="filter-heading-tags" className="space-y-2">
          <TagPicker
            label="Tags"
            name="library_filter_tags"
            tags={tags}
            mode="id"
            value={filters.tagIds}
            onChange={(next) => onChange({ ...filters, tagIds: next })}
          />
        </section>
      )}
    </div>
  );
};
