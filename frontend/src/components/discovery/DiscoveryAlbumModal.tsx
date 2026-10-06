import React from 'react';
import { Check, Loader2, Pause, Play, Plus } from 'lucide-react';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import { IssueReportButton } from '@/components/issues';
import type { UseIssuesReturn } from '@/hooks/useIssues';
import type { DiscoveryAlbumTrack } from '@/hooks/useDiscoveryAlbum';
import type { AudioPreviewTrack, DiscoveryItem } from '@/types/models';
import { ArtistNameLink } from './ArtistNameLink';

export interface DiscoveryAlbumModalProps {
  album: DiscoveryItem;
  tracks: DiscoveryAlbumTrack[];
  isLoading: boolean;
  requestedIds: ReadonlySet<string>;
  requestingId: string | null;
  currentPreviewTrackId?: string;
  isPreviewPlaying: boolean;
  issuesHook: UseIssuesReturn;
  onClose: () => void;
  onPlayTrack: (track: AudioPreviewTrack) => void;
  onRequestAlbum: () => void;
  onRequestTrack: (item: DiscoveryItem) => void;
  /** Open the album artist's profile (only offered when the album carries `artist_discovery_id`). */
  onOpenArtist: (discoveryId: string) => void;
}

/** Album tracklist modal: header, full-album request in the footer and per-track preview/request rows. */
export const DiscoveryAlbumModal: React.FC<DiscoveryAlbumModalProps> = ({
  album,
  tracks,
  isLoading,
  requestedIds,
  requestingId,
  currentPreviewTrackId,
  isPreviewPlaying,
  issuesHook,
  onClose,
  onPlayTrack,
  onRequestAlbum,
  onRequestTrack,
  onOpenArtist,
}) => {
  const isInLibrary = Boolean(album.in_library || album.status === 'in_library' || album.status === 'available');
  const isRequested = Boolean(requestedIds.has(album.id) || album.requested || album.status === 'requested' || album.status === 'pending');
  const isProcessing = requestingId === album.id || album.status === 'processing';

  return (
    <ObsidianModal
      isOpen
      onClose={onClose}
      title={album.title}
      subtitle={
        <>
          By <ArtistNameLink name={album.artist} discoveryId={album.artist_discovery_id} onOpen={onOpenArtist} className="inline-block align-bottom" />
        </>
      }
      footer={
        <div className="flex w-full flex-col items-stretch justify-between gap-2 sm:flex-row sm:items-center">
          <span className="text-center font-mono text-xs text-neutral-400 sm:text-left">{tracks.length} Tracks</span>
          {isInLibrary ? (
            <TapeDeckButton variant="default" size="md" disabled className="border-emerald-500/30 bg-emerald-950/20 text-emerald-400" icon={<Check className="h-4 w-4" />}>
              In Library
            </TapeDeckButton>
          ) : isRequested ? (
            <TapeDeckButton variant="default" size="md" disabled className="border-[#e5a00d]/30 text-[#e5a00d]" icon={<Check className="h-4 w-4" />}>
              Album Requested
            </TapeDeckButton>
          ) : isProcessing ? (
            <TapeDeckButton variant="default" size="md" disabled className="border-blue-500/30 text-blue-400" icon={<Loader2 className="h-4 w-4 animate-spin" />}>
              Processing
            </TapeDeckButton>
          ) : (
            <TapeDeckButton variant="amber" size="md" onClick={onRequestAlbum} icon={<Plus className="h-4 w-4" />}>
              Request Full Album
            </TapeDeckButton>
          )}
        </div>
      }
    >
      <div className="space-y-4">
        <div className="flex items-center gap-4">
          <img
            src={album.cover_url || '/placeholder.svg'}
            alt=""
            width={80}
            height={80}
            decoding="async"
            className="h-20 w-20 rounded-[3px] border border-[#222222] object-cover"
          />
          <div className="min-w-0 space-y-1">
            <h4 className="text-sm font-bold text-white sm:text-base">{album.title}</h4>
            <p className="flex min-w-0 text-sm text-neutral-400">
              <ArtistNameLink name={album.artist} discoveryId={album.artist_discovery_id} onOpen={onOpenArtist} />
            </p>
            {album.release_date && <p className="font-mono text-xs text-neutral-500">Released: {album.release_date}</p>}
            {isInLibrary && (
              <>
                <span className="inline-flex items-center gap-1 rounded-[2px] bg-emerald-500/90 px-2 py-0.5 font-mono text-[10px] font-bold text-black">
                  <Check className="h-3 w-3" /> In Library
                </span>
                <div>
                  <IssueReportButton mediaTitle={album.title} artist={album.artist} issuesHook={issuesHook} />
                </div>
              </>
            )}
          </div>
        </div>

        {isLoading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="h-6 w-6 animate-spin text-[#e5a00d]" />
          </div>
        ) : tracks.length > 0 ? (
          <div className="divide-y divide-[#1f1f1f] overflow-hidden rounded-[4px] border border-[#1f1f1f]">
            {tracks.map((t, idx) => {
              const isPlaying = currentPreviewTrackId === t.id && isPreviewPlaying;
              return (
                <div key={t.id || idx} className="flex items-center justify-between p-2.5 transition-colors hover:bg-[#181818]">
                  <div className="flex min-w-0 items-center gap-3">
                    <span className="w-5 font-mono text-xs text-neutral-500">{idx + 1}</span>
                    <span className="truncate text-sm text-neutral-200">{t.title}</span>
                  </div>
                  <div className="flex flex-shrink-0 items-center gap-2">
                    {t.preview_url && (
                      <TapeDeckButton
                        size="sm"
                        aria-label={isPlaying ? `Pause preview of ${t.title}` : `Play preview of ${t.title}`}
                        onClick={() =>
                          onPlayTrack({ id: t.id, title: t.title, artist: album.artist, cover_url: album.cover_url, preview_url: t.preview_url ?? '' })
                        }
                        icon={isPlaying ? <Pause className="h-3 w-3 text-[#e5a00d]" /> : <Play className="h-3 w-3 fill-current" />}
                      />
                    )}
                    {requestedIds.has(t.id) ? (
                      <span className="rounded-[2px] border border-[#e5a00d]/30 bg-[#e5a00d]/10 px-2 py-1 font-mono text-[10px] text-[#e5a00d]">Requested</span>
                    ) : requestingId === t.id ? (
                      <TapeDeckButton size="sm" variant="default" disabled aria-label="Requesting track" icon={<Loader2 className="h-3 w-3 animate-spin" />}>
                        ...
                      </TapeDeckButton>
                    ) : (
                      <TapeDeckButton
                        size="sm"
                        variant="amber"
                        onClick={() =>
                          onRequestTrack({
                            id: t.id,
                            title: t.title,
                            artist: album.artist,
                            album: album.title,
                            cover_url: album.cover_url,
                            type: 'track',
                          })
                        }
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
          <p className="py-4 font-mono text-xs text-neutral-500">No individual tracklist details available.</p>
        )}
      </div>
    </ObsidianModal>
  );
};
