/** Client-side mirror of the server's tag label rules (the server stays the authority). */
export const TAG_LABEL_MAX = 40;
const TAG_LABEL_PATTERN = /^[a-z0-9\-_ &]+$/;

/** Trimmed and lower-cased, exactly as the server stores it. */
export function normalizeTagLabel(raw: string): string {
  return raw.trim().toLowerCase();
}

/** Returns a message when `raw` is not a valid label after normalising, otherwise null. */
export function tagLabelProblem(raw: string): string | null {
  const label = normalizeTagLabel(raw);
  if (label.length === 0) return 'Enter a tag label.';
  if (label.length > TAG_LABEL_MAX) return `Tags are at most ${TAG_LABEL_MAX} characters.`;
  if (!TAG_LABEL_PATTERN.test(label)) return 'Use only letters, numbers, spaces, ampersands, hyphens and underscores.';
  return null;
}
