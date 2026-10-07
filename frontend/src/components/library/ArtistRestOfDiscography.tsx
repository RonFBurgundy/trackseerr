import React, { useCallback, useMemo } from 'react';
import { Compass, Disc, Loader2, Plus } from 'lucide-react';
import { MachinedCard, StatusMessage, TapeDeckButton } from '@/components/ui';
import {
  DiscoveryAlbumModal,
  DiscoveryTrackModal,
  ProfileRequestButton,
  ProfileStatusBadge,
  profileAlbumToDiscography,
  profileAlbumToItem,
  releaseYear,
} from '@/components/discovery';
import { useArtistProfile } from '@/hooks/useArtistProfile';
import { useDiscoveryAlbum } from '@/hooks/useDiscoveryAlbum';
import { useDiscoveryTrack } from '@/hooks/useDiscoveryTrack';
import { useProfileRequests } from '@/hooks/useProfileRequests';
import type { UseIssuesReturn } from '@/hooks/useIssues';
import type { ArtistDiscographyAlbum, ArtistProfileAlbum, AudioPreviewTrack, DiscoveryItem } from '@/types/models';

export interface ArtistRestOfDiscographyProps {
  /** Library artist id of the page this renders on (admin-only endpoint). */
  libraryArtistId: string;
  onRequestItem: (item: DiscoveryItem) => Promise<void>;
  onRequestDiscography: (artist: string, albums: ArtistDiscographyAlbum[]) => Promise<void>;
  onViewInDiscover: (discoveryId: string) => void;
  /** Release detail modal wiring, shared with Discover. */
  onPlayTrack: (track: AudioPreviewTrack) => void;
  currentPreviewTrackId?: string;
  isPreviewPlaying: boolean;
  requestedIds: ReadonlySet<string>;
  issuesHook: UseIssuesReturn;
}

/** Statuses that still offer the add button (and join the bulk add). */
const ACTIONABLE: ReadonlySet<string> = new Set(['missing', 'none', 'rejected']);

/**
 * Admin-only artist page section: known releases of the artist that are not matched to any library album row, i.e. the
 * ones the artist's metadata profile filters out (or that were never added). Monitored-but-missing albums already sit in
 * the library list above, so they are excluded here by `library_album_id`, not by status. Each can be inspected (tracklist)
 * and added and monitored in place.
 */
export const ArtistRestOfDiscography: React.FC<ArtistRestOfDiscographyProps> = ({
  libraryArtistId,
  onRequestItem,
  onRequestDiscography,
  onViewInDiscover,
  onPlayTrack,
  currentPreviewTrackId,
  isPreviewPlaying,
  requestedIds,
  issuesHook,
}) => {
  const target = useMemo(() => ({ libraryArtistId }), [libraryArtistId]);
  const { profile, isLoading, error, refetch } = useArtistProfile(target);
  const requests = useProfileRequests({ onRequest: onRequestItem, onRequestDiscography, onDone: refetch });
  const { requestItem, requestMany, busyIds } = requests;
  const albumDetail = useDiscoveryAlbum();
  const { open: openAlbum, close: closeAlbum } = albumDetail;
  const trackDetail = useDiscoveryTrack();
  const { open: openTrack, close: closeTrack } = trackDetail;

  const releases = useMemo<ArtistProfileAlbum[]>(() => {
    if (!profile) return [];
    const { albums, singles_eps: singles, compilations } = profile.discography;
    return [...albums, ...singles, ...compilations].filter((a) => !a.library_album_id);
  }, [profile]);

  const requestable = useMemo(() => releases.filter((a) => ACTIONABLE.has(a.status)), [releases]);
  const artistName = profile?.artist.name ?? '';
  const discoveryId = profile?.artist.discovery_id ?? null;

  const openedTrack = trackDetail.track;
  const currentAlbumId = albumDetail.album?.id;
  /** From the track modal: show the track's album, reusing the album modal when already open on it. */
  const openAlbumOfTrack = useCallback(
    (albumDiscoveryId: string): void => {
      closeTrack();
      if (openedTrack && currentAlbumId !== albumDiscoveryId) {
        void openAlbum({ ...openedTrack, album_discovery_id: albumDiscoveryId });
      }
    },
    [openedTrack, currentAlbumId, closeTrack, openAlbum]
  );

  const viewInDiscover = useCallback(
    (id: string): void => {
      closeTrack();
      closeAlbum();
      onViewInDiscover(id);
    },
    [closeTrack, closeAlbum, onViewInDiscover]
  );

  const addRelease = useCallback(
    (item: DiscoveryItem): void => {
      void requestItem({ ...item, album: item.type === 'album' ? item.title : item.album });
    },
    [requestItem]
  );

  const handleRequestAll = useCallback((): void => {
    void requestMany(artistName, requestable.map(profileAlbumToDiscography));
  }, [requestMany, artistName, requestable]);

  return (
    <MachinedCard className="overflow-hidden" aria-label="Rest of discography">
      <div className="flex flex-col gap-2 border-b border-[#1f1f1f] p-2 sm:flex-row sm:items-center sm:gap-3 sm:p-3">
        <div className="min-w-0 sm:flex-1">
          <h3 className="font-mono text-xs font-bold uppercase tracking-widest text-[#e5a00d]">
            Rest of discography
            {!isLoading && profile && <span className="ml-2 text-neutral-500">{releases.length}</span>}
          </h3>
          <p className="text-[11px] text-neutral-500">Releases your metadata profile filters out.</p>
        </div>
        {(discoveryId || requestable.length > 1) && (
          <div className="flex flex-wrap items-center gap-2 sm:shrink-0">
            {discoveryId && (
              <TapeDeckButton size="sm" onClick={() => onViewInDiscover(discoveryId)} icon={<Compass className="h-3.5 w-3.5" />}>
                View in Discover
              </TapeDeckButton>
            )}
            {requestable.length > 1 && (
              <TapeDeckButton
                size="sm"
                variant="amber"
                disabled={requests.bulkBusy}
                onClick={handleRequestAll}
                icon={requests.bulkBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
              >
                Add all
              </TapeDeckButton>
            )}
          </div>
        )}
      </div>
      <div className="space-y-2 p-2 sm:p-3">
        {requests.error && <StatusMessage variant="error">{requests.error}</StatusMessage>}
        {isLoading ? (
          <div className="flex justify-center py-6">
            <Loader2 className="h-5 w-5 animate-spin text-[#e5a00d]" />
          </div>
        ) : error && !profile ? (
          <StatusMessage variant="error">{error}</StatusMessage>
        ) : releases.length === 0 ? (
          <p className="py-3 text-center font-mono text-xs text-neutral-500">
            {discoveryId ? 'Nothing filtered out — every known release is in the library.' : 'No matching discovery artist, so the wider discography is unknown.'}
          </p>
        ) : (
          <ul className="divide-y divide-[#1f1f1f] overflow-hidden rounded-[4px] border border-[#1f1f1f]">
            {releases.map((a) => {
              const meta = [releaseYear(a.release_date), a.record_type].filter(Boolean).join(' / ');
              return (
                <li key={a.id} className="flex items-center gap-2 p-2 sm:gap-3 sm:p-2.5">
                  <button
                    type="button"
                    aria-label={`View tracks of ${a.title}`}
                    onClick={() => void openAlbum(profileAlbumToItem(a, a.status))}
                    className="flex min-w-0 flex-1 items-center gap-2 rounded-[3px] text-left hover:bg-[#141414] focus-visible:outline focus-visible:outline-1 focus-visible:outline-[#e5a00d] sm:gap-3"
                  >
                    {a.cover_url ? (
                      <img src={a.cover_url} alt="" width={40} height={40} loading="lazy" decoding="async" className="h-10 w-10 shrink-0 rounded-[3px] border border-[#222222] object-cover" />
                    ) : (
                      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[3px] bg-[#141414]">
                        <Disc className="h-5 w-5 text-neutral-600" />
                      </div>
                    )}
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm text-neutral-200" title={a.title}>
                        {a.title}
                      </p>
                      {meta && <p className="truncate font-mono text-[11px] text-neutral-500">{meta}</p>}
                    </div>
                  </button>
                  <ProfileStatusBadge status={a.status} className="hidden sm:inline-flex" />
                  <ProfileRequestButton
                    status={a.status}
                    busy={busyIds.has(a.id)}
                    label="Add & monitor"
                    onRequest={() => addRelease(profileAlbumToItem(a, a.status))}
                    className="shrink-0"
                  />
                </li>
              );
            })}
          </ul>
        )}
      </div>
      {albumDetail.album && (
        <DiscoveryAlbumModal
          album={albumDetail.album}
          tracks={albumDetail.tracks}
          isLoading={albumDetail.isLoading}
          requestedIds={requestedIds}
          requestingId={busyIds.has(albumDetail.album.id) ? albumDetail.album.id : null}
          currentPreviewTrackId={currentPreviewTrackId}
          isPreviewPlaying={isPreviewPlaying}
          issuesHook={issuesHook}
          onClose={closeAlbum}
          onPlayTrack={onPlayTrack}
          onRequestAlbum={() => albumDetail.album && addRelease(albumDetail.album)}
          onRequestTrack={addRelease}
          onOpenTrack={(item) => void openTrack(item)}
          onOpenArtist={viewInDiscover}
        />
      )}
      {openedTrack && (
        <DiscoveryTrackModal
          track={openedTrack}
          detail={trackDetail.detail}
          isLoading={trackDetail.isLoading}
          error={trackDetail.error}
          requestedIds={requestedIds}
          requestingId={busyIds.has(openedTrack.id) ? openedTrack.id : null}
          currentPreviewTrackId={currentPreviewTrackId}
          isPreviewPlaying={isPreviewPlaying}
          issuesHook={issuesHook}
          onClose={closeTrack}
          onPlayTrack={onPlayTrack}
          onRequestTrack={addRelease}
          onRequestAlbum={addRelease}
          onOpenArtist={viewInDiscover}
          onOpenAlbum={openAlbumOfTrack}
        />
      )}
    </MachinedCard>
  );
};
