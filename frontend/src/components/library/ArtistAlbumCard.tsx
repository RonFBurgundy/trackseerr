import React, { useCallback, useId, useState } from 'react';
import { BookmarkPlus, ChevronDown, ChevronUp, Disc, Loader2, Search, Sliders } from 'lucide-react';
import type { AlbumItem } from '@/types/models';
import { useAlbumTracks } from '@/hooks/useAlbumTracks';
import { useLidarrSearch } from '@/hooks/useLidarrSearch';
import { errorMessage } from '@/services/apiClient';
import {
  MachinedCard,
  OverflowMenu,
  SelectionCheckbox,
  TactileSwitch,
  TapeDeckButton,
  type OverflowMenuItem,
} from '@/components/ui';
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
  /** Present while the bulk editor is active. */
  selected?: { checked: boolean; onToggle: () => void };
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
  selected,
}) => {
  const [expanded, setExpanded] = useState<boolean>(false);
  const tracksId = useId();
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

  const menuItems: OverflowMenuItem[] = [];
  if (isAdmin) {
    menuItems.push({
      key: 'monitor',
      label: 'Monitored',
      icon: <Sliders className="h-3.5 w-3.5 text-[#e5a00d]" />,
      checked: album.monitored,
      onSelect: () => onToggleAlbumMonitored(album.id, album.monitored),
    });
  }
  if (isAdmin && lidarrMode) {
    menuItems.push({
      key: 'search',
      label: 'Search',
      icon: <Search className="h-3.5 w-3.5" />,
      disabled: searching,
      onSelect: () => void lidarrSearch.searchAlbum(album.id),
    });
  }
  if (canCollect) {
    menuItems.push({
      key: 'collect',
      label: 'Add to Collection',
      icon: <BookmarkPlus className="h-3.5 w-3.5 text-[#e5a00d]" />,
      onSelect: () => onCollect(album),
    });
  }

  return (
    <MachinedCard className={`relative p-2 sm:p-4 space-y-2 sm:space-y-4 ${selected?.checked ? 'ring-1 ring-[#e5a00d]' : ''}`}>
      <div className="flex flex-row items-center justify-between gap-2 sm:gap-4">
        <div className="flex items-center gap-2 sm:gap-3 min-w-0 flex-1">
          {selected && (
            <SelectionCheckbox
              inline
              checked={selected.checked}
              label={`Select ${album.title}`}
              onChange={selected.onToggle}
            />
          )}
          <div className="flex items-center gap-2 sm:gap-3 cursor-pointer min-w-0 flex-1" onClick={toggleExpanded}>
          <div
            className={`h-10 w-10 sm:h-12 sm:w-12 rounded-[3px] bg-[#1a1a1a] border border-[#262626] overflow-hidden flex-shrink-0 flex items-center justify-center ${
              album.in_profile === false ? 'opacity-60' : ''
            }`}
          >
            {album.cover_url ? (
              <img src={album.cover_url} alt={album.title} className="w-full h-full object-cover" loading="lazy" />
            ) : (
              <Disc className="h-6 w-6 text-neutral-600" />
            )}
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h4 className="font-bold text-[13px] sm:text-sm leading-tight text-white truncate" title={album.title}>
                {album.title}
              </h4>
              {expanded ? (
                <ChevronUp className="hidden sm:block h-4 w-4 text-neutral-400 flex-shrink-0" />
              ) : (
                <ChevronDown className="hidden sm:block h-4 w-4 text-neutral-400 flex-shrink-0" />
              )}
            </div>
            <p className="text-[11px] sm:text-xs text-neutral-400 font-mono mt-0.5">
              {album.release_date ? album.release_date.substring(0, 4) : (album.year ?? 'Unknown Year')} &bull;{' '}
              {album.track_count ?? tracks.length} {(album.track_count ?? tracks.length) === 1 ? 'track' : 'tracks'}
              {(album.track_file_count ?? 0) > 0 && <> &bull; {album.track_file_count} in library</>}
              {album.in_profile === false && (
                <span
                  className="ml-2 inline-block align-middle px-1.5 py-px rounded-[2px] border border-[#2a2a2a] bg-[#161616] text-[10px] uppercase text-neutral-500"
                  title="Outside release profile — not auto-monitored"
                  aria-label={`${album.album_type ?? 'album'}: outside release profile, not auto-monitored`}
                >
                  {album.album_type ?? 'album'}
                </span>
              )}
            </p>
          </div>
          </div>
        </div>

        <div className="flex sm:hidden items-center flex-shrink-0 -my-1.5">
          <button
            type="button"
            aria-label={expanded ? `Hide tracks for ${album.title}` : `Show tracks for ${album.title}`}
            aria-expanded={expanded}
            aria-controls={tracksId}
            onClick={toggleExpanded}
            className="inline-flex h-9 w-9 items-center justify-center rounded-[3px] text-neutral-400 hover:text-white focus:outline-none focus-visible:ring-1 focus-visible:ring-[#e5a00d]"
          >
            {loading ? (
              <Loader2 className="h-4 w-4 animate-spin text-[#e5a00d]" />
            ) : (
              <ChevronDown className={`h-4 w-4 transition-transform duration-150 ${expanded ? 'rotate-180' : ''}`} />
            )}
          </button>
          <OverflowMenu items={menuItems} label={`Actions for ${album.title}`} />
        </div>

        <div className="hidden sm:flex items-center gap-3 justify-end">
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
        <div id={tracksId} className="pt-2 border-t border-[#1f1f1f]">
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
