import React from 'react';
import { Pause, Play } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';
import type { ArtistProfileTrack, DiscoveryStatus } from '@/types/models';
import { ProfileRequestButton } from './ProfileRequestButton';

export interface ProfileTrackRowProps {
  index: number;
  track: ArtistProfileTrack;
  status: DiscoveryStatus;
  busy: boolean;
  isPlaying: boolean;
  onPlay: (track: ArtistProfileTrack) => void;
  onRequest: (track: ArtistProfileTrack) => void;
}

function formatDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return '';
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

/** One top track: index, preview key, title/album, duration, status and request key. */
export const ProfileTrackRow: React.FC<ProfileTrackRowProps> = React.memo(({ index, track, status, busy, isPlaying, onPlay, onRequest }) => (
  <li className="flex items-center gap-2 p-2 transition-colors hover:bg-[#181818] sm:gap-3 sm:p-2.5">
    <span className="w-5 shrink-0 text-center font-mono text-xs text-neutral-500">{index + 1}</span>
    {track.preview_url ? (
      <TapeDeckButton
        size="sm"
        className="shrink-0"
        aria-label={isPlaying ? `Pause preview of ${track.title}` : `Play preview of ${track.title}`}
        onClick={() => onPlay(track)}
        icon={isPlaying ? <Pause className="h-3 w-3 text-[#e5a00d]" /> : <Play className="h-3 w-3 fill-current" />}
      />
    ) : (
      <span className="w-9 shrink-0" aria-hidden="true" />
    )}
    <div className="min-w-0 flex-1">
      <p className="truncate text-sm text-neutral-200" title={track.title}>
        {track.title}
      </p>
      {track.album && <p className="truncate text-[11px] font-mono text-neutral-500">{track.album}</p>}
    </div>
    <span className="hidden shrink-0 font-mono text-xs text-neutral-500 sm:inline">{formatDuration(track.duration)}</span>
    <div className="flex shrink-0 items-center gap-2">
      <ProfileRequestButton status={status} busy={busy} onRequest={() => onRequest(track)} />
    </div>
  </li>
));
ProfileTrackRow.displayName = 'ProfileTrackRow';
