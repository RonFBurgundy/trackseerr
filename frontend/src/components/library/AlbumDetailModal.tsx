import React, { useCallback } from 'react';
import { BookmarkPlus, Disc, FolderInput, Loader2, Search, User } from 'lucide-react';
import type { AlbumItem } from '@/types/models';
import { useAlbumTracks } from '@/hooks/useAlbumTracks';
import { useLidarrSearch } from '@/hooks/useLidarrSearch';
import { errorMessage } from '@/services/apiClient';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import { AlbumTrackList } from './AlbumTrackList';

export interface AlbumDetailModalProps {
  album: AlbumItem | null;
  isAdmin: boolean;
  canCollect: boolean;
  lidarrMode: boolean;
  onClose: () => void;
  onCollect: (album: AlbumItem) => void;
  onGoToArtist: (artistId: number | string) => void;
  /** Opens Manual Import scoped to this album (native mode, admin). */
  onImportFiles: (album: AlbumItem) => void;
  onToggleTrackMonitored: (trackId: number | string, monitored: boolean) => Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

export const AlbumDetailModal: React.FC<AlbumDetailModalProps> = ({
  album,
  isAdmin,
  canCollect,
  lidarrMode,
  onClose,
  onCollect,
  onGoToArtist,
  onImportFiles,
  onToggleTrackMonitored,
  onToast,
}) => {
  const { tracks, loading, error, patchMonitored } = useAlbumTracks(album ? album.id : null);
  const lidarrSearch = useLidarrSearch(onToast);
  const searching = album !== null && lidarrSearch.busyKey === `album:${album.id}`;

  const handleToggle = useCallback(
    async (trackId: number | string, current: boolean): Promise<void> => {
      try {
        await onToggleTrackMonitored(trackId, !current);
        patchMonitored(trackId, !current);
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to update track monitoring'), 'error');
      }
    },
    [onToggleTrackMonitored, patchMonitored, onToast]
  );

  const goToArtist = (): void => {
    if (!album) return;
    const artistId = album.artist_id;
    onClose();
    onGoToArtist(artistId);
  };

  const genres = album?.genres && album.genres.length > 0 ? album.genres.join(', ') : null;

  return (
    <ObsidianModal
      isOpen={album !== null}
      onClose={onClose}
      title="Album Details"
      subtitle={album ? `${album.title}${album.artist_name ? ` • ${album.artist_name}` : ''}` : undefined}
      footer={
        <>
          <>
            {isAdmin && lidarrMode && (
              <TapeDeckButton
                size="sm"
                disabled={searching || album === null}
                onClick={() => album && void lidarrSearch.searchAlbum(album.id)}
                icon={searching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
                title="Ask Lidarr to search for this album"
              >
                Search
              </TapeDeckButton>
            )}
            {isAdmin && !lidarrMode && (
              <TapeDeckButton
                size="sm"
                disabled={album === null}
                onClick={() => album && onImportFiles(album)}
                icon={<FolderInput className="h-3.5 w-3.5" />}
              >
                Import files&hellip;
              </TapeDeckButton>
            )}
            {canCollect && (
              <TapeDeckButton
                size="sm"
                onClick={() => album && onCollect(album)}
                icon={<BookmarkPlus className="h-3.5 w-3.5 text-[#e5a00d]" />}
              >
                Collect
              </TapeDeckButton>
            )}
            <TapeDeckButton size="sm" onClick={goToArtist} icon={<User className="h-3.5 w-3.5" />}>
              Go to Artist
            </TapeDeckButton>
          </>
          <TapeDeckButton size="sm" onClick={onClose}>
            Close
          </TapeDeckButton>
        </>
      }
    >
      {album && (
        <div className="space-y-4">
          <div className="flex items-center gap-4 p-3 bg-[#181818] border border-[#262626] rounded-[4px]">
            <div className="h-16 w-16 sm:h-20 sm:w-20 rounded-[3px] bg-[#1a1a1a] border border-[#2a2a2a] overflow-hidden flex-shrink-0 flex items-center justify-center shadow-md">
              {album.cover_url ? (
                <img src={album.cover_url} alt={album.title} className="w-full h-full object-cover" loading="lazy" />
              ) : (
                <Disc className="h-8 w-8 text-neutral-600" />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <h3 className="text-sm sm:text-lg font-bold text-white truncate font-mono" title={album.title}>
                {album.title}
              </h3>
              <button
                type="button"
                onClick={goToArtist}
                className="text-sm text-[#e5a00d] hover:underline truncate block max-w-full min-h-[36px] sm:min-h-0 font-mono text-left"
                title="View Artist Discography"
              >
                {album.artist_name || 'Unknown Artist'}
              </button>
              <div className="flex flex-wrap items-center gap-2 pt-1 text-xs font-mono text-neutral-400">
                <span>{album.release_date ? album.release_date.slice(0, 4) : 'Unknown Year'}</span>
                {genres && (
                  <>
                    <span>&bull;</span>
                    <span className="px-1.5 py-0.5 rounded-[2px] text-[10px] bg-[#e5a00d]/10 text-[#e5a00d] border border-[#e5a00d]/30">
                      {genres}
                    </span>
                  </>
                )}
                <span>&bull;</span>
                <span>{loading ? (album.track_count ?? 0) : tracks.length} Tracks</span>
              </div>
            </div>
          </div>
          <AlbumTrackList
            tracks={tracks}
            loading={loading}
            error={error}
            isAdmin={isAdmin}
            canMonitorTracks={!lidarrMode}
            onToggleMonitored={(id, cur) => void handleToggle(id, cur)}
          />
        </div>
      )}
    </ObsidianModal>
  );
};
