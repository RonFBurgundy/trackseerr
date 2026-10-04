import type { TrackItem } from '@/types/models';

export interface QualityBadge {
  label: string;
  className: string;
}

const LOSSLESS = 'bg-emerald-950/40 text-emerald-400 border border-emerald-500/30';
const AMBER = 'bg-[#e5a00d]/10 text-[#e5a00d] border border-[#e5a00d]/30';
const NEUTRAL = 'bg-neutral-800 text-neutral-300 border border-neutral-700';

/** Track length in seconds from whichever field the backend sent (`duration_seconds`, else legacy `duration_ms`). */
export function trackSeconds(track: TrackItem): number | undefined {
  if (typeof track.duration_seconds === 'number' && track.duration_seconds > 0) return track.duration_seconds;
  if (typeof track.duration_ms === 'number' && track.duration_ms > 0) return track.duration_ms / 1000;
  return undefined;
}

export function formatTrackDuration(seconds?: number): string {
  if (!seconds || seconds <= 0) return '--:--';
  const totalSeconds = Math.floor(seconds);
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${s < 10 ? '0' : ''}${s}`;
}

export function getQualityBadge(track: TrackItem): QualityBadge {
  if (track.quality) {
    return { label: track.quality, className: track.quality.toLowerCase().includes('flac') ? LOSSLESS : AMBER };
  }
  const file = track.file;
  if (!file && !track.has_file && !track.file_path) {
    return { label: 'Missing', className: 'bg-red-950/30 text-red-400 border border-red-800/40' };
  }
  if (file?.quality) {
    return { label: file.quality, className: file.quality.toLowerCase().includes('flac') ? LOSSLESS : AMBER };
  }
  if (file) {
    const fmt = (file.format || '').toUpperCase();
    const bits = file.bits_per_sample;
    const bitrate = file.bitrate;
    if (fmt === 'FLAC') {
      return { label: bits === 24 ? 'FLAC 24-bit' : 'FLAC Lossless', className: LOSSLESS };
    }
    if (bitrate) {
      const kbps = Math.round(bitrate / 1000);
      return { label: `${fmt || 'MP3'} ${kbps}`, className: kbps >= 320 ? AMBER : NEUTRAL };
    }
    if (fmt) return { label: fmt, className: AMBER };
  }
  const path = track.file_path ?? file?.file_path ?? null;
  if (path) {
    const lower = path.toLowerCase();
    if (lower.endsWith('.flac')) return { label: 'FLAC Lossless', className: LOSSLESS };
    if (lower.endsWith('.mp3')) return { label: 'MP3 320', className: AMBER };
    return { label: 'Available', className: LOSSLESS };
  }
  if (track.has_file) return { label: 'Available', className: LOSSLESS };
  return { label: 'Missing', className: 'bg-neutral-800 text-neutral-400 border border-neutral-700' };
}
