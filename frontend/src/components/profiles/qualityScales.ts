/** Slider scale (max kbps) per quality id. Rows come from the API; this only tunes the track of known codecs. */
const SCALES: Readonly<Record<string, number>> = {
  'FLAC 24bit': 10000,
  'FLAC 16bit': 1600,
  ALAC: 1600,
  'WAV/AIFF': 5000,
  'MP3 320': 400,
  'MP3 V0': 400,
  'MP3 V1': 400,
  'MP3 V2': 400,
  'MP3 192': 300,
  'AAC 256': 400,
  'AAC (other)': 400,
  Opus: 300,
  'OGG Vorbis': 400,
  Unknown: 10000,
};

/** Ignore case, spaces and punctuation so `AAC other`, `aac-other` and `AAC (other)` all hit the same entry. */
const norm = (id: string): string => id.toLowerCase().replace(/[^a-z0-9]+/g, '');

const NORMALIZED: ReadonlyMap<string, number> = new Map(Object.entries(SCALES).map(([k, v]) => [norm(k), v]));

/** Upper end of a quality's slider track; unknown ids get max(2 x default max, 500). */
export function scaleFor(quality: string, defaultMax: number | null): number {
  return NORMALIZED.get(norm(quality)) ?? Math.max(2 * (defaultMax ?? 0), 500);
}

/** MB per minute of audio at a bitrate: kbps x 60 / 8 / 1000. */
export const mbPerMin = (kbps: number): number => (kbps * 60) / 8 / 1000;

export function formatMbPerMin(kbps: number): string {
  const v = mbPerMin(kbps);
  return `${v >= 10 ? v.toFixed(1) : v.toFixed(2)} MB/min`;
}
