import React from 'react';
import { Check, Loader2, Pause, Play, Plus } from 'lucide-react';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import { IssueReportButton } from '@/components/issues';
import type { UseIssuesReturn } from '@/hooks/useIssues';
import type { AudioPreviewTrack, DiscoveryItem, DiscoveryTrackDetail } from '@/types/models';
import { MEDIA_ISSUE_TYPES } from '@/types/models';
import { ArtistNameLink } from './ArtistNameLink';
import { ProfileRequestButton } from './ProfileRequestButton';
import { ProfileStatusBadge } from './ProfileStatusBadge';
import { effectiveStatus } from './profileItems';

export interface DiscoveryTrackModalProps {
  /** The item the modal was opened with; fills the header while `detail` loads or when it fails. */
  track: DiscoveryItem;
  detail: DiscoveryTrackDetail | null;
  isLoading: boolean;
  error: string | null;
  requestedIds: ReadonlySet<string>;
  requestingId: string | null;
  currentPreviewTrackId?: string;
  isPreviewPlaying: boolean;
  issuesHook: UseIssuesReturn;
  onClose: () => void;
  onPlayTrack: (track: AudioPreviewTrack) => void;
  /** Request this single track (item carries `type: 'track'`). */
  onRequestTrack: (item: DiscoveryItem) => void;
  /** Request the whole album the track is on (item carries `type: 'album'`). */
  onRequestAlbum: (item: DiscoveryItem) => void;
  onOpenArtist: (discoveryId: string) => void;
  onOpenAlbum: (albumDiscoveryId: string) => void;
}

function formatTrackDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return '';
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

interface MetaCellProps {
  label: string;
  children: React.ReactNode;
}

const MetaCell: React.FC<MetaCellProps> = ({ label, children }) => (
  <div className="min-w-0 rounded-[3px] border border-[#1f1f1f] bg-[#0d0d0d] px-2.5 py-2">
    <dt className="font-mono text-[10px] uppercase tracking-wider text-neutral-500">{label}</dt>
    <dd className="mt-0.5 break-words text-xs text-neutral-200">{children}</dd>
  </div>
);

/** Track detail: cover, artist/album links, metadata grid, preview and request actions. No library management. */
export const DiscoveryTrackModal: React.FC<DiscoveryTrackModalProps> = ({
  track,
  detail,
  isLoading,
  error,
  requestedIds,
  requestingId,
  currentPreviewTrackId,
  isPreviewPlaying,
  issuesHook,
  onClose,
  onPlayTrack,
  onRequestTrack,
  onRequestAlbum,
  onOpenArtist,
  onOpenAlbum,
}) => {
  const title = detail?.title || track.title;
  const artist = detail?.artist || track.artist;
  const artistId = detail?.artist_discovery_id ?? track.artist_discovery_id;
  const albumTitle = detail?.album ?? track.album ?? undefined;
  const albumId = detail?.album_discovery_id ?? track.album_discovery_id;
  const cover = detail?.cover_url ?? track.cover_url ?? undefined;
  const previewUrl = detail?.preview_url ?? track.preview_url ?? undefined;
  const releaseDate = detail?.release_date ?? track.release_date ?? undefined;

  const status = effectiveStatus(detail?.status ?? track.status, track.id, requestedIds);
  const trackBusy = requestingId === track.id;
  const albumRequested = albumId ? requestedIds.has(albumId) : false;
  const albumBusy = albumId ? requestingId === albumId : false;
  // Reports are about media we have, so only for tracks that are in the library.
  const canReport = status === 'in_library' || status === 'available' || status === 'partial';
  const isPlaying = currentPreviewTrackId === track.id && isPreviewPlaying;

  const trackItem: DiscoveryItem = {
    id: track.id,
    title,
    artist,
    album: albumTitle,
    cover_url: cover,
    preview_url: previewUrl,
    release_date: releaseDate,
    type: 'track',
    artist_discovery_id: artistId,
    album_discovery_id: albumId,
  };
  const albumItem: DiscoveryItem | null =
    albumId && albumTitle
      ? { id: albumId, title: albumTitle, artist, album: albumTitle, cover_url: cover, release_date: releaseDate, type: 'album', artist_discovery_id: artistId }
      : null;

  const position =
    detail?.track_position != null
      ? `Track ${detail.track_position}${detail.disk_number ? ` / Disc ${detail.disk_number}` : ''}`
      : null;
  const credits = detail?.contributors ?? [];

  return (
    <ObsidianModal
      isOpen
      onClose={onClose}
      title="Track"
      subtitle={title}
      footer={
        <div className="flex w-full flex-col items-stretch justify-end gap-2 sm:flex-row sm:items-center">
          {albumItem &&
            (albumRequested ? (
              <TapeDeckButton size="md" disabled className="border-[#e5a00d]/30 text-[#e5a00d]" icon={<Check className="h-4 w-4" />}>
                Album Requested
              </TapeDeckButton>
            ) : albumBusy ? (
              <TapeDeckButton size="md" disabled className="border-blue-500/30 text-blue-400" icon={<Loader2 className="h-4 w-4 animate-spin" />}>
                Processing
              </TapeDeckButton>
            ) : (
              <TapeDeckButton size="md" onClick={() => onRequestAlbum(albumItem)} icon={<Plus className="h-4 w-4" />}>
                Request Full Album
              </TapeDeckButton>
            ))}
          <ProfileRequestButton
            size="md"
            status={status}
            busy={trackBusy}
            label="Request Track"
            onRequest={() => onRequestTrack(trackItem)}
          />
        </div>
      }
    >
      <div className="space-y-4">
        <div className="flex items-start gap-3 sm:gap-4">
          <img
            src={cover || '/placeholder.svg'}
            alt=""
            width={112}
            height={112}
            decoding="async"
            className="h-28 w-28 shrink-0 rounded-[3px] border border-[#222222] object-cover"
          />
          <div className="min-w-0 flex-1 space-y-1">
            <h4 className="break-words text-sm font-bold text-white sm:text-base">{title}</h4>
            <p className="flex min-w-0 text-sm text-neutral-300">
              <ArtistNameLink name={artist} discoveryId={artistId} onOpen={onOpenArtist} />
            </p>
            {albumTitle && (
              <p className="flex min-w-0 text-xs text-neutral-400">
                {albumId ? (
                  <button
                    type="button"
                    onClick={() => onOpenAlbum(albumId)}
                    className="max-w-full truncate text-left hover:text-[#e5a00d] hover:underline focus:text-[#e5a00d] focus:outline-none"
                    title={`Open album ${albumTitle}`}
                  >
                    {albumTitle}
                  </button>
                ) : (
                  <span className="truncate">{albumTitle}</span>
                )}
              </p>
            )}
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <ProfileStatusBadge status={status} />
              {detail?.explicit && (
                <span className="rounded-[2px] border border-neutral-600 px-1.5 py-0.5 font-mono text-[10px] font-bold text-neutral-300" title="Explicit">
                  E
                </span>
              )}
              {canReport && (
                <IssueReportButton
                  mediaTitle={title}
                  artist={artist}
                  types={MEDIA_ISSUE_TYPES}
                  reference={{ discoveryId: track.id, itemType: 'track' }}
                  issuesHook={issuesHook}
                />
              )}
              {previewUrl && (
                <TapeDeckButton
                  size="sm"
                  aria-label={isPlaying ? `Pause preview of ${title}` : `Play preview of ${title}`}
                  onClick={() => onPlayTrack({ id: track.id, title, artist, cover_url: cover, preview_url: previewUrl })}
                  icon={isPlaying ? <Pause className="h-3 w-3 text-[#e5a00d]" /> : <Play className="h-3 w-3 fill-current" />}
                >
                  Preview
                </TapeDeckButton>
              )}
            </div>
          </div>
        </div>

        {isLoading && (
          <div className="flex justify-center py-4" role="status" aria-label="Loading track details">
            <Loader2 className="h-5 w-5 animate-spin text-[#e5a00d]" />
          </div>
        )}
        {error && !isLoading && (
          <p className="rounded-[4px] border border-red-800/50 bg-red-950/40 p-3 font-mono text-xs text-red-300">{error}</p>
        )}

        {detail && (
          <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {detail.duration > 0 && <MetaCell label="Duration">{formatTrackDuration(detail.duration)}</MetaCell>}
            {position && <MetaCell label="Position">{position}</MetaCell>}
            {releaseDate && <MetaCell label="Released">{releaseDate.slice(0, 10)}</MetaCell>}
            {detail.bpm ? <MetaCell label="BPM">{detail.bpm}</MetaCell> : null}
            {detail.isrc && <MetaCell label="ISRC">{detail.isrc}</MetaCell>}
            {detail.label && <MetaCell label="Label">{detail.label}</MetaCell>}
            {detail.genres && detail.genres.length > 0 && <MetaCell label="Genres">{detail.genres.join(', ')}</MetaCell>}
            {credits.length > 0 && (
              <MetaCell label="Contributors">
                {credits.map((c) => `${c.name}${c.role && c.role !== 'Main' ? ` (${c.role})` : ''}`).join(', ')}
              </MetaCell>
            )}
          </dl>
        )}
      </div>
    </ObsidianModal>
  );
};
