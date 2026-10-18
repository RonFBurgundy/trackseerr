import React, { useCallback, useMemo, useState } from 'react';
import { Trash2 } from 'lucide-react';
import type { ActivityBlocklistRecord, ListSortDir } from '@/types/activity';
import { getActivityBlocklist, getActivityBlocklistIndex, bulkRemoveBlocklist } from '@/services/activityService';
import { errorMessage } from '@/services/apiClient';
import { pagedFetcher, useVirtualPagedList, type ListKey } from '@/hooks/useVirtualPagedList';
import { useGroupIndex } from '@/hooks/useGroupIndex';
import { useListSelection } from '@/hooks/useListSelection';
import { ConfirmDangerButton, ConfirmDialog, type OverflowMenuItem } from '@/components/ui';
import { FlatList, ListPanel, ScrubberRail, formatDateTime, orDash, type FlatListColumn } from '@/components/lists';
import type { ActivityPanelProps } from './ActivityQueuePanel';
import { LibraryLink } from './LibraryLink';

const fetchBlocklist = pagedFetcher(getActivityBlocklist);
const getKey = (r: ActivityBlocklistRecord): string | number => r.id;

export const ActivityBlocklistPanel: React.FC<ActivityPanelProps> = ({ onToast }) => {
  const [sortKey, setSortKey] = useState<string>('date');
  const [sortDir, setSortDir] = useState<ListSortDir>('desc');
  const [busy, setBusy] = useState<boolean>(false);
  const { selected, setSelected, clear } = useListSelection();

  const [removingId, setRemovingId] = useState<string | number | null>(null);

  const list = useVirtualPagedList<ActivityBlocklistRecord>(fetchBlocklist, { sortKey, sortDir, getKey });
  const { removeItems } = list;
  const index = useGroupIndex(getActivityBlocklistIndex, { sortKey, sortDir, total: list.total });

  const onSortChange = useCallback((key: string, dir: ListSortDir) => {
    setSortKey(key);
    setSortDir(dir);
  }, []);

  /** Removes via bulk API; on partial failure the survivors stay listed and selected. */
  const removeMany = useCallback(
    async (ids: ReadonlyArray<ListKey>) => {
      setBusy(true);
      const done = new Set<ListKey>();
      let failure: string | null = null;
      try {
        const res = await bulkRemoveBlocklist(ids);
        for (const r of res.results) {
          if (r.success) {
            done.add(r.id);
          } else if (!failure) {
            failure = r.message || 'Failed to remove blocklist entry';
          }
        }
      } catch (err: unknown) {
        failure = errorMessage(err, 'Failed to remove blocklist entry');
      }
      removeItems(done);
      setSelected(new Set(ids.filter((id) => !done.has(id))));
      setRemovingId(null);
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
      { key: 'artist', label: 'Artist', sortable: true, width: 'minmax(0,1fr)', mobile: 'title', render: (r) => <LibraryLink kind="artist" id={r.artist_id} label={r.artist} /> },
      { key: 'title', label: 'Title', width: 'minmax(0,1fr)', mobile: 'sub', render: (r) => orDash(r.title) },
      { key: 'release_title', label: 'Release', width: 'minmax(0,1.4fr)', mobile: 'sub', render: (r) => orDash(r.release_title) },
      { key: 'quality', label: 'Quality', width: '80px', mobile: 'meta', render: (r) => orDash(r.quality) },
      { key: 'indexer', label: 'Indexer', width: '100px', xlOnly: true, hideOnMobile: true, render: (r) => orDash(r.indexer) },
      { key: 'reason', label: 'Reason', width: 'minmax(0,1fr)', mobile: 'meta', render: (r) => orDash(r.reason) },
    ],
    []
  );

  const rowMenu = useCallback(
    (r: ActivityBlocklistRecord): OverflowMenuItem[] => [
      {
        key: 'remove',
        label: 'Remove from blocklist…',
        icon: <Trash2 className="h-3.5 w-3.5" />,
        disabled: busy,
        onSelect: () => setRemovingId(r.id),
      },
    ],
    [busy]
  );

  const rowMenuLabel = useCallback(
    (r: ActivityBlocklistRecord) => `Actions for ${r.artist || r.title || r.release_title || 'item'}`,
    []
  );

  const rowDetails = useCallback((r: ActivityBlocklistRecord): React.ReactNode | null => {
    const details: { label: string; value: string }[] = [];
    if (r.indexer) details.push({ label: 'Indexer', value: r.indexer });
    if (r.protocol) details.push({ label: 'Protocol', value: r.protocol });
    if (r.release_title) details.push({ label: 'Release', value: r.release_title });

    if (details.length === 0) return null;
    return (
      <div className="flex flex-col gap-0.5">
        {details.map((d) => (
          <div key={d.label}>
            <span className="text-neutral-500">{d.label}:</span> {d.value}
          </div>
        ))}
      </div>
    );
  }, []);

  const toolbar =
    selected.size > 0 ? (
      <>
        <span className="self-center text-[11px] font-mono text-neutral-400">{selected.size} selected</span>
        <ConfirmDangerButton
          icon={<Trash2 className="h-3.5 w-3.5" />}
          idleLabel="Remove selected"
          collapseLabel
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
        layout="card"
        rowMenu={rowMenu}
        rowMenuLabel={rowMenuLabel}
        rowDetails={rowDetails}
        emptyMessage="The blocklist is empty."
        rail={<ScrubberRail groups={index.groups} pending={index.pending} ariaLabel={sortKey === 'artist' ? 'Jump to artist' : 'Jump to date'} />}
      />
      <ConfirmDialog
        isOpen={removingId !== null}
        title="Remove from blocklist"
        confirmLabel="Remove"
        onConfirm={() => {
          if (removingId !== null) void removeMany([removingId]);
        }}
        onCancel={() => setRemovingId(null)}
        busy={busy && removingId !== null}
      >
        <p className="text-xs font-mono text-neutral-300 py-1">
          Remove this release from the blocklist?
        </p>
      </ConfirmDialog>
    </ListPanel>
  );
};

