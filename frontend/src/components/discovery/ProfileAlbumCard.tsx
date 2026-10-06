import React from 'react';
import { Disc } from 'lucide-react';
import { MachinedCard } from '@/components/ui';
import type { ArtistProfileAlbum, DiscoveryStatus } from '@/types/models';
import { ProfileStatusBadge } from './ProfileStatusBadge';
import { ProfileRequestButton } from './ProfileRequestButton';
import { releaseYear } from './profileItems';

export interface ProfileAlbumCardProps {
  album: ArtistProfileAlbum;
  /** Status after folding in this session's requests. */
  status: DiscoveryStatus;
  busy: boolean;
  onOpen: (album: ArtistProfileAlbum) => void;
  onRequest: (album: ArtistProfileAlbum) => void;
}

/** One release of an artist profile: art, status chip, title, year/type and the request key. Click opens the tracklist. */
export const ProfileAlbumCard: React.FC<ProfileAlbumCardProps> = React.memo(({ album, status, busy, onOpen, onRequest }) => {
  const meta = [releaseYear(album.release_date), album.record_type].filter(Boolean).join(' / ');
  return (
    <MachinedCard interactive onClick={() => onOpen(album)} className="group flex flex-col overflow-hidden">
      <div className="relative aspect-square w-full overflow-hidden bg-[#1c1c1c]">
        {album.cover_url ? (
          <img
            src={album.cover_url}
            alt=""
            width={300}
            height={300}
            loading="lazy"
            decoding="async"
            className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-105"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center bg-[#151515]">
            <Disc className="h-10 w-10 text-neutral-600" />
          </div>
        )}
        <ProfileStatusBadge status={status} have={album.have_tracks} total={album.total_tracks} className="absolute left-2 top-2 z-10 shadow-md" />
      </div>
      <div className="flex flex-1 flex-col justify-between gap-2 p-2 sm:p-3">
        <div className="min-w-0">
          <h3 className="truncate text-xs font-bold text-white sm:text-sm" title={album.title}>
            {album.title}
          </h3>
          {meta && <p className="truncate text-[11px] font-mono text-neutral-400 sm:text-xs">{meta}</p>}
        </div>
        <ProfileRequestButton status={status} busy={busy} onRequest={() => onRequest(album)} className="w-full" />
      </div>
    </MachinedCard>
  );
});
ProfileAlbumCard.displayName = 'ProfileAlbumCard';
