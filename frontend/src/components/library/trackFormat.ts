import type { TrackItem } from '@/types/models';

export interface QualityBadge {
  label: string;
  className: string;
}

const LOSSLESS = 'bg-emerald-950/40 text-emerald-400 border border-emerald-500/30';
const AMBER = 'bg-[#e5a00d]/10 text-[#e5a00d] border border-[#e5a00d]/30';
const NEUTRAL = 'bg-neutral-800 text-neutral-300 border border-neutral-700';

/** Track length in seconds from `duration_seconds`, the only duration field the backend sends. */
export function trackSeconds(track: TrackItem): number | undefined {
  if (typeof track.duration_seconds === 'number' && track.duration_seconds > 0) return track.duration_seconds;
  return undefined;
}

export function formatTrackDuration(seconds?: number): string {
  if (!seconds || seconds <= 0) return '--:--';
  const totalSeconds = Math.floor(seconds);
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${s < 10 ? '0' : ''}${s}`;
}

const UNMONITORED = 'bg-transparent text-neutral-500 border border-neutral-800';

/**
 * Badge for a track's file state. A file-less track reads "Missing" only when it is wanted:
 * explicitly unmonitored tracks, or tracks on an unmonitored album (Lidarr tracks carry
 * `monitored: null`, so the album's flag decides), read "Unmonitored" instead.
 */
export function getQualityBadge(track: TrackItem, albumMonitored?: boolean): QualityBadge {
  const file = track.file;
  const wanted = track.monitored !== false && albumMonitored !== false;
  if (!file && !track.has_file) {
    if (!wanted) return { label: 'Unmonitored', className: UNMONITORED };
    return { label: 'Missing', className: 'bg-red-950/30 text-red-400 border border-red-800/40' };
  }
  if (file?.quality) {
    return { label: file.quality, className: file.quality.toLowerCase().includes('flac') ? LOSSLESS : AMBER };
  }
  if (file) {
    // The backend sends no `format` (only `codec`), so this stays the bitrate-only path.
    const bitrate = file.bitrate;
    if (bitrate) {
      const kbps = Math.round(bitrate / 1000);
      return { label: `MP3 ${kbps}`, className: kbps >= 320 ? AMBER : NEUTRAL };
    }
  }
  const path = file?.file_path ?? null;
  if (path) {
    const lower = path.toLowerCase();
    if (lower.endsWith('.flac')) return { label: 'FLAC Lossless', className: LOSSLESS };
    if (lower.endsWith('.mp3')) return { label: 'MP3 320', className: AMBER };
    return { label: 'Available', className: LOSSLESS };
  }
  if (track.has_file) return { label: 'Available', className: LOSSLESS };
  if (!wanted) return { label: 'Unmonitored', className: UNMONITORED };
  return { label: 'Missing', className: 'bg-neutral-800 text-neutral-400 border border-neutral-700' };
}
