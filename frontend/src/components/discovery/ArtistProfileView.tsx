import React, { useCallback, useMemo } from 'react';
import { Disc, Library, Loader2, Plus, User } from 'lucide-react';
import { MachinedCard, StatusMessage, TapeDeckButton } from '@/components/ui';
import { DetailHeaderBar, PageFrame } from '@/components/layout';
import { useArtistProfile } from '@/hooks/useArtistProfile';
import { useProfileRequests } from '@/hooks/useProfileRequests';
import type { AppRoute } from '@/hooks/useAppRoute';
import type {
  ArtistDiscographyAlbum,
  ArtistProfile,
  ArtistProfileAlbum,
  ArtistProfileLibraryOnly,
  ArtistProfileTrack,
  AudioPreviewTrack,
  DiscoveryItem,
} from '@/types/models';
import { ProfileAlbumCard } from './ProfileAlbumCard';
import { ProfileStatusBadge } from './ProfileStatusBadge';
import { ProfileTrackRow } from './ProfileTrackRow';
import {
  effectiveStatus,
  missingReleases,
  profileAlbumToDiscography,
  profileAlbumToItem,
  profileTrackToItem,
} from './profileItems';

export interface ArtistProfileViewProps {
  discoveryId: string;
  /** Library UI (chips, links) renders only for admins; requesters never get these fields from the server either. */
  isAdmin: boolean;
  onBack: () => void;
  onNavigate: (route: AppRoute) => void;
  /** Opens the discovery tracklist modal for a release. */
  onOpenAlbum: (item: DiscoveryItem) => void;
  /** Opens the track detail modal for a top track. */
  onOpenTrack: (item: DiscoveryItem) => void;
  onPlayTrack: (track: AudioPreviewTrack) => void;
  currentPreviewTrackId?: string;
  isPreviewPlaying: boolean;
  onRequest: (item: DiscoveryItem) => Promise<void>;
  onRequestDiscography: (artist: string, albums: ArtistDiscographyAlbum[]) => Promise<void>;
  requestedIds: ReadonlySet<string>;
}

type GroupKey = 'albums' | 'singles_eps' | 'compilations';

const GROUPS: ReadonlyArray<{ key: GroupKey; title: string }> = [
  { key: 'albums', title: 'Albums' },
  { key: 'singles_eps', title: 'Singles & EPs' },
  { key: 'compilations', title: 'Compilations' },
];

const GRID = 'grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6';

const SectionHeading: React.FC<{ title: string; count: number }> = ({ title, count }) => (
  <h3 className="mb-2 flex items-baseline gap-2 font-mono text-xs font-bold uppercase tracking-widest text-[#e5a00d]">
    {title}
    <span className="text-neutral-500">{count}</span>
  </h3>
);

interface LibraryOnlySectionProps {
  items: ArtistProfileLibraryOnly[];
  /** Present for admins only: opens the album in the Library. */
  onOpen?: (item: ArtistProfileLibraryOnly) => void;
}

const LibraryOnlySection: React.FC<LibraryOnlySectionProps> = ({ items, onOpen }) => (
  <section aria-label="Also in library">
    <SectionHeading title="Also in library" count={items.length} />
    <ul className="divide-y divide-[#1f1f1f] overflow-hidden rounded-[4px] border border-[#1f1f1f]">
      {items.map((item, idx) => {
        const body = (
          <>
            {onOpen && (
              item.cover_url ? (
                <img src={item.cover_url} alt="" width={40} height={40} loading="lazy" decoding="async" className="h-10 w-10 shrink-0 rounded-[3px] border border-[#222222] object-cover" />
              ) : (
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[3px] bg-[#141414]">
                  <Disc className="h-5 w-5 text-neutral-600" />
                </div>
              )
            )}
            <span className="min-w-0 flex-1 truncate text-sm text-neutral-200" title={item.title}>
              {item.title}
            </span>
            {item.year ? <span className="shrink-0 font-mono text-xs text-neutral-500">{item.year}</span> : null}
            <ProfileStatusBadge status={item.status} have={item.have_tracks} total={item.total_tracks} />
          </>
        );
        const key = `${item.library_album_id ?? item.title}-${idx}`;
        return (
          <li key={key}>
            {onOpen && item.library_album_id != null ? (
              <button type="button" onClick={() => onOpen(item)} className="flex w-full items-center gap-3 p-2.5 text-left transition-colors hover:bg-[#181818]">
                {body}
              </button>
            ) : (
              <div className="flex w-full items-center gap-3 p-2.5">{body}</div>
            )}
          </li>
        );
      })}
    </ul>
  </section>
);

interface HeroProps {
  profile: ArtistProfile;
  isAdmin: boolean;
  missingCount: number;
  bulkBusy: boolean;
  onOpenLibrary: () => void;
  onRequestMissing: () => void;
}

const ProfileHero: React.FC<HeroProps> = React.memo(({ profile, isAdmin, missingCount, bulkBusy, onOpenLibrary, onRequestMissing }) => {
  const { artist, library } = profile;
  const showLibrary = isAdmin && library !== null && Boolean(artist.library_artist_id);
  return (
    <MachinedCard className="overflow-hidden">
      <div className="grid grid-cols-[7rem_minmax(0,1fr)] gap-x-3 p-2 sm:grid-cols-[9rem_minmax(0,1fr)] sm:gap-x-4 sm:p-4">
        <div className="relative flex h-28 w-28 items-center justify-center overflow-hidden rounded-[4px] border-2 border-[#e5a00d]/70 bg-[#1a1a1a] shadow-xl sm:h-36 sm:w-36">
          {artist.image_url ? (
            <img src={artist.image_url} alt={artist.name} width={144} height={144} loading="lazy" decoding="async" className="absolute inset-0 h-full w-full object-cover" />
          ) : (
            <User className="h-10 w-10 text-neutral-600" />
          )}
        </div>
        <div className="flex min-w-0 flex-col justify-between gap-2">
          <div className="min-w-0">
            <span className="hidden font-mono text-[10px] uppercase tracking-widest text-[#e5a00d] sm:block">Artist</span>
            <h2 className="line-clamp-2 break-words font-mono text-[17px] font-black leading-tight tracking-tight text-white sm:text-3xl">{artist.name}</h2>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            {showLibrary && library && (
              <span className="inline-flex h-[22px] items-center rounded-[2px] border border-emerald-500/40 bg-emerald-950/30 px-1.5 font-mono text-[11px] font-bold text-emerald-300">
                In Library · {library.album_count} {library.album_count === 1 ? 'album' : 'albums'}
                {library.monitored ? ' · monitored' : ''}
              </span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {showLibrary && (
              <TapeDeckButton size="sm" onClick={onOpenLibrary} icon={<Library className="h-3.5 w-3.5" />}>
                Open in Library
              </TapeDeckButton>
            )}
            {missingCount > 0 && (
              <TapeDeckButton
                size="sm"
                variant="amber"
                disabled={bulkBusy}
                onClick={onRequestMissing}
                icon={bulkBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
              >
                Request {missingCount} missing
              </TapeDeckButton>
            )}
          </div>
        </div>
      </div>
    </MachinedCard>
  );
});
ProfileHero.displayName = 'ProfileHero';

/** Discover > artist: hero, top tracks, grouped discography and (library-owned extras) for one artist. */
export const ArtistProfileView: React.FC<ArtistProfileViewProps> = ({
  discoveryId,
  isAdmin,
  onBack,
  onNavigate,
  onOpenAlbum,
  onOpenTrack,
  onPlayTrack,
  currentPreviewTrackId,
  isPreviewPlaying,
  onRequest,
  onRequestDiscography,
  requestedIds,
}) => {
  const target = useMemo(() => ({ discoveryId }), [discoveryId]);
  const { profile, isLoading, error, notFound, refetch } = useArtistProfile(target);
  const requests = useProfileRequests({ onRequest, onRequestDiscography, onDone: refetch });
  const { requestItem, requestMany, busyIds } = requests;

  const artistName = profile?.artist.name ?? '';
  const artistImage = profile?.artist.image_url ?? undefined;
  const libraryArtistId = isAdmin ? profile?.artist.library_artist_id ?? null : null;

  const missing = useMemo(
    () =>
      profile
        ? missingReleases([...profile.discography.albums, ...profile.discography.singles_eps], requestedIds)
        : [],
    [profile, requestedIds]
  );

  const handleRequestMissing = useCallback((): void => {
    void requestMany(artistName, missing.map(profileAlbumToDiscography));
  }, [requestMany, artistName, missing]);

  const handleOpenAlbum = useCallback(
    (album: ArtistProfileAlbum): void => {
      onOpenAlbum(profileAlbumToItem(album, effectiveStatus(album.status, album.id, requestedIds)));
    },
    [onOpenAlbum, requestedIds]
  );

  const handleRequestAlbum = useCallback(
    (album: ArtistProfileAlbum): void => {
      void requestItem({ ...profileAlbumToItem(album, album.status), album: album.title });
    },
    [requestItem]
  );

  const handlePlay = useCallback(
    (track: ArtistProfileTrack): void => {
      if (!track.preview_url) return;
      onPlayTrack({ id: track.id, title: track.title, artist: artistName, cover_url: artistImage, preview_url: track.preview_url });
    },
    [onPlayTrack, artistName, artistImage]
  );

  const handleOpenTrack = useCallback(
    (track: ArtistProfileTrack): void => {
      onOpenTrack(profileTrackToItem(track, artistName, artistImage));
    },
    [onOpenTrack, artistName, artistImage]
  );

  const handleRequestTrack = useCallback(
    (track: ArtistProfileTrack): void => {
      void requestItem(profileTrackToItem(track, artistName, artistImage));
    },
    [requestItem, artistName, artistImage]
  );

  const openLibrary = useCallback((): void => {
    if (libraryArtistId) onNavigate({ tab: 'library', sub: 'artists', detail: { artistId: String(libraryArtistId) } });
  }, [libraryArtistId, onNavigate]);

  const openLibraryAlbum = useCallback(
    (item: ArtistProfileLibraryOnly): void => {
      if (libraryArtistId && item.library_album_id != null) {
        onNavigate({
          tab: 'library',
          sub: 'artists',
          detail: { artistId: String(libraryArtistId), albumId: String(item.library_album_id) },
        });
      }
    },
    [libraryArtistId, onNavigate]
  );

  const header = <DetailHeaderBar parentLabel="Discover" title={artistName || undefined} onBack={onBack} />;

  if (isLoading) {
    return (
      <PageFrame nav={header} ariaLabel="Artist profile">
        <div className="flex flex-col items-center justify-center gap-3 py-20">
          <Loader2 className="h-8 w-8 animate-spin text-[#e5a00d]" />
          <span className="font-mono text-xs uppercase tracking-widest text-neutral-400">Loading artist...</span>
        </div>
      </PageFrame>
    );
  }

  if (!profile) {
    return (
      <PageFrame nav={header} ariaLabel="Artist profile">
        <div className="space-y-3 py-6">
          <StatusMessage variant="error">{notFound ? 'This artist could not be found.' : error ?? 'Failed to load artist profile'}</StatusMessage>
          <TapeDeckButton size="sm" onClick={onBack}>
            Back to Discover
          </TapeDeckButton>
        </div>
      </PageFrame>
    );
  }

  const { discography, top_tracks: topTracks } = profile;
  const libraryOnly = discography.library_only;

  return (
    <PageFrame nav={header} bodyClassName="space-y-5" ariaLabel="Artist profile">
      {requests.error && <StatusMessage variant="error">{requests.error}</StatusMessage>}
      {error && <StatusMessage variant="error">{error}</StatusMessage>}
      <ProfileHero
        profile={profile}
        isAdmin={isAdmin}
        missingCount={missing.length}
        bulkBusy={requests.bulkBusy}
        onOpenLibrary={openLibrary}
        onRequestMissing={handleRequestMissing}
      />

      {topTracks.length > 0 && (
        <section aria-label="Top tracks">
          <SectionHeading title="Top tracks" count={topTracks.length} />
          <ul className="divide-y divide-[#1f1f1f] overflow-hidden rounded-[4px] border border-[#1f1f1f]">
            {topTracks.map((track, idx) => (
              <ProfileTrackRow
                key={track.id}
                index={idx}
                track={track}
                status={effectiveStatus(track.status, track.id, requestedIds)}
                busy={busyIds.has(track.id)}
                isPlaying={isPreviewPlaying && currentPreviewTrackId === track.id}
                onPlay={handlePlay}
                onRequest={handleRequestTrack}
                onOpen={handleOpenTrack}
              />
            ))}
          </ul>
        </section>
      )}

      {GROUPS.map(({ key, title }) => {
        const albums = discography[key];
        if (albums.length === 0) return null;
        return (
          <section key={key} aria-label={title}>
            <SectionHeading title={title} count={albums.length} />
            <div className={GRID}>
              {albums.map((album) => (
                <ProfileAlbumCard
                  key={album.id}
                  album={album}
                  status={effectiveStatus(album.status, album.id, requestedIds)}
                  busy={busyIds.has(album.id)}
                  onOpen={handleOpenAlbum}
                  onRequest={handleRequestAlbum}
                />
              ))}
            </div>
          </section>
        );
      })}

      {libraryOnly.length > 0 && <LibraryOnlySection items={libraryOnly} onOpen={libraryArtistId ? openLibraryAlbum : undefined} />}

      {topTracks.length === 0 &&
        GROUPS.every(({ key }) => discography[key].length === 0) &&
        libraryOnly.length === 0 && (
          <p className="py-10 text-center font-mono text-sm text-neutral-500">No releases found for this artist.</p>
        )}
    </PageFrame>
  );
};
