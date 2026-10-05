import React, { useCallback, useEffect, useMemo } from 'react';
import type { AlbumItem } from '@/types/models';
import { getAlbumsIndex, getAlbumsPaged } from '@/services/libraryService';
import { pagedFetcher } from '@/hooks/useVirtualPagedList';
import { useLibraryCatalog, type LibrarySortOption } from '@/hooks/useLibraryCatalog';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import { useSelectAllMatching } from '@/hooks/useSelectAllMatching';
import { useBulkSelection } from '@/hooks/useBulkSelection';
import { useAlbumBulkEdit } from '@/hooks/useAlbumBulkEdit';
import { ScrubberRail, VirtualGrid } from '@/components/lists';
import { LibrarySortControl } from './LibrarySortControl';
import { LibrarySelectKey, LibraryToolbarPortal } from './LibraryToolbarPortal';
import { AlbumTile } from './AlbumTile';
import { AlbumBulkBar } from './AlbumBulkBar';

const SORT_OPTIONS: ReadonlyArray<LibrarySortOption> = [
  { key: 'title', label: 'Title', defaultDir: 'asc' },
  { key: 'artist', label: 'Artist', defaultDir: 'asc' },
  { key: 'release_date', label: 'Released', defaultDir: 'desc' },
  { key: 'added_at', label: 'Added', defaultDir: 'desc' },
];

/** Exact height under the square art: p-1.5 padding, 16px title, 2px gap, 14px detail line, 2px borders. */
const CAPTION_HEIGHT = 46;

const fetchAlbums = pagedFetcher<AlbumItem>(getAlbumsPaged);
const getKey = (a: AlbumItem): string | number => a.id;

export interface AlbumsPanelProps {
  query: string;
  monitoredOnly: boolean;
  isAdmin: boolean;
  reloadToken: number;
  onOpenAlbum: (album: AlbumItem) => void;
  /** Toolbar slot in the page header row; select and sort render there. */
  toolbarSlot: HTMLElement | null;
  /** Stats footer, rendered after the last row inside the scroll area. */
  footer: React.ReactNode;
  onModeChange: (mode: string | null) => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Albums as a virtualized cover grid with a scrubber; search and sort are server-side. */
export const AlbumsPanel: React.FC<AlbumsPanelProps> = ({
  query,
  monitoredOnly,
  isAdmin,
  reloadToken,
  onOpenAlbum,
  toolbarSlot,
  footer,
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
  const phone = useMediaQuery('(max-width: 639px)');
  const { reload, mode } = list;

  const canBulkEdit = isAdmin && mode !== 'lidarr';
  const selection = useBulkSelection();
  const { toggle: toggleSelected, isSelected, active: selecting, exit: exitSelection, selectKeys } = selection;
  const bulk = useAlbumBulkEdit(onToast);
  const filters = useMemo<Record<string, string>>(
    () => ({ q: query.trim(), monitored_only: monitoredOnly ? 'true' : '' }),
    [query, monitoredOnly]
  );
  const selectAll = useSelectAllMatching<AlbumItem>(getAlbumsPaged, getKey, filters, 'title', onToast);
  const { collect: collectAllIds } = selectAll;

  const handleSelectAll = useCallback(async (): Promise<void> => {
    const keys = await collectAllIds();
    if (keys) selectKeys(keys);
  }, [collectAllIds, selectKeys]);

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

  const renderItem = useCallback(
    (album: AlbumItem): React.ReactNode => (
      <AlbumTile
        album={album}
        monitored={album.monitored}
        onOpen={onOpenAlbum}
        selection={
          selecting ? { checked: isSelected(album.id), locked: false, onToggle: () => toggleSelected(album.id) } : undefined
        }
      />
    ),
    [onOpenAlbum, selecting, isSelected, toggleSelected]
  );

  return (
    <section className="flex min-h-0 flex-col gap-2" aria-label="Albums">
      <LibraryToolbarPortal slot={toolbarSlot}>
        {canBulkEdit && <LibrarySelectKey active={selecting} onToggle={selecting ? exitSelection : selection.enter} />}
        <LibrarySortControl options={SORT_OPTIONS} sortKey={sortKey} sortDir={sortDir} onChange={changeSort} />
      </LibraryToolbarPortal>
      {canBulkEdit && selecting && (
        <AlbumBulkBar
          count={selection.selected.size}
          busy={bulk.busy}
          selectBusy={selectAll.busy}
          selectLabel={`All ${list.total.toLocaleString()} albums`}
          onSelectAll={() => void handleSelectAll()}
          onClear={selection.clear}
          onDone={exitSelection}
          onApply={(m) => void handleBulkApply(m)}
        />
      )}
      <VirtualGrid<AlbumItem>
        list={list}
        getKey={getKey}
        renderItem={renderItem}
        fixedColumns={phone ? 3 : undefined}
        minTileWidth={128}
        gap={phone ? 8 : 12}
        captionHeight={CAPTION_HEIGHT}
        emptyMessage={query ? 'No albums match your search.' : 'No albums found in library.'}
        ariaLabel="Albums"
        rail={<ScrubberRail groups={index.groups} ariaLabel="Jump to group" />}
        footer={footer}
      />
    </section>
  );
};
