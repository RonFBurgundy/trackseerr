import React from 'react';
import type { LibraryStats } from '@/types/models';
import { SourceBadge, formatBytes } from '@/components/lists';
import { LedJewel } from './ArtistStatusBadge';

export interface LibraryStatsBarProps {
  stats: LibraryStats | null;
  /** Show the artist status-LED legend (artists tab). */
  showLegend?: boolean;
}

interface Stat {
  key: string;
  label: string;
  value: string;
  accent?: boolean;
}

const fmt = (n: number): string => n.toLocaleString();

/** Builds only the stats the payload carries; every field of LibraryStats beyond the three counts is optional. */
function buildStats(s: LibraryStats): Stat[] {
  const out: Stat[] = [{ key: 'artists', label: 'Artists', value: fmt(s.artist_count) }];
  if (s.monitored_artist_count !== undefined) {
    out.push({ key: 'mon', label: 'Monitored', value: fmt(s.monitored_artist_count), accent: true });
  }
  if (s.unmonitored_artist_count !== undefined) {
    out.push({ key: 'unmon', label: 'Unmonitored', value: fmt(s.unmonitored_artist_count) });
  }
  if (typeof s.continuing_artist_count === 'number') {
    out.push({ key: 'cont', label: 'Continuing', value: fmt(s.continuing_artist_count) });
  }
  if (typeof s.ended_artist_count === 'number') {
    out.push({ key: 'ended', label: 'Ended', value: fmt(s.ended_artist_count) });
  }
  out.push({ key: 'albums', label: 'Albums', value: fmt(s.album_count) });
  out.push({ key: 'tracks', label: 'Tracks', value: fmt(s.track_count) });
  if (s.total_track_count !== undefined && s.total_track_count !== s.track_count) {
    out.push({ key: 'alltracks', label: 'All editions', value: fmt(s.total_track_count) });
  }
  const files = s.track_file_count ?? s.file_count;
  if (files !== undefined) out.push({ key: 'files', label: 'Track files', value: fmt(files) });
  if (s.missing_track_count !== undefined) {
    out.push({ key: 'missing', label: 'Missing', value: fmt(s.missing_track_count), accent: s.missing_track_count > 0 });
  }
  if (s.total_size_bytes !== undefined) out.push({ key: 'size', label: 'On disk', value: formatBytes(s.total_size_bytes) });
  return out;
}

const LEGEND: ReadonlyArray<{ key: string; kind: 'complete' | 'missing' | 'unmonitored'; ended: boolean; text: string }> = [
  { key: 'c', kind: 'complete', ended: false, text: 'all tracks' },
  { key: 'm', kind: 'missing', ended: false, text: 'missing, monitored' },
  { key: 'u', kind: 'unmonitored', ended: false, text: 'unmonitored' },
  { key: 'e', kind: 'complete', ended: true, text: 'square = ended' },
];

/** Library totals as a compact readout; rendered at the bottom of the scrolled list. */
export const LibraryStatsBar: React.FC<LibraryStatsBarProps> = ({ stats, showLegend = false }) => {
  if (!stats) return null;
  const items = buildStats(stats);
  return (
    <footer
      aria-label="Library statistics"
      className="mx-3 mb-3 mt-1 rounded-[4px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3 py-2 font-mono"
    >
      <dl className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        {items.map((it) => (
          <div key={it.key} className="flex items-baseline gap-1.5">
            <dt className="text-[9px] uppercase tracking-widest text-[var(--text-muted)]">{it.label}</dt>
            <dd
              className={`text-xs font-bold tabular-nums ${it.accent ? 'text-[var(--accent-amber)]' : 'text-[var(--text-primary)]'}`}
            >
              {it.value}
            </dd>
          </div>
        ))}
        <SourceBadge mode={stats.source ?? null} />
      </dl>
      {showLegend && (
        <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[9px] uppercase tracking-wider text-[var(--text-muted)]">
          <span>Status</span>
          {LEGEND.map((l) => (
            <span key={l.key} className="inline-flex items-center gap-1">
              <LedJewel kind={l.kind} ended={l.ended} />
              {l.text}
            </span>
          ))}
        </p>
      )}
    </footer>
  );
};
