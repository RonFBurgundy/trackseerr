import React from 'react';
import { User } from 'lucide-react';
import type { ArtistItem } from '@/types/models';
import { MachinedCard, TactileSwitch } from '@/components/ui';

export interface ArtistTileProps {
  artist: ArtistItem;
  /** Effective flag (server value or pending optimistic one). */
  monitored: boolean;
  isAdmin: boolean;
  onOpen: (artistId: number | string) => void;
  onToggleMonitored: (artistId: number | string, monitored: boolean) => void;
}

function hideBrokenImage(e: React.SyntheticEvent<HTMLImageElement>): void {
  e.currentTarget.style.display = 'none';
}

/** Square artwork tile for the artists grid. */
export const ArtistTile: React.FC<ArtistTileProps> = React.memo(
  ({ artist, monitored, isAdmin, onOpen, onToggleMonitored }) => (
    <MachinedCard
      interactive
      role="button"
      tabIndex={0}
      aria-label={`Open ${artist.name}`}
      onClick={() => onOpen(artist.id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onOpen(artist.id);
        }
      }}
      className="overflow-hidden group"
    >
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
