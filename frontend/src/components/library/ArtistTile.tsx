import React from 'react';
import { User } from 'lucide-react';
import type { ArtistItem } from '@/types/models';
import { MachinedCard, SelectionCheckbox, TactileSwitch } from '@/components/ui';

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
  isAdmin: boolean;
  onOpen: (artistId: number | string) => void;
  onToggleMonitored: (artistId: number | string, monitored: boolean) => void;
  /** Present while the bulk editor is active. */
  selection?: TileSelection;
}

function hideBrokenImage(e: React.SyntheticEvent<HTMLImageElement>): void {
  e.currentTarget.style.display = 'none';
}

/** Square artwork tile for the artists grid. */
export const ArtistTile: React.FC<ArtistTileProps> = React.memo(
  ({ artist, monitored, isAdmin, onOpen, onToggleMonitored, selection }) => (
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
      className={`relative overflow-hidden group ${selection?.checked ? 'ring-1 ring-[#e5a00d]' : ''}`}
    >
      {selection && (
        <SelectionCheckbox
          checked={selection.checked}
          disabled={selection.locked}
          label={`Select ${artist.name}`}
          onChange={selection.onToggle}
        />
      )}
      <div className="aspect-square w-full bg-[#1a1a1a] flex items-center justify-center overflow-hidden">
        {artist.image_url ? (
          <img
            src={artist.image_url}
            alt=""
            className="w-full h-full object-cover transition-transform duration-300 group-hover:scale-105"
            loading="lazy"
            onError={hideBrokenImage}
          />
        ) : (
          <User className="h-1/3 w-1/3 text-neutral-700" />
        )}
      </div>
      <div className="p-2 space-y-1">
        <h4
          className="font-bold text-sm text-white truncate group-hover:text-[#e5a00d] transition-colors"
          title={artist.name}
        >
          {artist.name}
        </h4>
        <p className="text-[11px] text-neutral-400 font-mono truncate">
          {artist.album_count || 0} Albums &bull; {artist.track_count || 0} Tracks
        </p>
        <div className="flex items-center justify-between min-h-[28px]" onClick={(e) => e.stopPropagation()}>
          <span
            className={`text-[10px] font-mono uppercase tracking-wider ${
              monitored ? 'text-[#e5a00d]' : 'text-neutral-500'
            }`}
          >
            {monitored ? 'Monitored' : 'Unmonitored'}
          </span>
          {isAdmin && (
            <TactileSwitch
              checked={monitored}
              onChange={(val) => onToggleMonitored(artist.id, val)}
              label=""
              title={monitored ? 'Monitored: new releases are grabbed' : 'Unmonitored: releases are not grabbed'}
            />
          )}
        </div>
      </div>
    </MachinedCard>
  )
);
ArtistTile.displayName = 'ArtistTile';
