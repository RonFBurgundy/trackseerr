import React from 'react';
import { BookmarkPlus, Disc } from 'lucide-react';
import type { AlbumItem } from '@/types/models';
import { MachinedCard, TactileSwitch } from '@/components/ui';

export interface AlbumTileProps {
  album: AlbumItem;
  monitored: boolean;
  isAdmin: boolean;
  /** Collections are a native-library feature; false hides the Collect key. */
  canCollect: boolean;
  onOpen: (album: AlbumItem) => void;
  onCollect: (album: AlbumItem) => void;
  onToggleMonitored: (albumId: number | string, monitored: boolean) => void;
}

function hideBrokenImage(e: React.SyntheticEvent<HTMLImageElement>): void {
  e.currentTarget.style.display = 'none';
}

/** Cover tile for the albums grid. */
export const AlbumTile: React.FC<AlbumTileProps> = React.memo(
  ({ album, monitored, isAdmin, canCollect, onOpen, onCollect, onToggleMonitored }) => (
    <MachinedCard
      interactive
      role="button"
      tabIndex={0}
      aria-label={`Open ${album.title}`}
      onClick={() => onOpen(album)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onOpen(album);
        }
      }}
      className="overflow-hidden group"
    >
      <div className="aspect-square w-full bg-[#1a1a1a] flex items-center justify-center overflow-hidden">
        {album.cover_url ? (
          <img
            src={album.cover_url}
            alt=""
            className="w-full h-full object-cover transition-transform duration-300 group-hover:scale-105"
            loading="lazy"
            onError={hideBrokenImage}
          />
        ) : (
          <Disc className="h-1/3 w-1/3 text-neutral-700" />
        )}
      </div>
      <div className="p-2 space-y-1">
        <h4
          className="font-bold text-sm text-white truncate group-hover:text-[#e5a00d] transition-colors"
          title={album.title}
        >
          {album.title}
        </h4>
        <p className="text-[11px] text-neutral-400 font-mono truncate" title={album.artist_name}>
          {album.artist_name || 'Unknown Artist'}
          {album.release_date ? ` (${album.release_date.slice(0, 4)})` : ''}
        </p>
        <div className="flex items-center justify-between gap-2 min-h-[28px]" onClick={(e) => e.stopPropagation()}>
          {canCollect ? (
            <button
              type="button"
              onClick={() => onCollect(album)}
              title="Add to Collection"
              aria-label={`Add ${album.title} to a collection`}
              className="inline-flex items-center justify-center h-11 w-11 max-sm:-my-1.5 sm:h-8 sm:w-8 rounded-[3px] border border-[#2a2a2a] bg-[#181818] hover:border-[#e5a00d]/50"
            >
              <BookmarkPlus className="h-3.5 w-3.5 text-[#e5a00d]" />
            </button>
          ) : (
            <span
              className={`text-[10px] font-mono uppercase tracking-wider ${
                monitored ? 'text-[#e5a00d]' : 'text-neutral-500'
              }`}
            >
              {monitored ? 'Monitored' : 'Unmonitored'}
            </span>
          )}
          {isAdmin && (
            <TactileSwitch
              checked={monitored}
              onChange={(val) => onToggleMonitored(album.id, val)}
              label=""
              title={monitored ? 'Monitored: releases are grabbed' : 'Unmonitored'}
            />
          )}
        </div>
      </div>
    </MachinedCard>
  )
);
AlbumTile.displayName = 'AlbumTile';
