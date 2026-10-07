import React from 'react';
import { tileArtSources } from './artSrc';
import { Disc } from 'lucide-react';
import type { AlbumItem } from '@/types/models';
import { MachinedCard, SelectionCheckbox } from '@/components/ui';
import type { TileSelection } from './ArtistTile';

export interface AlbumTileProps {
  album: AlbumItem;
  monitored: boolean;
  onOpen: (album: AlbumItem) => void;
  /** Present while the bulk editor is active. */
  selection?: TileSelection;
}

function hideBrokenImage(e: React.SyntheticEvent<HTMLImageElement>): void {
  e.currentTarget.style.display = 'none';
}

/** Compact cover tile for the albums grid. Monitoring and collections live in the album detail. */
export const AlbumTile: React.FC<AlbumTileProps> = React.memo(({ album, monitored, onOpen, selection }) => (
  <MachinedCard
    interactive
    role="button"
    tabIndex={0}
    aria-label={selection ? `Select ${album.title}` : `Open ${album.title}`}
    onClick={() => (selection ? (selection.locked ? undefined : selection.onToggle()) : onOpen(album))}
    onKeyDown={(e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        if (selection) {
          if (!selection.locked) selection.onToggle();
        } else onOpen(album);
      }
    }}
    className={`relative overflow-hidden group ${selection?.checked ? 'ring-1 ring-[var(--accent-amber)]' : ''}`}
  >
    {selection && (
      <SelectionCheckbox
        checked={selection.checked}
        disabled={selection.locked}
        label={`Select ${album.title}`}
        name="select-album"
        onChange={selection.onToggle}
      />
    )}
    <span
      role="img"
      aria-label={monitored ? 'Monitored' : 'Not monitored'}
      title={monitored ? 'Monitored' : 'Not monitored'}
      className="absolute right-1 top-1 z-10 flex h-[14px] w-[14px] items-center justify-center rounded-[3px] border border-[#1f1f1f] bg-[#0d0d0d]/90 shadow-[inset_0_1px_2px_rgba(0,0,0,0.8)]"
    >
      <span
        aria-hidden="true"
        className={`inline-block h-[7px] w-[7px] rounded-full ${
          monitored ? 'bg-[var(--accent-amber)] shadow-[0_0_5px_var(--accent-amber)]' : 'border border-[var(--text-muted)]'
        }`}
      />
    </span>
    <div className="aspect-square w-full bg-[var(--bg-surface-elevated)] flex items-center justify-center overflow-hidden">
      {album.cover_url ? (
        <img
          {...tileArtSources(album.cover_url)}
          alt=""
          width={160}
          height={160}
          className="w-full h-full object-cover"
          loading="lazy"
          decoding="async"
          onError={hideBrokenImage}
        />
      ) : (
        <Disc className="h-1/3 w-1/3 text-neutral-700" />
      )}
    </div>
    <div className="p-1.5 space-y-0.5">
      <h4
        className="font-bold text-xs leading-4 text-white truncate group-hover:text-[var(--accent-amber)] transition-colors"
        title={album.title ?? undefined}
      >
        {album.title}
      </h4>
      <p className="text-[10px] leading-[14px] text-neutral-400 font-mono truncate" title={album.artist_name ?? undefined}>
        {album.artist_name || 'Unknown artist'}
        {album.release_date ? ` (${album.release_date.slice(0, 4)})` : ''}
      </p>
    </div>
  </MachinedCard>
));
AlbumTile.displayName = 'AlbumTile';
