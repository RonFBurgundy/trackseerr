import React, { useCallback, useState } from 'react';
import { Play, Pause, Plus, Check, Disc, Music, Loader2 } from 'lucide-react';
import type { ArtistDiscographyAlbum, DiscoveryItem, AudioPreviewTrack } from '@/types/models';
import { ArtistNameLink, ArtistProfileView, DiscoveryAlbumModal, DiscoveryTrackModal } from '@/components/discovery';
import type { UseDiscoveryReturn } from '@/hooks/useDiscovery';
import { useDiscoveryAlbum } from '@/hooks/useDiscoveryAlbum';
import { useDiscoveryTrack } from '@/hooks/useDiscoveryTrack';
import type { AppRoute, DiscoverRoute } from '@/hooks/useAppRoute';
import {
  TabStrip,
  TapeDeckButton,
  MachinedCard,
  SearchBar,
} from '@/components/ui';
import { PageFrame } from '@/components/layout';
import type { UseIssuesReturn } from '@/hooks/useIssues';

export interface DiscoverViewProps {
  discovery: UseDiscoveryReturn;
  /** Current discover route; `artistId` drills into an artist profile. */
  route: DiscoverRoute;
  onNavigate: (route: AppRoute) => void;
  /** Step up to `parent`: history back when the previous entry is that parent, otherwise replace with it. */
  onNavigateUp: (parent: AppRoute) => void;
  /** Library links and chips on the profile render only for admins. */
  isAdmin: boolean;
  onPlayTrack: (track: AudioPreviewTrack) => void;
  currentPreviewTrackId?: string;
  isPreviewPlaying?: boolean;
  onRequest: (item: DiscoveryItem) => Promise<void>;
  /** POST /api/requests/batch with kind=discography. Throws the server's quota/duplicate message. */
  onRequestDiscography: (artist: string, albums: ArtistDiscographyAlbum[]) => Promise<void>;
  requestedIds: Set<string>;
  issuesHook: UseIssuesReturn;
}

const DISCOVER_HOME: DiscoverRoute = { tab: 'discover' };

export const DiscoverView: React.FC<DiscoverViewProps> = ({
  discovery,
  route,
  onNavigate,
  onNavigateUp,
  isAdmin,
  onPlayTrack,
  currentPreviewTrackId,
  isPreviewPlaying = false,
  onRequest,
  onRequestDiscography,
  requestedIds,
  issuesHook,
}) => {
  const album = useDiscoveryAlbum();
  const { open: openAlbum, close: closeAlbum } = album;
  const trackDetail = useDiscoveryTrack();
  const { open: openTrack, close: closeTrack } = trackDetail;
  const [requestingId, setRequestingId] = useState<string | null>(null);

  const openArtist = useCallback(
    (discoveryId: string): void => {
      closeTrack();
      closeAlbum();
      onNavigate({ tab: 'discover', artistId: discoveryId });
    },
    [closeAlbum, closeTrack, onNavigate]
  );

  const currentAlbumId = album.album?.id;
  const openedTrack = trackDetail.track;
  /** From the track modal: show the track's album, reusing the album modal when it is already open on it. */
  const openAlbumOfTrack = useCallback(
    (albumDiscoveryId: string): void => {
      const source = openedTrack;
      closeTrack();
      if (source && currentAlbumId !== albumDiscoveryId) {
        void openAlbum({ ...source, album_discovery_id: albumDiscoveryId });
      }
    },
    [openedTrack, currentAlbumId, closeTrack, openAlbum]
  );

  const handleOpenItem = (item: DiscoveryItem): void => {
    if (item.type === 'artist') {
      openArtist(item.id);
      return;
    }
    if (item.type === 'track') {
      void openTrack(item);
      return;
    }
    void openAlbum(item);
  };

  const requestWithBusy = async (item: DiscoveryItem): Promise<void> => {
    setRequestingId(item.id);
    try {
      await onRequest(item);
    } finally {
      setRequestingId(null);
    }
  };

  const handleRequestClick = async (e: React.MouseEvent, item: DiscoveryItem) => {
    e.stopPropagation();
    await requestWithBusy(item);
  };

  const current = album.album;
  const albumModal = current && (
    <DiscoveryAlbumModal
      album={current}
      tracks={album.tracks}
      isLoading={album.isLoading}
      requestedIds={requestedIds}
      requestingId={requestingId}
      currentPreviewTrackId={currentPreviewTrackId}
      isPreviewPlaying={isPreviewPlaying}
      issuesHook={issuesHook}
      onClose={closeAlbum}
      onPlayTrack={onPlayTrack}
      onRequestAlbum={() => void requestWithBusy(current)}
      onRequestTrack={(item) => void requestWithBusy(item)}
      onOpenTrack={(item) => void openTrack(item)}
      onOpenArtist={openArtist}
    />
  );

  const trackItem = trackDetail.track;
  const trackModal = trackItem && (
    <DiscoveryTrackModal
      track={trackItem}
      detail={trackDetail.detail}
      isLoading={trackDetail.isLoading}
      error={trackDetail.error}
      requestedIds={requestedIds}
      requestingId={requestingId}
      currentPreviewTrackId={currentPreviewTrackId}
      isPreviewPlaying={isPreviewPlaying}
      issuesHook={issuesHook}
      onClose={closeTrack}
      onPlayTrack={onPlayTrack}
      onRequestTrack={(item) => void requestWithBusy(item)}
      onRequestAlbum={(item) => void requestWithBusy(item)}
      onOpenArtist={openArtist}
      onOpenAlbum={openAlbumOfTrack}
    />
  );

  if (route.artistId) {
    return (
      <>
        <ArtistProfileView
          key={route.artistId}
          discoveryId={route.artistId}
          isAdmin={isAdmin}
          onBack={() => onNavigateUp(DISCOVER_HOME)}
          onNavigate={onNavigate}
          onOpenAlbum={handleOpenItem}
          onOpenTrack={(item) => void openTrack(item)}
          onPlayTrack={onPlayTrack}
          currentPreviewTrackId={currentPreviewTrackId}
          isPreviewPlaying={isPreviewPlaying}
          onRequest={onRequest}
          onRequestDiscography={onRequestDiscography}
          requestedIds={requestedIds}
        />
        {albumModal}
        {trackModal}
      </>
    );
  }

  return (
    <PageFrame
      bodyClassName="space-y-6"
      ariaLabel="Discover results"
      nav={
        <TabStrip fill>
          <TapeDeckButton
            size="sm"
            active={discovery.category === 'trending'}
            onClick={() => {
              discovery.clearSearch();
              discovery.setCategory('trending');
            }}
            icon={<Disc className="h-3.5 w-3.5" />}
          >
            Trending
          </TapeDeckButton>
          <TapeDeckButton
            size="sm"
            active={discovery.category === 'new_releases'}
            onClick={() => {
              discovery.clearSearch();
              discovery.setCategory('new_releases');
            }}
            icon={<Music className="h-3.5 w-3.5" />}
          >
            New Releases
          </TapeDeckButton>
        </TabStrip>
      }
      actions={
        <div className="max-w-lg">
          <SearchBar
            value={discovery.query}
            onChange={(val) => {
              if (!val) discovery.clearSearch();
              else discovery.search(val);
            }}
            placeholder="Search albums, artists, or tracks..."
          />
        </div>
      }
    >
      {/* Loading state */}
      {discovery.isLoading && (
        <div className="flex flex-col items-center justify-center py-20 gap-3">
          <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
          <span className="text-xs uppercase tracking-widest text-neutral-400 font-mono">
            Scanning Analog Frequencies...
          </span>
        </div>
      )}

      {/* Error state */}
      {discovery.error && !discovery.isLoading && (
        <div className="p-4 bg-red-950/40 border border-red-800/50 rounded-[4px] text-xs text-red-300 font-mono">
          {discovery.error}
        </div>
      )}

      {/* Items Grid */}
      {!discovery.isLoading && discovery.items.length === 0 && !discovery.error && (
        <div className="text-center py-16 text-neutral-500 font-mono text-sm">
          No releases found for this query.
        </div>
      )}

      {!discovery.isLoading && discovery.items.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
          {discovery.items.map((item) => {
            const isInLibrary = Boolean(item.status === 'in_library' || item.status === 'available');
            const isRequested = Boolean(requestedIds.has(item.id) || item.status === 'requested' || item.status === 'pending');
            const isProcessing = Boolean(requestingId === item.id || item.status === 'processing');
            const isPlayingThis = currentPreviewTrackId === item.id && isPreviewPlaying;

            return (
              <MachinedCard
                key={item.id}
                interactive
                onClick={() => handleOpenItem(item)}
                className="group flex flex-col overflow-hidden"
              >
                {/* Artwork with overlay transport controls and status badges */}
                <div className="relative aspect-square w-full bg-[#1c1c1c] overflow-hidden">
                  {item.cover_url ? (
                    <img
                      src={item.cover_url}
                      alt={item.title}
                      className="w-full h-full object-cover transition-transform duration-300 group-hover:scale-105"
                      loading="lazy"
                      onError={(e) => {
                        (e.currentTarget as HTMLImageElement).src = '/placeholder.svg';
                      }}
                    />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center bg-[#151515]">
                      <Disc className="h-10 w-10 text-neutral-600" />
                    </div>
                  )}

                  {/* Top-left Status Badges */}
                  {isInLibrary ? (
                    <span className="absolute top-2 left-2 px-2 py-0.5 rounded-[2px] text-[10px] font-mono font-bold bg-emerald-500/90 text-black flex items-center gap-1 shadow-md z-10">
                      <Check className="h-3 w-3" /> In Library
                    </span>
                  ) : isRequested ? (
                    <span className="absolute top-2 left-2 px-2 py-0.5 rounded-[2px] text-[10px] font-mono font-bold bg-[#e5a00d]/90 text-black shadow-md z-10">
                      Requested
                    </span>
                  ) : isProcessing ? (
                    <span className="absolute top-2 left-2 px-2 py-0.5 rounded-[2px] text-[10px] font-mono font-bold bg-blue-500/90 text-white flex items-center gap-1 shadow-md z-10">
                      <span className="w-1.5 h-1.5 rounded-full bg-white animate-ping" /> Processing
                    </span>
                  ) : null}

                  {/* 30s Preview Key Overlay */}
                  {item.preview_url && (
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        onPlayTrack({
                          id: item.id,
                          title: item.title,
                          artist: item.artist ?? '',
                          cover_url: item.cover_url ?? undefined,
                          preview_url: item.preview_url!,
                        });
                      }}
                      className="absolute bottom-2 left-2 inline-flex h-9 w-9 items-center justify-center rounded-[3px] bg-black/80 hover:bg-[#e5a00d] text-white hover:text-black border border-[#2a2a2a] shadow-lg transition-colors z-10"
                      aria-label="Toggle 30s preview"
                    >
                      {isPlayingThis ? (
                        <Pause className="h-3.5 w-3.5" />
                      ) : (
                        <Play className="h-3.5 w-3.5 fill-current" />
                      )}
                    </button>
                  )}
                </div>

                {/* Info & Action */}
                <div className="p-3 flex flex-col flex-1 justify-between gap-2">
                  <div className="min-w-0">
                    <h3 className="font-bold text-xs sm:text-sm text-white truncate" title={item.title}>
                      {item.title}
                    </h3>
                    <p className="flex min-w-0 text-[11px] sm:text-xs text-neutral-400">
                      <ArtistNameLink name={item.artist} discoveryId={item.artist_discovery_id} onOpen={openArtist} />
                    </p>
                  </div>

                  {isInLibrary ? (
                    <TapeDeckButton
                      size="sm"
                      variant="default"
                      disabled
                      className="w-full text-emerald-400 border-emerald-500/30 bg-emerald-950/20"
                      icon={<Check className="h-3.5 w-3.5" />}
                    >
                      In Library
                    </TapeDeckButton>
                  ) : isRequested ? (
                    <TapeDeckButton
                      size="sm"
                      variant="default"
                      disabled
                      className="w-full text-[#e5a00d] border-[#e5a00d]/30"
                      icon={<Check className="h-3.5 w-3.5" />}
                    >
                      Requested
                    </TapeDeckButton>
                  ) : isProcessing ? (
                    <TapeDeckButton
                      size="sm"
                      variant="default"
                      disabled
                      className="w-full text-blue-400 border-blue-500/30"
                      icon={<Loader2 className="h-3.5 w-3.5 animate-spin" />}
                    >
                      Processing
                    </TapeDeckButton>
                  ) : (
                    <TapeDeckButton
                      size="sm"
                      variant="amber"
                      onClick={(e) => handleRequestClick(e, item)}
                      className="w-full"
                      icon={<Plus className="h-3.5 w-3.5" />}
                    >
                      Request
                    </TapeDeckButton>
                  )}
                </div>
              </MachinedCard>
            );
          })}
        </div>
      )}

      {albumModal}
      {trackModal}
    </PageFrame>
  );
};
