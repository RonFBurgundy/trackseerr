import type { LibraryHealthFinding } from '@/types/libraryHealth';
import type { CleanupFailedInfo, OrphanTorrentInfo } from '@/types/seedCleanup';

const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v : null);
const numOrNull = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

export function orphanInfo(f: LibraryHealthFinding): OrphanTorrentInfo {
  const d = f.detail ?? {};
  return {
    name: str(d.name) ?? f.path,
    size: numOrNull(d.size),
    ratio: numOrNull(d.ratio),
    seedingSeconds: numOrNull(d.seeding_seconds),
    path: f.path,
  };
}

export function failedInfo(f: LibraryHealthFinding): CleanupFailedInfo {
  const d = f.detail ?? {};
  return { title: f.path, error: str(d.error), attempts: numOrNull(d.attempts) };
}

/** Seconds as "5 h" / "3.5 d"; "-" when unknown. */
export function formatSeedTime(seconds: number | null): string {
  if (seconds === null || seconds < 0) return '-';
  const hours = seconds / 3600;
  if (hours < 48) return `${parseFloat(hours.toFixed(1))} h`;
  return `${parseFloat((hours / 24).toFixed(1))} d`;
}

export const isSeedCleanupKind = (kind: string): boolean => kind === 'orphan_torrent' || kind === 'cleanup_failed';
