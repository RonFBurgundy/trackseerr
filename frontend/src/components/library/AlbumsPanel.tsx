import React, { useCallback, useEffect } from 'react';
import { CheckSquare } from 'lucide-react';
import type { AlbumItem } from '@/types/models';
import { getAlbumsIndex, getAlbumsPaged } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';
import { pagedFetcher } from '@/hooks/useVirtualPagedList';
import { useLibraryCatalog, type LibrarySortOption } from '@/hooks/useLibraryCatalog';
import { useMonitoredOverrides } from '@/hooks/useMonitoredOverrides';
import { useBulkSelection } from '@/hooks/useBulkSelection';
import { useAlbumBulkEdit } from '@/hooks/useAlbumBulkEdit';
import { TapeDeckButton } from '@/components/ui';
import { ListPanel, ScrubberRail, VirtualGrid } from '@/components/lists';
import { LibrarySortControl } from './LibrarySortControl';
import { AlbumTile } from './AlbumTile';
import { AlbumBulkBar } from './AlbumBulkBar';

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

  const canBulkEdit = isAdmin && mode !== 'lidarr';
  const selection = useBulkSelection();
  const { toggle: toggleSelected, isSelected, active: selecting, exit: exitSelection, selectKeys } = selection;
  const bulk = useAlbumBulkEdit(onToast);
  const { getLoadedItems } = list;

  useEffect(() => {
    exitSelection();
  }, [query, monitoredOnly, exitSelection]);

  const handleBulkApply = useCallback(
    async (monitored: boolean): Promise<void> => {
      if (await bulk.apply(Array.from(selection.selected), monitored)) {
        exitSelection();
        reload();
      }
    },
    [bulk, selection.selected, exitSelection, reload]
  );

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
        selection={
          selecting ? { checked: isSelected(album.id), locked: false, onToggle: () => toggleSelected(album.id) } : undefined
        }
      />
    ),
    [overrides, isAdmin, canCollect, onOpenAlbum, onCollectAlbum, handleToggle, selecting, isSelected, toggleSelected]
  );

  return (
    <ListPanel
      title="Albums"
      mode={mode}
      total={list.total}
      toolbar={
        <>
          {canBulkEdit && (
            <TapeDeckButton
              size="sm"
              active={selecting}
              aria-pressed={selecting}
              icon={<CheckSquare className="h-3.5 w-3.5" />}
              onClick={selecting ? exitSelection : selection.enter}
            >
              Select
            </TapeDeckButton>
          )}
          <LibrarySortControl options={SORT_OPTIONS} sortKey={sortKey} sortDir={sortDir} onChange={changeSort} />
        </>
      }
    >
      {canBulkEdit && selecting && (
        <AlbumBulkBar
          count={selection.selected.size}
          busy={bulk.busy}
          selectLabel="Select loaded"
          onSelectAll={() => selectKeys(getLoadedItems().map((a) => a.id))}
          onClear={selection.clear}
          onDone={exitSelection}
          onApply={(m) => void handleBulkApply(m)}
        />
      )}
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
