/** Adds `size` to an art URL, respecting an existing query string. Pure, so the string is stable across renders. */
export function sizedArtUrl(url: string, size: 250 | 500): string {
  const hashAt = url.indexOf('#');
  const base = hashAt === -1 ? url : url.slice(0, hashAt);
  const hash = hashAt === -1 ? '' : url.slice(hashAt);
  return `${base}${base.includes('?') ? '&' : '?'}size=${size}${hash}`;
}

/** `src` (250px) and `srcSet` (500px at 2x) for a library art tile. */
export function tileArtSources(url: string): { src: string; srcSet: string } {
  return { src: sizedArtUrl(url, 250), srcSet: `${sizedArtUrl(url, 500)} 2x` };
}
