/** Number of entries in a playlist's `tracks_json` snapshot, or null when absent or unparseable. */
export function parseTrackCount(tracksJson: string | null | undefined): number | null {
  if (!tracksJson) return null;
  try {
    const parsed: unknown = JSON.parse(tracksJson);
    return Array.isArray(parsed) ? parsed.length : null;
  } catch {
    return null;
  }
}
