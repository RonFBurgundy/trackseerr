/** Normalises the backend's `genres` (native: stored comma text, Lidarr: a list, or null) into display names. */
export function genreNames(genres: string | readonly string[] | null | undefined): string[] {
  if (Array.isArray(genres)) return genres.filter((g): g is string => typeof g === 'string' && g.length > 0);
  if (typeof genres === 'string') return genres.split(',').map((g) => g.trim()).filter(Boolean);
  return [];
}
