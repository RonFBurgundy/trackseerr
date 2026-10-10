import React, { useCallback, useEffect, useMemo } from 'react';
import type { TrackItem } from '@/types/models';
import { getTracksIndex, getTracksPaged } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';
import { pagedFetcher } from '@/hooks/useVirtualPagedList';
import { useLibraryCatalog, type LibrarySortOption } from '@/hooks/useLibraryCatalog';
import { useMonitoredOverrides } from '@/hooks/useMonitoredOverrides';
import { useBulkSelection } from '@/hooks/useBulkSelection';
import { useTrackBulkEdit } from '@/hooks/useTrackBulkEdit';
import { useSelectAllMatching } from '@/hooks/useSelectAllMatching';
import {
  FlatList,
  ScrubberRail,
  formatBytes,
  formatDateTime,
  orDash,
  type FlatListColumn,
} from '@/components/lists';
import { TactileSwitch } from '@/components/ui';
import { LibrarySortControl } from './LibrarySortControl';
import { LibraryToolbarPortal, type LibrarySelectAction } from './LibraryToolbarPortal';
import { AlbumBulkBar } from './AlbumBulkBar';
import { getQualityBadge } from './trackFormat';

const SORT_OPTIONS: ReadonlyArray<LibrarySortOption> = [
  { key: 'title', label: 'Title', defaultDir: 'asc' },
  { key: 'artist', label: 'Artist', defaultDir: 'asc' },
  { key: 'album', label: 'Album', defaultDir: 'asc' },
  { key: 'year', label: 'Year', defaultDir: 'desc' },
  { key: 'popularity', label: 'Popularity', defaultDir: 'desc' },
  { key: 'added_at', label: 'Added', defaultDir: 'desc' },
  { key: 'size_bytes', label: 'Size', defaultDir: 'desc' },
];

const fetchTracks = pagedFetcher<TrackItem>(getTracksPaged);
const getKey = (t: TrackItem): string | number => t.id;

export interface TracksPanelProps {
  query: string;
  monitoredOnly: boolean;
  isAdmin: boolean;
  reloadToken: number;
  onToggleMonitored: (trackId: number | string, monitored: boolean) => Promise<void>;
  /** Toolbar slot in the page header row; select and sort render there. */
  toolbarSlot: HTMLElement | null;
  /** Stats footer, rendered right after the list. */
  footer: React.ReactNode;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
  onSelectActionChange: (action: LibrarySelectAction | null) => void;
  facets?: Readonly<Record<string, string | readonly string[]>>;
}

/** Every track as a virtualized table with a scrubber (native library only). */
export const TracksPanel: React.FC<TracksPanelProps> = ({
  query,
  monitoredOnly,
  isAdmin,
  reloadToken,
  onToggleMonitored,
  toolbarSlot,
  footer,
  onToast,
  onSelectActionChange,
  facets,
}) => {
  const { list, index, sortKey, sortDir, changeSort } = useLibraryCatalog<TrackItem>({
    fetchPage: fetchTracks,
    fetchIndex: getTracksIndex,
    getKey,
    sortOptions: SORT_OPTIONS,
    query,
    monitoredOnly,
    extraFilters: facets,
  });
  const overrides = useMonitoredOverrides();
  const { refresh, reload } = list;

  const hasActiveFacets = Boolean(facets && Object.keys(facets).length > 0);
  const facetsJson = JSON.stringify(facets ?? {});

  // Track bulk edit is native-only: the route answers 409 when Lidarr manages the library.
  const canBulkEdit = isAdmin && list.mode !== 'lidarr';
  const selection = useBulkSelection();
  const { active: selecting, exit: exitSelection, selectKeys } = selection;
  const bulk = useTrackBulkEdit(onToast);
  const filters = useMemo<Record<string, string | readonly string[]>>(
    () => ({
      ...(facets ?? {}),
      q: query.trim(),
      monitored_only: monitoredOnly ? 'true' : '',
    }),
    [query, monitoredOnly, facets]
  );
  const selectAll = useSelectAllMatching<TrackItem>(getTracksPaged, getKey, filters, 'title', onToast);
  const { collect: collectAllIds } = selectAll;

  useEffect(() => {
    exitSelection();
  }, [query, monitoredOnly, facetsJson, exitSelection]);

  useEffect(() => {
    onSelectActionChange(
      canBulkEdit ? { active: selecting, toggle: selecting ? exitSelection : selection.enter } : null
    );
    return () => {
      onSelectActionChange(null);
    };
  }, [canBulkEdit, selecting, exitSelection, selection.enter, onSelectActionChange]);

  const handleSelectAll = useCallback(async (): Promise<void> => {
    const keys = await collectAllIds();
    if (keys) selectKeys(keys);
  }, [collectAllIds, selectKeys]);

  const handleBulkApply = useCallback(
    async (monitored: boolean): Promise<boolean> => {
      const ok = await bulk.apply(Array.from(selection.selected), monitored);
      if (ok) reload();
      return ok;
    },
    [bulk, selection.selected, reload]
  );

  useEffect(() => {
    if (reloadToken > 0) reload();
  }, [reloadToken, reload]);

  const handleToggle = useCallback(
    async (trackId: number | string, next: boolean): Promise<void> => {
      overrides.set(trackId, next);
      try {
        await onToggleMonitored(trackId, next);
        await refresh();
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to update monitoring'), 'error');
      } finally {
        overrides.clear(trackId);
      }
    },
    [overrides, onToggleMonitored, refresh, onToast]
  );

  const onSortChange = useCallback(
    (key: string, dir: 'asc' | 'desc') => changeSort(key, dir),
    [changeSort]
  );

  const columns = useMemo<FlatListColumn<TrackItem>[]>(
    () => [
      { key: 'title', label: 'Title', sortable: true, width: 'minmax(0,1.4fr)', mobile: 'title', render: (t) => orDash(t.title) },
      { key: 'artist', label: 'Artist', sortable: true, width: 'minmax(0,1fr)', mobile: 'sub', render: (t) => orDash(t.artist_name) },
      { key: 'album', label: 'Album', sortable: true, width: 'minmax(0,1fr)', mobile: 'sub', render: (t) => orDash(t.album_title) },
      {
        key: 'quality',
        label: 'Quality',
        width: '130px',
        mobile: 'end',
        render: (t) => {
          const badge = getQualityBadge(t);
          return (
            <span className={`px-2 py-0.5 rounded-[2px] text-[10px] font-mono font-bold ${badge.className}`}>
              {badge.label}
            </span>
          );
        },
      },
      {
        key: 'size_bytes',
        label: 'Size',
        sortable: true,
        width: '90px',
        hideOnMobile: true,
        render: (t) => (t.file?.size_bytes ? formatBytes(t.file.size_bytes) : '-'),
      },
      {
        key: 'added_at',
        label: 'Added',
        sortable: true,
        width: '150px',
        xlOnly: true,
        hideOnMobile: true,
        render: (t) => formatDateTime(t.created_at),
      },
    ],
    []
  );

  const rowActions = useCallback(
    (t: TrackItem): React.ReactNode => {
      const monitored = overrides.resolve(t.id, t.monitored ?? null);
      // Unknown (Lidarr mode may not report it): no indicator at all rather than a false "Unmonitored".
      if (monitored === null) return null;
      return (
        <div className="flex items-center justify-end gap-2">
          <span
            className={`hidden lg:inline text-[10px] font-mono uppercase tracking-wider ${
              monitored ? 'text-[#e5a00d]' : 'text-neutral-500'
            }`}
          >
            {monitored ? 'Monitored' : 'Unmonitored'}
          </span>
          {isAdmin && (
            <TactileSwitch
              checked={monitored}
              onChange={(val) => void handleToggle(t.id, val)}
              title={monitored ? 'Monitored' : 'Unmonitored'}
              ariaLabel={`Monitor ${t.title}`}
            />
          )}
        </div>
      );
    },
    [overrides, isAdmin, handleToggle]
  );

  return (
    <section className="flex min-h-0 flex-col gap-2" aria-label="Tracks">
      <LibraryToolbarPortal slot={toolbarSlot}>
        <LibrarySortControl options={SORT_OPTIONS} sortKey={sortKey} sortDir={sortDir} onChange={changeSort} />
      </LibraryToolbarPortal>
      {canBulkEdit && selecting && (
        <AlbumBulkBar
          noun="tracks"
          count={selection.selected.size}
          busy={bulk.busy}
          selectBusy={selectAll.busy}
          selectLabel={
            hasActiveFacets || query || monitoredOnly
              ? `All ${list.total.toLocaleString()} matching tracks`
              : `All ${list.total.toLocaleString()} tracks`
          }
          onSelectAll={() => void handleSelectAll()}
          onClear={selection.clear}
          onDone={exitSelection}
          onApply={handleBulkApply}
        />
      )}
      <FlatList<TrackItem>
        ariaLabel="Tracks"
        columns={columns}
        list={list}
        getKey={getKey}
        sortKey={sortKey}
        sortDir={sortDir}
        onSortChange={onSortChange}
        selectedKeys={canBulkEdit && selecting ? selection.selected : undefined}
        onSelectedKeysChange={canBulkEdit && selecting ? selectKeys : undefined}
        rowActions={rowActions}
        actionsLabel="Monitoring"
        actionsWidth="200px"
        mobileLayout="compact"
        hideMobileSortBar
        emptyMessage={
          hasActiveFacets
            ? 'Nothing in your library matches these filters.'
            : query
            ? 'No tracks match your search.'
            : 'No tracks found in library.'
        }
        rail={<ScrubberRail groups={index.groups} ariaLabel="Jump to group" />}
      />
      {footer}
    </section>
  );
};
