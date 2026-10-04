import React, { useCallback, useState } from 'react';
import { BookmarkPlus, ChevronDown, ChevronUp, Disc, Loader2, Search } from 'lucide-react';
import type { AlbumItem } from '@/types/models';
import { useAlbumTracks } from '@/hooks/useAlbumTracks';
import { useLidarrSearch } from '@/hooks/useLidarrSearch';
import { errorMessage } from '@/services/apiClient';
import { MachinedCard, TactileSwitch, TapeDeckButton } from '@/components/ui';
import { AlbumTrackList } from './AlbumTrackList';

export interface ArtistAlbumCardProps {
  album: AlbumItem;
  isAdmin: boolean;
  canCollect: boolean;
  lidarrMode: boolean;
  onCollect: (album: AlbumItem) => void;
  onToggleAlbumMonitored: (albumId: number | string, currentMonitored: boolean) => void;
  onToggleTrackMonitored: (trackId: number | string, monitored: boolean) => Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** One release in an artist's discography; expanding it loads the tracks through the paged endpoint. */
export const ArtistAlbumCard: React.FC<ArtistAlbumCardProps> = ({
  album,
  isAdmin,
  canCollect,
  lidarrMode,
  onCollect,
  onToggleAlbumMonitored,
  onToggleTrackMonitored,
  onToast,
}) => {
  const [expanded, setExpanded] = useState<boolean>(false);
  const { tracks, loading, error, patchMonitored } = useAlbumTracks(expanded ? album.id : null);
  const lidarrSearch = useLidarrSearch(onToast);
  const searching = lidarrSearch.busyKey === `album:${album.id}`;

  const handleTrackToggle = useCallback(
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

  const toggleExpanded = (): void => setExpanded((v) => !v);

  return (
    <MachinedCard className="p-4 space-y-4">
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 sm:gap-4">
        <div className="flex items-center gap-3 cursor-pointer min-w-0 w-full sm:w-auto flex-1" onClick={toggleExpanded}>
          <div className="h-12 w-12 rounded-[3px] bg-[#1a1a1a] border border-[#262626] overflow-hidden flex-shrink-0 flex items-center justify-center">
            {album.cover_url ? (
              <img src={album.cover_url} alt={album.title} className="w-full h-full object-cover" loading="lazy" />
            ) : (
              <Disc className="h-6 w-6 text-neutral-600" />
            )}
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h4 className="font-bold text-sm text-white truncate" title={album.title}>
                {album.title}
              </h4>
              {expanded ? (
                <ChevronUp className="h-4 w-4 text-neutral-400 flex-shrink-0" />
              ) : (
                <ChevronDown className="h-4 w-4 text-neutral-400 flex-shrink-0" />
              )}
            </div>
            <p className="text-xs text-neutral-400 font-mono mt-0.5">
              {album.release_date ? album.release_date.substring(0, 4) : 'Unknown Year'} &bull;{' '}
              {album.track_count ?? tracks.length} Tracks
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2 sm:gap-3 self-stretch sm:self-center justify-between sm:justify-end flex-wrap sm:flex-nowrap">
          {isAdmin && (
            <div className="flex items-center gap-2">
              <TactileSwitch
                checked={album.monitored}
                onChange={() => onToggleAlbumMonitored(album.id, album.monitored)}
                label={album.monitored ? 'Monitored' : 'Unmonitored'}
                title={album.monitored ? 'Monitored for automated library acquisition' : 'Unmonitored'}
              />
            </div>
          )}
          <div className="flex items-center gap-2">
            {isAdmin && lidarrMode && (
              <TapeDeckButton
                size="sm"
                disabled={searching}
                onClick={() => void lidarrSearch.searchAlbum(album.id)}
                icon={searching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
                title="Ask Lidarr to search for this album"
              >
                Search
              </TapeDeckButton>
            )}
            {canCollect && (
              <TapeDeckButton
                size="sm"
                onClick={() => onCollect(album)}
                icon={<BookmarkPlus className="h-3.5 w-3.5 text-[#e5a00d]" />}
                title="Add to Collection"
              >
                Collect
              </TapeDeckButton>
            )}
            <TapeDeckButton
              size="sm"
              onClick={toggleExpanded}
              icon={
                loading ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : expanded ? (
                  <ChevronUp className="h-3.5 w-3.5" />
                ) : (
                  <ChevronDown className="h-3.5 w-3.5" />
                )
              }
            >
              {expanded ? 'Collapse' : 'Tracks'}
            </TapeDeckButton>
          </div>
        </div>
      </div>

      {expanded && (
        <div className="pt-2 border-t border-[#1f1f1f]">
          <AlbumTrackList
            tracks={tracks}
            loading={loading}
            error={error}
            isAdmin={isAdmin}
            canMonitorTracks={!lidarrMode}
            onToggleMonitored={(id, cur) => void handleTrackToggle(id, cur)}
          />
        </div>
      )}
    </MachinedCard>
  );
};
