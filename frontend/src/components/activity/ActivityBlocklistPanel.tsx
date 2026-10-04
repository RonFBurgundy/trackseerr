import React, { useCallback, useMemo, useState } from 'react';
import { Trash2 } from 'lucide-react';
import type { ActivityBlocklistRecord, ListSortDir } from '@/types/activity';
import { getActivityBlocklist, removeBlocklistItem } from '@/services/activityService';
import { errorMessage } from '@/services/apiClient';
import { pagedFetcher, useVirtualPagedList, type ListKey } from '@/hooks/useVirtualPagedList';
import { useListSelection } from '@/hooks/useListSelection';
import { ConfirmDangerButton } from '@/components/ui';
import { FlatList, ListPanel, formatDateTime, orDash, type FlatListColumn } from '@/components/lists';
import type { ActivityPanelProps } from './ActivityQueuePanel';

const fetchBlocklist = pagedFetcher(getActivityBlocklist);
const getKey = (r: ActivityBlocklistRecord): string | number => r.id;

export const ActivityBlocklistPanel: React.FC<ActivityPanelProps> = ({ onToast }) => {
  const [sortKey, setSortKey] = useState<string>('date');
  const [sortDir, setSortDir] = useState<ListSortDir>('desc');
  const [busy, setBusy] = useState<boolean>(false);
  const { selected, setSelected, clear } = useListSelection();

  const list = useVirtualPagedList<ActivityBlocklistRecord>(fetchBlocklist, { sortKey, sortDir, getKey });
  const { removeItems } = list;

  const onSortChange = useCallback((key: string, dir: ListSortDir) => {
    setSortKey(key);
    setSortDir(dir);
  }, []);

  /** Removes sequentially; on partial failure the survivors stay listed and selected. */
  const removeMany = useCallback(
    async (ids: ReadonlyArray<ListKey>) => {
      setBusy(true);
      const done = new Set<ListKey>();
      let failure: string | null = null;
      for (const id of ids) {
        try {
          const res = await removeBlocklistItem(id);
          if (!res.success) {
            failure = res.message || 'Failed to remove blocklist entry';
            break;
          }
          done.add(id);
        } catch (err: unknown) {
          failure = errorMessage(err, 'Failed to remove blocklist entry');
          break;
        }
      }
      removeItems(done);
      setSelected(new Set(ids.filter((id) => !done.has(id))));
      if (failure) onToast(`Removed ${done.size} of ${ids.length}: ${failure}`, 'error');
      else {
        clear();
        onToast(`Removed ${done.size} blocklist ${done.size === 1 ? 'entry' : 'entries'}`);
      }
      setBusy(false);
    },
    [removeItems, setSelected, clear, onToast]
  );

  const columns = useMemo<FlatListColumn<ActivityBlocklistRecord>[]>(
    () => [
      { key: 'date', label: 'Date', sortable: true, width: '150px', mobile: 'meta', render: (r) => formatDateTime(r.date) },
      { key: 'artist', label: 'Artist', sortable: true, width: 'minmax(0,1fr)', mobile: 'title', render: (r) => orDash(r.artist) },
      { key: 'title', label: 'Title', width: 'minmax(0,1fr)', mobile: 'sub', render: (r) => orDash(r.title) },
      { key: 'release_title', label: 'Release', width: 'minmax(0,1.4fr)', mobile: 'sub', render: (r) => orDash(r.release_title) },
      { key: 'quality', label: 'Quality', width: '80px', mobile: 'meta', render: (r) => orDash(r.quality) },
      { key: 'indexer', label: 'Indexer', width: '100px', xlOnly: true, hideOnMobile: true, render: (r) => orDash(r.indexer) },
      { key: 'reason', label: 'Reason', width: 'minmax(0,1fr)', mobile: 'meta', render: (r) => orDash(r.reason) },
    ],
    []
  );

  const rowActions = useCallback(
    (r: ActivityBlocklistRecord): React.ReactNode => (
      <ConfirmDangerButton
        icon={<Trash2 className="h-3.5 w-3.5" />}
        ariaLabel="Remove from blocklist"
        confirmLabel="Remove"
        disabled={busy}
        onConfirm={() => void removeMany([r.id])}
      />
    ),
    [busy, removeMany]
  );

  const toolbar =
    selected.size > 0 ? (
      <>
        <span className="text-[11px] font-mono text-neutral-400">{selected.size} selected</span>
        <ConfirmDangerButton
          icon={<Trash2 className="h-3.5 w-3.5" />}
          ariaLabel={`Remove ${selected.size} selected from blocklist`}
          confirmLabel={`Remove ${selected.size}`}
          disabled={busy}
          onConfirm={() => void removeMany(Array.from(selected))}
        />
      </>
    ) : null;

  return (
    <ListPanel
      title="Blocklist"
      description="Releases that will not be grabbed again."
      mode={list.mode}
      total={list.total}
      toolbar={toolbar}
    >
      <FlatList
        ariaLabel="Blocklist"
        columns={columns}
        list={list}
        getKey={getKey}
        sortKey={sortKey}
        sortDir={sortDir}
        onSortChange={onSortChange}
        selectedKeys={selected}
        onSelectedKeysChange={setSelected}
        rowActions={rowActions}
        actionsWidth="112px"
        actionsLabel=""
        mobileLayout="compact"
        emptyMessage="The blocklist is empty."
      />
    </ListPanel>
  );
};
