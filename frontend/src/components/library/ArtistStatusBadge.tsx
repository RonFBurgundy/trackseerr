import React from 'react';
import type { ArtistItem } from '@/types/models';

export type ArtistLedKind = 'complete' | 'missing' | 'unmonitored' | 'monitored';

export interface ArtistLedState {
  kind: ArtistLedKind;
  /** Ended artists get a square jewel, continuing (or unknown) ones a round one. */
  ended: boolean;
  label: string;
}

type StatusInput = Pick<ArtistItem, 'status' | 'track_count' | 'track_file_count'>;

/**
 * Derives the status LED from fields the artist record carries. There is no downloading or error signal on the
 * artist item, so neither a pulse nor a red state exists here.
 * - complete: tracks known, and files cover every track (green, regardless of monitoring, like Lidarr)
 * - missing: files known and fewer than tracks, artist monitored (amber)
 * - unmonitored: not complete and not monitored (hollow grey ring)
 * - monitored: monitored but counts unknown or no tracks yet (dim grey jewel)
 */
export function deriveArtistLed(artist: StatusInput, monitored: boolean): ArtistLedState {
  const ended = (artist.status ?? '').toLowerCase() === 'ended';
  const tracks = artist.track_count;
  const files = artist.track_file_count;
  const known = typeof tracks === 'number' && typeof files === 'number' && tracks > 0;
  const complete = known && files >= tracks;
  const missingCount = known ? tracks - files : 0;
  const lifecycle = ended ? 'ended' : 'continuing';

  if (complete) return { kind: 'complete', ended, label: `${lifecycle}, all tracks downloaded` };
  if (!monitored) {
    const detail = missingCount > 0 ? `${missingCount} missing tracks, not monitored` : 'not monitored';
    return { kind: 'unmonitored', ended, label: `${lifecycle}, ${detail}` };
  }
  if (missingCount > 0) {
    return { kind: 'missing', ended, label: `${lifecycle}, ${missingCount} missing ${missingCount === 1 ? 'track' : 'tracks'}, monitored` };
  }
  return { kind: 'monitored', ended, label: `${lifecycle}, monitored` };
}

const JEWEL_CLASS: Record<ArtistLedKind, string> = {
  complete: 'bg-[var(--status-success)] shadow-[0_0_5px_var(--status-success)]',
  missing: 'bg-[var(--accent-amber)] shadow-[0_0_5px_var(--accent-amber)]',
  monitored: 'bg-[var(--text-muted)]',
  unmonitored: 'border border-[var(--text-muted)] bg-transparent',
};

export interface LedJewelProps {
  kind: ArtistLedKind;
  ended: boolean;
  className?: string;
}

/** The bare jewel (no chip), shared by the tile corner and the stats legend. */
export const LedJewel: React.FC<LedJewelProps> = ({ kind, ended, className = '' }) => (
  <span
    aria-hidden="true"
    className={`inline-block h-[7px] w-[7px] shrink-0 ${ended ? 'rounded-[1px]' : 'rounded-full'} ${JEWEL_CLASS[kind]} ${className}`}
  />
);

export interface ArtistStatusBadgeProps {
  artist: StatusInput;
  monitored: boolean;
}

/** Recessed corner chip holding the LED jewel. Overlays the top-right of the tile art (parent must be `relative`). */
export const ArtistStatusBadge: React.FC<ArtistStatusBadgeProps> = React.memo(({ artist, monitored }) => {
  const state = deriveArtistLed(artist, monitored);
  return (
    <span
      role="img"
      aria-label={`Status: ${state.label}`}
      title={state.label}
      className="absolute right-1 top-1 z-10 flex h-[14px] w-[14px] items-center justify-center rounded-[3px] border border-[#1f1f1f] bg-[#0d0d0d]/90 shadow-[inset_0_1px_2px_rgba(0,0,0,0.8)]"
    >
      <LedJewel kind={state.kind} ended={state.ended} />
    </span>
  );
});
ArtistStatusBadge.displayName = 'ArtistStatusBadge';
