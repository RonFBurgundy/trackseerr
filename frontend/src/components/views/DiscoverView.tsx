import React, { useState } from 'react';
import { Play, Pause, Plus, Check, Disc, Music, Loader2 } from 'lucide-react';
import type { ArtistDiscographyAlbum, DiscoveryItem, AudioPreviewTrack } from '@/types/models';
import { ArtistDiscographyModal } from '@/components/discovery';
import type { UseDiscoveryReturn } from '@/hooks/useDiscovery';
import {
  TabStrip,
  TapeDeckButton,
  MachinedCard,
  SearchBar,
  ObsidianModal,
} from '@/components/ui';
import { PageFrame } from '@/components/layout';
import { IssueReportButton } from '@/components/issues';
import type { UseIssuesReturn } from '@/hooks/useIssues';
import { getDiscoveryAlbumDetail } from '@/services/discoveryService';

export interface DiscoverViewProps {
  discovery: UseDiscoveryReturn;
  onPlayTrack: (track: AudioPreviewTrack) => void;
  currentPreviewTrackId?: string;
  isPreviewPlaying?: boolean;
  onRequest: (item: DiscoveryItem) => Promise<void>;
  /** POST /api/requests/batch with kind=discography. Throws the server's quota/duplicate message. */
  onRequestDiscography: (artist: string, albums: ArtistDiscographyAlbum[]) => Promise<void>;
  requestedIds: Set<string>;
  issuesHook: UseIssuesReturn;
}

export const DiscoverView: React.FC<DiscoverViewProps> = ({
  discovery,
  onPlayTrack,
  currentPreviewTrackId,
  isPreviewPlaying = false,
  onRequest,
  onRequestDiscography,
  requestedIds,
  issuesHook,
}) => {
  const [selectedAlbum, setSelectedAlbum] = useState<DiscoveryItem | null>(null);
  const [selectedArtist, setSelectedArtist] = useState<DiscoveryItem | null>(null);
  const [albumDetails, setAlbumDetails] = useState<{
    tracks?: Array<{ id: string; title: string; duration_ms?: number; preview_url?: string }>;
  } | null>(null);
  const [isLoadingAlbum, setIsLoadingAlbum] = useState<boolean>(false);
  const [requestingId, setRequestingId] = useState<string | null>(null);

  const handleOpenAlbum = async (item: DiscoveryItem) => {
    if (item.type === 'artist') {
      setSelectedArtist(item);
      return;
    }
    setSelectedAlbum(item);
    setIsLoadingAlbum(true);
    try {
      const data = await getDiscoveryAlbumDetail(item.id);
      setAlbumDetails(data as { tracks?: Array<{ id: string; title: string; duration_ms?: number; preview_url?: string }> });
    } catch {
      setAlbumDetails(null);
    } finally {
      setIsLoadingAlbum(false);
    }
  };

  const handleRequestClick = async (e: React.MouseEvent, item: DiscoveryItem) => {
    e.stopPropagation();
    setRequestingId(item.id);
    try {
      await onRequest(item);
    } finally {
      setRequestingId(null);
    }
  };

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
            const isInLibrary = Boolean(item.in_library || item.status === 'in_library' || item.status === 'available');
            const isRequested = Boolean(requestedIds.has(item.id) || item.requested || item.status === 'requested' || item.status === 'pending');
            const isProcessing = Boolean(requestingId === item.id || item.status === 'processing');
            const isPlayingThis = currentPreviewTrackId === item.id && isPreviewPlaying;

            return (
              <MachinedCard
                key={item.id}
                interactive
                onClick={() => handleOpenAlbum(item)}
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
                          artist: item.artist,
                          cover_url: item.cover_url,
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
                    <p className="text-[11px] sm:text-xs text-neutral-400 truncate" title={item.artist}>
                      {item.artist}
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

      <ArtistDiscographyModal
        artist={selectedArtist}
        onClose={() => setSelectedArtist(null)}
        onRequestDiscography={onRequestDiscography}
      />

      {/* Album Tracklist Modal */}
      {selectedAlbum && (
        <ObsidianModal
          isOpen={Boolean(selectedAlbum)}
          onClose={() => {
            setSelectedAlbum(null);
            setAlbumDetails(null);
          }}
          title={selectedAlbum.title}
          subtitle={`By ${selectedAlbum.artist}`}
          footer={
            <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-2 w-full">
              <span className="text-xs text-neutral-400 font-mono text-center sm:text-left">
                {albumDetails?.tracks?.length || 0} Tracks
              </span>
              {selectedAlbum.in_library || selectedAlbum.status === 'in_library' || selectedAlbum.status === 'available' ? (
                <TapeDeckButton
                  variant="default"
                  size="md"
                  disabled
                  className="text-emerald-400 border-emerald-500/30 bg-emerald-950/20"
                  icon={<Check className="h-4 w-4" />}
                >
                  In Library
                </TapeDeckButton>
              ) : requestedIds.has(selectedAlbum.id) || selectedAlbum.requested || selectedAlbum.status === 'requested' || selectedAlbum.status === 'pending' ? (
                <TapeDeckButton
                  variant="default"
                  size="md"
                  disabled
                  className="text-[#e5a00d] border-[#e5a00d]/30"
                  icon={<Check className="h-4 w-4" />}
                >
                  Album Requested
                </TapeDeckButton>
              ) : requestingId === selectedAlbum.id || selectedAlbum.status === 'processing' ? (
                <TapeDeckButton
                  variant="default"
                  size="md"
                  disabled
                  className="text-blue-400 border-blue-500/30"
                  icon={<Loader2 className="h-4 w-4 animate-spin" />}
                >
                  Processing
                </TapeDeckButton>
              ) : (
                <TapeDeckButton
                  variant="amber"
                  size="md"
                  onClick={(e) => handleRequestClick(e, selectedAlbum)}
                  icon={<Plus className="h-4 w-4" />}
                >
                  Request Full Album
                </TapeDeckButton>
              )}
            </div>
          }
        >
          <div className="space-y-4">
            <div className="flex items-center gap-4">
              <img
                src={selectedAlbum.cover_url || '/placeholder.svg'}
                alt=""
                className="h-20 w-20 rounded-[3px] object-cover border border-[#222222]"
              />
              <div className="space-y-1">
                <h4 className="font-bold text-white text-sm sm:text-base">{selectedAlbum.title}</h4>
                <p className="text-sm text-neutral-400">{selectedAlbum.artist}</p>
                {selectedAlbum.release_date && (
                  <p className="text-xs text-neutral-500 font-mono">
                    Released: {selectedAlbum.release_date}
                  </p>
                )}
                {Boolean(selectedAlbum.in_library || selectedAlbum.status === 'in_library' || selectedAlbum.status === 'available') && (
                  <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-[2px] text-[10px] font-mono font-bold bg-emerald-500/90 text-black">
                    <Check className="h-3 w-3" /> In Library
                  </span>
                )}
                {Boolean(selectedAlbum.in_library || selectedAlbum.status === 'in_library' || selectedAlbum.status === 'available') && (
                  <div>
                    <IssueReportButton
                      mediaTitle={selectedAlbum.title}
                      artist={selectedAlbum.artist}
                      issuesHook={issuesHook}
                    />
                  </div>
                )}
              </div>
            </div>

            {isLoadingAlbum ? (
              <div className="flex justify-center py-8">
                <Loader2 className="h-6 w-6 text-[#e5a00d] animate-spin" />
              </div>
            ) : albumDetails?.tracks && albumDetails.tracks.length > 0 ? (
              <div className="divide-y divide-[#1f1f1f] border border-[#1f1f1f] rounded-[4px] overflow-hidden">
                {albumDetails.tracks.map((t, idx) => {
                  const isTrackRequested = requestedIds.has(t.id);
                  const isTrackProcessing = requestingId === t.id;

                  const handleRequestSingleTrack = async () => {
                    setRequestingId(t.id);
                    try {
                      await onRequest({
                        id: t.id,
                        title: t.title,
                        artist: selectedAlbum.artist,
                        album: selectedAlbum.title,
                        cover_url: selectedAlbum.cover_url,
                        type: 'track',
                      });
                    } finally {
                      setRequestingId(null);
                    }
                  };

                  return (
                    <div
                      key={t.id || idx}
                      className="flex items-center justify-between p-2.5 hover:bg-[#181818] transition-colors"
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        <span className="font-mono text-xs text-neutral-500 w-5">
                          {idx + 1}
                        </span>
                        <span className="text-sm text-neutral-200 truncate">{t.title}</span>
                      </div>

                      <div className="flex items-center gap-2 flex-shrink-0">
                        {t.preview_url && (
                          <TapeDeckButton
                            size="sm"
                            aria-label={
                              currentPreviewTrackId === t.id && isPreviewPlaying
                                ? `Pause preview of ${t.title}`
                                : `Play preview of ${t.title}`
                            }
                            onClick={() =>
                              onPlayTrack({
                                id: t.id,
                                title: t.title,
                                artist: selectedAlbum.artist,
                                cover_url: selectedAlbum.cover_url,
                                preview_url: t.preview_url!,
                              })
                            }
                            icon={
                              currentPreviewTrackId === t.id && isPreviewPlaying ? (
                                <Pause className="h-3 w-3 text-[#e5a00d]" />
                              ) : (
                                <Play className="h-3 w-3 fill-current" />
                              )
                            }
                          />
                        )}

                        {isTrackRequested ? (
                          <span className="text-[10px] font-mono text-[#e5a00d] px-2 py-1 border border-[#e5a00d]/30 rounded-[2px] bg-[#e5a00d]/10">
                            Requested
                          </span>
                        ) : isTrackProcessing ? (
                          <TapeDeckButton size="sm" variant="default" disabled aria-label="Requesting track" icon={<Loader2 className="h-3 w-3 animate-spin" />}>
                            ...
                          </TapeDeckButton>
                        ) : (
                          <TapeDeckButton
                            size="sm"
                            variant="amber"
                            onClick={handleRequestSingleTrack}
                            icon={<Plus className="h-3 w-3" />}
                          >
                            Request
                          </TapeDeckButton>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-xs text-neutral-500 font-mono py-4">
                No individual tracklist details available.
              </p>
            )}
          </div>
        </ObsidianModal>
      )}
    </PageFrame>
  );
};
