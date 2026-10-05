import React from 'react';
import { tileArtSources } from './artSrc';
import { User } from 'lucide-react';
import type { ArtistItem } from '@/types/models';
import { MachinedCard, SelectionCheckbox } from '@/components/ui';
import { ArtistStatusBadge } from './ArtistStatusBadge';

/** Selection-mode wiring for a tile: clicking selects instead of opening. */
export interface TileSelection {
  checked: boolean;
  locked: boolean;
  onToggle: () => void;
}

export interface ArtistTileProps {
  artist: ArtistItem;
  /** Effective flag (server value or pending optimistic one). */
  monitored: boolean;
  onOpen: (artistId: number | string) => void;
  /** Present while the bulk editor is active. */
  selection?: TileSelection;
}

function hideBrokenImage(e: React.SyntheticEvent<HTMLImageElement>): void {
  e.currentTarget.style.display = 'none';
}

/** Compact artwork tile for the artists grid: art, name, counts and the status LED. Monitoring lives in the detail view. */
export const ArtistTile: React.FC<ArtistTileProps> = React.memo(({ artist, monitored, onOpen, selection }) => (
  <MachinedCard
    interactive
    role="button"
    tabIndex={0}
    aria-label={selection ? `Select ${artist.name}` : `Open ${artist.name}`}
    onClick={() => (selection ? (selection.locked ? undefined : selection.onToggle()) : onOpen(artist.id))}
    onKeyDown={(e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        if (selection) {
          if (!selection.locked) selection.onToggle();
        } else onOpen(artist.id);
      }
    }}
    className={`relative overflow-hidden group ${selection?.checked ? 'ring-1 ring-[var(--accent-amber)]' : ''}`}
  >
    {selection && (
      <SelectionCheckbox
        checked={selection.checked}
        disabled={selection.locked}
        label={`Select ${artist.name}`}
        name="select-artist"
        onChange={selection.onToggle}
      />
    )}
    <ArtistStatusBadge artist={artist} monitored={monitored} />
    <div className="aspect-square w-full bg-[var(--bg-surface-elevated)] flex items-center justify-center overflow-hidden">
      {artist.image_url ? (
        <img
          {...tileArtSources(artist.image_url)}
          alt=""
          width={160}
          height={160}
          className="w-full h-full object-cover"
          loading="lazy"
          decoding="async"
          onError={hideBrokenImage}
        />
      ) : (
        <User className="h-1/3 w-1/3 text-neutral-700" />
      )}
    </div>
    <div className="p-1.5 space-y-0.5">
      <h4
        className="font-bold text-xs leading-4 text-white truncate group-hover:text-[var(--accent-amber)] transition-colors"
        title={artist.name}
      >
        {artist.name}
      </h4>
      <p className="text-[10px] leading-[14px] text-neutral-400 font-mono truncate">
        {artist.album_count || 0} albums &bull; {artist.track_count || 0} tracks
      </p>
    </div>
  </MachinedCard>
));
ArtistTile.displayName = 'ArtistTile';
