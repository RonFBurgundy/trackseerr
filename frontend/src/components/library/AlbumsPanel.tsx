import React, { useCallback, useEffect } from 'react';
import type { AlbumItem } from '@/types/models';
import { getAlbumsIndex, getAlbumsPaged } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';
import { pagedFetcher } from '@/hooks/useVirtualPagedList';
import { useLibraryCatalog, type LibrarySortOption } from '@/hooks/useLibraryCatalog';
import { useMonitoredOverrides } from '@/hooks/useMonitoredOverrides';
import { ListPanel, ScrubberRail, VirtualGrid } from '@/components/lists';
import { LibrarySortControl } from './LibrarySortControl';
import { AlbumTile } from './AlbumTile';

const SORT_OPTIONS: ReadonlyArray<LibrarySortOption> = [
  { key: 'title', label: 'Title', defaultDir: 'asc' },
  { key: 'artist', label: 'Artist', defaultDir: 'asc' },
  { key: 'release_date', label: 'Released', defaultDir: 'desc' },
  { key: 'added_at', label: 'Added', defaultDir: 'desc' },
];

const fetchAlbums = pagedFetcher<AlbumItem>(getAlbumsPaged);
const getKey = (a: AlbumItem): string | number => a.id;

export interface AlbumsPanelProps {
  query: string;
  monitoredOnly: boolean;
  isAdmin: boolean;
  canCollect: boolean;
  reloadToken: number;
  onOpenAlbum: (album: AlbumItem) => void;
  onCollectAlbum: (album: AlbumItem) => void;
  onToggleMonitored: (albumId: number | string, monitored: boolean) => Promise<void>;
  onModeChange: (mode: string | null) => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Albums as a virtualized cover grid with a scrubber; search and sort are server-side. */
export const AlbumsPanel: React.FC<AlbumsPanelProps> = ({
  query,
  monitoredOnly,
  isAdmin,
  canCollect,
  reloadToken,
  onOpenAlbum,
  onCollectAlbum,
  onToggleMonitored,
  onModeChange,
  onToast,
}) => {
  const { list, index, sortKey, sortDir, changeSort } = useLibraryCatalog<AlbumItem>({
    fetchPage: fetchAlbums,
    fetchIndex: getAlbumsIndex,
    getKey,
    sortOptions: SORT_OPTIONS,
    query,
    monitoredOnly,
  });
  const overrides = useMonitoredOverrides();
  const { refresh, reload, mode } = list;

  useEffect(() => onModeChange(mode), [mode, onModeChange]);
  useEffect(() => {
    if (reloadToken > 0) reload();
  }, [reloadToken, reload]);

  const handleToggle = useCallback(
    async (albumId: number | string, next: boolean): Promise<void> => {
      overrides.set(albumId, next);
      try {
        await onToggleMonitored(albumId, next);
        await refresh();
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to update monitoring'), 'error');
      } finally {
        overrides.clear(albumId);
      }
    },
    [overrides, onToggleMonitored, refresh, onToast]
  );

  const renderItem = useCallback(
    (album: AlbumItem): React.ReactNode => (
      <AlbumTile
        album={album}
        monitored={overrides.resolve(album.id, album.monitored)}
        isAdmin={isAdmin}
        canCollect={canCollect}
        onOpen={onOpenAlbum}
        onCollect={onCollectAlbum}
        onToggleMonitored={(id, val) => void handleToggle(id, val)}
      />
    ),
    [overrides, isAdmin, canCollect, onOpenAlbum, onCollectAlbum, handleToggle]
  );

  return (
    <ListPanel
      title="Albums"
      mode={mode}
      total={list.total}
      toolbar={<LibrarySortControl options={SORT_OPTIONS} sortKey={sortKey} sortDir={sortDir} onChange={changeSort} />}
    >
      <VirtualGrid<AlbumItem>
        list={list}
        getKey={getKey}
        renderItem={renderItem}
        captionHeight={93}
        emptyMessage={query ? 'No albums match your search.' : 'No albums found in library.'}
        ariaLabel="Albums"
        rail={<ScrubberRail groups={index.groups} ariaLabel="Jump to group" />}
      />
    </ListPanel>
  );
};
