import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { FolderInput, Search } from 'lucide-react';
import type { IndexFetcher, ListSortDir, WantedCutoffRecord, WantedListName, WantedRecord } from '@/types/activity';
import { getWantedCutoff, getWantedIndex, getWantedMissing, searchWanted } from '@/services/activityService';
import { errorMessage } from '@/services/apiClient';
import { pagedFetcher, useVirtualPagedList } from '@/hooks/useVirtualPagedList';
import { useGroupIndex } from '@/hooks/useGroupIndex';
import { useListSelection } from '@/hooks/useListSelection';
import { ConfirmDangerButton, TapeDeckButton } from '@/components/ui';
import { ManualImportModal } from '@/components/manualImport';
import type { ManualImportScope } from '@/types/manualImport';
import {
  FlatList,
  ListPanel,
  ScrubberRail,
  formatCalendarDate,
  formatDateTime,
  orDash,
  type FlatListColumn,
} from '@/components/lists';

export interface WantedPanelProps {
  list: WantedListName;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Missing rows have no quality fields; cutoff rows carry both. */
type WantedRow = WantedRecord & Partial<Pick<WantedCutoffRecord, 'current_quality' | 'cutoff_quality'>>;

const fetchMissing = pagedFetcher<WantedRow>(getWantedMissing);
const fetchCutoff = pagedFetcher<WantedRow>(getWantedCutoff);
/** Largest native "search all" batch the backend queues in one call. */
const MAX_NATIVE_SEARCH_ALL = 1000;
const getKey = (r: WantedRow): string | number => r.id;

/** Missing / Cutoff Unmet list. Cutoff rows carry current and cutoff quality (absent on missing rows). */
export const WantedPanel: React.FC<WantedPanelProps> = ({ list: listName, onToast }) => {
  const isCutoff = listName === 'cutoff';
  const [sortKey, setSortKey] = useState<string>('artist');
  const [sortDir, setSortDir] = useState<ListSortDir>('asc');
  const [busy, setBusy] = useState<boolean>(false);
  const [importScope, setImportScope] = useState<ManualImportScope | null>(null);
  const { selected, setSelected, clear } = useListSelection();

  const list = useVirtualPagedList<WantedRow>(isCutoff ? fetchCutoff : fetchMissing, {
    sortKey,
    sortDir,
    getKey,
  });

  const fetchIndex = useCallback<IndexFetcher>((q, signal) => getWantedIndex(listName, q, signal), [listName]);
  // Native mode only; Lidarr answers `groups: []`, which hides the rail.
  const index = useGroupIndex(fetchIndex, { sortKey, sortDir, total: list.total, enabled: list.mode !== 'lidarr' });

  const onSortChange = useCallback((key: string, dir: ListSortDir) => {
    setSortKey(key);
    setSortDir(dir);
  }, []);

  const runSearch = useCallback(
    async (body: Parameters<typeof searchWanted>[0], clearSelection: boolean) => {
      setBusy(true);
      try {
        const res = await searchWanted(body);
        if (res.queued === 0) {
          // Nothing was queued (a batch is already running, or everything was searched moments ago).
          onToast(res.message || 'No searches were queued', 'error');
          return;
        }
        const queued = `Queued ${res.queued} ${res.queued === 1 ? 'search' : 'searches'}`;
        onToast(res.message ? `${queued}. ${res.message}` : queued);
        if (clearSelection) clear();
      } catch (err: unknown) {
        // 409 and other server refusals carry a readable `detail`; shown verbatim.
        onToast(errorMessage(err, 'Search failed'), 'error');
      } finally {
        setBusy(false);
      }
    },
    [onToast, clear]
  );

  const lidarrMode = list.mode === 'lidarr';

  // If the manager flips to Lidarr while the unsortable column is the active sort, fall back to the default.
  useEffect(() => {
    if (lidarrMode && sortKey === 'last_searched_at') {
      setSortKey('artist');
      setSortDir('asc');
    }
  }, [lidarrMode, sortKey]);

  const columns = useMemo<FlatListColumn<WantedRow>[]>(() => {
    const cols: FlatListColumn<WantedRow>[] = [
      { key: 'artist', label: 'Artist', sortable: true, width: 'minmax(0,1.1fr)', mobile: 'title', render: (r) => orDash(r.artist) },
      { key: 'album', label: 'Album', sortable: true, width: 'minmax(0,1.1fr)', mobile: 'sub', render: (r) => orDash(r.album) },
      { key: 'title', label: 'Title', sortable: true, width: 'minmax(0,1.2fr)', mobile: 'sub', render: (r) => (r.item_type === 'album' ? '-' : orDash(r.title)) },
      { key: 'release_date', label: 'Release date', sortable: true, width: '120px', mobile: 'meta', render: (r) => formatCalendarDate(r.release_date) },
      // Lidarr has no sort key for the last search time, so the column stays plain in lidarr mode.
      { key: 'last_searched_at', label: 'Last searched', sortable: !lidarrMode, width: '150px', mobile: 'meta', render: (r) => formatDateTime(r.last_searched_at) },
    ];
    if (isCutoff) {
      cols.push(
        { key: 'current_quality', label: 'Current', width: '100px', mobile: 'meta', render: (r) => orDash(r.current_quality) },
        { key: 'cutoff_quality', label: 'Cutoff', width: '100px', mobile: 'meta', render: (r) => orDash(r.cutoff_quality) }
      );
    }
    return cols;
  }, [isCutoff, lidarrMode]);

  // The native side queues at most 1000 per call; Lidarr runs its own all-items command.
  const searchAllCapped = list.mode === 'native' && list.total > MAX_NATIVE_SEARCH_ALL;
  const searchAllLabel = searchAllCapped ? `Search all (up to ${MAX_NATIVE_SEARCH_ALL})` : `Search all ${list.total}`;

  const rowActions = useCallback(
    (r: WantedRow): React.ReactNode =>
      r.source === 'native' && r.album_id ? (
        <TapeDeckButton
          size="sm"
          onClick={() => setImportScope({ kind: 'album', albumId: r.album_id ?? '', title: r.album || 'Album' })}
          icon={<FolderInput className="h-3.5 w-3.5" />}
        >
          Import files&hellip;
        </TapeDeckButton>
      ) : null,
    []
  );

  const toolbar = (
    <>
      <TapeDeckButton
        size="sm"
        variant="amber"
        disabled={busy || selected.size === 0}
        onClick={() => void runSearch({ ids: Array.from(selected) }, true)}
        icon={<Search className="h-3.5 w-3.5" />}
      >
        Search selected{selected.size > 0 ? ` (${selected.size})` : ''}
      </TapeDeckButton>
      <ConfirmDangerButton
        icon={<Search className="h-3.5 w-3.5" />}
        idleLabel="Search all"
        ariaLabel={`${searchAllLabel}: ${isCutoff ? 'cutoff unmet' : 'missing'} items`}
        confirmLabel={searchAllLabel}
        disabled={busy || list.total === 0}
        onConfirm={() => void runSearch({ all: true, list: listName }, false)}
      />
    </>
  );

  return (
    <ListPanel
      stackToolbar
      title={isCutoff ? 'Cutoff Unmet' : 'Missing'}
      description={
        isCutoff
          ? 'Files below their quality profile cutoff.'
          : 'Monitored items that are not in the library.'
      }
      mode={list.mode}
      total={list.total}
      toolbar={toolbar}
    >
      <FlatList
        ariaLabel={isCutoff ? 'Cutoff unmet' : 'Missing'}
        columns={columns}
        list={list}
        getKey={getKey}
        sortKey={sortKey}
        sortDir={sortDir}
        onSortChange={onSortChange}
        selectedKeys={selected}
        onSelectedKeysChange={setSelected}
        rowActions={rowActions}
        actionsWidth="150px"
        mobileLayout="compact"
        emptyMessage={isCutoff ? 'Nothing is below its cutoff.' : 'Nothing is missing.'}
        rail={<ScrubberRail groups={index.groups} ariaLabel="Jump to group" />}
      />
      <ManualImportModal
        scope={importScope}
        onClose={() => setImportScope(null)}
        onImported={() => {
          onToast('Files imported');
          void list.refresh();
        }}
      />
    </ListPanel>
  );
};
