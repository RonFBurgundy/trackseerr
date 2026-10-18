import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Ban, FolderInput, RotateCcw, Trash2 } from 'lucide-react';
import type { ActivityQueueRecord, ActivitySeeding, ListSortDir } from '@/types/activity';
import { getActivityQueue, bulkQueueAction, retryActivityQueueItem } from '@/services/activityService';
import { errorMessage } from '@/services/apiClient';
import { pagedFetcher, useVirtualPagedList, type ListKey } from '@/hooks/useVirtualPagedList';
import { usePolling } from '@/hooks/usePolling';
import { useListSelection } from '@/hooks/useListSelection';
import { ConfirmDangerButton, ConfirmDialog, TactileSwitch, TapeDeckButton, type OverflowMenuItem } from '@/components/ui';
import { SoulseekQueueDetail } from './SoulseekQueueDetail';
import { ManualImportModal } from '@/components/manualImport';
import type { ManualImportScope } from '@/types/manualImport';
import {
  FlatList,
  ListPanel,
  ProgressMeter,
  ScrubberRail,
  formatBytes,
  formatDateTime,
  formatEta,
  formatMinutes,
  orDash,
  type FlatListColumn,
  type RowTone,
} from '@/components/lists';

export interface ActivityPanelProps {
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

const fetchQueue = pagedFetcher(getActivityQueue);
const getKey = (r: ActivityQueueRecord): string | number => r.id;
const rowTone = (r: ActivityQueueRecord): RowTone => (r.stalled ? 'warning' : null);
const NO_GROUPS = [] as const;

const SeedingLine: React.FC<{ seeding: ActivitySeeding }> = ({ seeding }) => {
  const ratio =
    seeding.ratio_target == null ? seeding.ratio.toFixed(2) : `${seeding.ratio.toFixed(2)} / ${parseFloat(seeding.ratio_target.toFixed(2))}`;
  const time =
    seeding.time_target_minutes == null
      ? formatMinutes(seeding.seeding_minutes)
      : `${formatMinutes(seeding.seeding_minutes)} / ${formatMinutes(seeding.time_target_minutes)}`;
  const mins = seeding.removes_in_minutes;
  const removes =
    mins === null || mins === undefined || !seeding.action || seeding.action === 'keep'
      ? null
      : mins <= 0
        ? 'removes on next check'
        : `removes in ~${formatMinutes(mins)}`;
  return (
    <span className="basis-full text-[10px] font-mono text-[#e5a00d] break-words" title="Seeding ratio and time versus targets">
      Seeding &middot; {ratio} &middot; {time}
      {removes && <> &middot; {removes}</>}
    </span>
  );
};

const TitleCell: React.FC<{ record: ActivityQueueRecord }> = ({ record }) => (
  <span className="flex flex-col min-w-0">
    <span className="truncate">{orDash(record.title)}</span>
    {record.peer && <SoulseekQueueDetail record={record} />}
  </span>
);

const StatusCell: React.FC<{ record: ActivityQueueRecord }> = ({ record }) => {
  const reason = record.stalled_reason || record.messages.join(' | ') || 'Stalled';
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <span className="uppercase text-[10px] font-bold text-neutral-300">{record.status}</span>
      {record.stalled && (
        <span
          title={reason}
          aria-label={`Stalled: ${reason}`}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[2px] border border-[#e5a00d]/50 bg-[#e5a00d]/10 text-[#e5a00d] text-[10px] font-bold uppercase cursor-help"
        >
          <AlertTriangle className="h-3 w-3" /> Stalled
        </span>
      )}
      {record.seeding && <SeedingLine seeding={record.seeding} />}
    </span>
  );
};

/** Live download queue; refreshes in place every 5s while the tab is visible. */
export const ActivityQueuePanel: React.FC<ActivityPanelProps> = ({ onToast }) => {
  const [sortKey, setSortKey] = useState<string>('added_at');
  const [sortDir, setSortDir] = useState<ListSortDir>('desc');
  const [removingTarget, setRemovingTarget] = useState<{
    ids: ReadonlyArray<ListKey>;
    fromSelection: boolean;
  } | null>(null);
  const [removeFromClient, setRemoveFromClient] = useState<boolean>(true);
  const [blocklist, setBlocklist] = useState<boolean>(false);
  const [busyId, setBusyId] = useState<string | number | null>(null);
  const [bulkBusy, setBulkBusy] = useState<boolean>(false);
  const [importScope, setImportScope] = useState<ManualImportScope | null>(null);
  const { selected, setSelected, clear } = useListSelection();

  const list = useVirtualPagedList<ActivityQueueRecord>(fetchQueue, { sortKey, sortDir, getKey });
  const { refresh, removeItems, getLoadedItems, version } = list;
  usePolling(() => void refresh(), 5000);

  // Drop selected keys that are no longer in loaded rows after each cache version change/refresh
  useEffect(() => {
    if (selected.size === 0) return;
    const loaded = getLoadedItems();
    const loadedKeys = new Set(loaded.map(getKey));
    const nextSelected = new Set(Array.from(selected).filter((key) => loadedKeys.has(key)));
    if (nextSelected.size !== selected.size) {
      setSelected(nextSelected);
    }
  }, [version, getLoadedItems, selected, setSelected]);

  const onSortChange = useCallback((key: string, dir: ListSortDir) => {
    setSortKey(key);
    setSortDir(dir);
  }, []);

  const doRemoveMany = useCallback(
    async (ids: ReadonlyArray<ListKey>, fromSelection: boolean) => {
      setBulkBusy(true);
      const byString = new Map(ids.map((id) => [String(id), id]));
      const done = new Set<ListKey>();
      let failure: string | null = null;
      try {
        const stringIds = ids.map((id) => String(id));
        const res = await bulkQueueAction({
          ids: stringIds,
          action: 'remove',
          remove_from_client: removeFromClient,
          blocklist,
        });
        for (const r of res.results) {
          if (r.success) {
            const originalId = byString.get(r.id);
            if (originalId !== undefined) {
              done.add(originalId);
            }
          } else if (!failure) {
            failure = r.message || 'Failed to remove queue item';
          }
        }
      } catch (err: unknown) {
        failure = errorMessage(err, 'Failed to remove queue item');
      }
      removeItems(done);
      if (fromSelection) {
        setSelected(new Set(ids.filter((id) => !done.has(id))));
      } else {
        let changed = false;
        const next = new Set(selected);
        for (const id of done) {
          if (next.delete(id)) {
            changed = true;
          }
        }
        if (changed) {
          setSelected(next);
        }
      }
      setRemovingTarget(null);
      if (failure) {
        onToast(`Removed ${done.size} of ${ids.length}: ${failure}`, 'error');
      } else {
        if (fromSelection) {
          clear();
        }
        onToast(`Removed ${done.size} from the queue`);
      }
      setBulkBusy(false);
    },
    [removeFromClient, blocklist, removeItems, setSelected, selected, clear, onToast]
  );

  const doRetryMany = useCallback(
    async (ids: ReadonlyArray<ListKey>) => {
      setBulkBusy(true);
      const byString = new Map(ids.map((id) => [String(id), id]));
      const done = new Set<ListKey>();
      let failure: string | null = null;
      const chunkSize = 20;

      for (let i = 0; i < ids.length; i += chunkSize) {
        const chunk = ids.slice(i, i + chunkSize);
        try {
          const stringIds = chunk.map((id) => String(id));
          const res = await bulkQueueAction({
            ids: stringIds,
            action: 'retry',
            remove_from_client: false,
            blocklist: false,
          });
          for (const r of res.results) {
            if (r.success) {
              const originalId = byString.get(r.id);
              if (originalId !== undefined) {
                done.add(originalId);
              }
            } else if (!failure) {
              failure = r.message || 'Failed to retry';
            }
          }
        } catch (err: unknown) {
          if (!failure) failure = errorMessage(err, 'Failed to retry');
          break;
        }
      }

      void refresh();
      setSelected(new Set(ids.filter((id) => !done.has(id))));
      if (failure) {
        onToast(`Retried ${done.size} of ${ids.length}: ${failure}`, 'error');
      } else {
        clear();
        onToast(`Retried ${done.size}`);
      }
      setBulkBusy(false);
    },
    [refresh, setSelected, clear, onToast]
  );

  const doRetry = useCallback(
    async (id: string | number, blocklistCurrent = false) => {
      setBusyId(id);
      try {
        const res = await retryActivityQueueItem(id, blocklistCurrent ? { blocklist: true } : undefined);
        if (!res.success) {
          // Nothing was grabbed: the server keeps the stuck row, so there is nothing to refresh.
          onToast(res.message || 'No release found', 'error');
          return;
        }
        onToast(res.message || 'Search queued');
        void refresh();
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to retry'), 'error');
      } finally {
        setBusyId(null);
      }
    },
    [refresh, onToast]
  );

  const columns = useMemo<FlatListColumn<ActivityQueueRecord>[]>(
    () => [
      { key: 'artist', label: 'Artist', sortable: true, width: 'minmax(120px,1fr)', truncate: true, mobile: 'sub', render: (r) => orDash(r.artist) },
      { key: 'album', label: 'Album', width: 'minmax(120px,1fr)', truncate: true, xlOnly: true, mobile: 'sub', render: (r) => orDash(r.album) },
      { key: 'title', label: 'Title', sortable: true, width: 'minmax(140px,1.4fr)', truncate: true, mobile: 'title', render: (r) => <TitleCell record={r} /> },
      { key: 'quality', label: 'Quality', width: '80px', mobile: 'meta', render: (r) => orDash(r.quality) },
      { key: 'protocol', label: 'Protocol', width: '70px', xlOnly: true, hideOnMobile: true, render: (r) => orDash(r.protocol) },
      { key: 'indexer', label: 'Indexer', width: '100px', xlOnly: true, hideOnMobile: true, render: (r) => orDash(r.indexer) },
      { key: 'client', label: 'Client', width: '100px', xlOnly: true, hideOnMobile: true, render: (r) => orDash(r.client) },
      {
        key: 'progress',
        label: 'Progress',
        sortable: true,
        width: '110px',
        mobile: 'meta',
        render: (r) => <ProgressMeter value={r.progress} tone={r.stalled ? 'warning' : 'default'} />,
      },
      {
        key: 'size_bytes',
        label: 'Size',
        sortable: true,
        width: '100px',
        xlOnly: true,
        mobile: 'hide',
        render: (r) => (
          <span title="size left / total">
            {formatBytes(r.sizeleft_bytes)} / {formatBytes(r.size_bytes)}
          </span>
        ),
      },
      { key: 'eta', label: 'ETA', width: '64px', mobile: 'meta', render: (r) => formatEta(r.eta_seconds) },
      { key: 'status', label: 'Status', sortable: true, width: '100px', mobile: 'end', render: (r) => <StatusCell record={r} /> },
      { key: 'added_at', label: 'Added', sortable: true, width: '120px', xlOnly: true, mobile: 'hide', render: (r) => formatDateTime(r.added_at) },
    ],
    []
  );

  const rowMenu = useCallback(
    (r: ActivityQueueRecord): OverflowMenuItem[] => {
      const busy = busyId === r.id;
      const items: OverflowMenuItem[] = [];

      if (r.source === 'native' && r.needs_manual_import && r.download_id) {
        items.push({
          key: 'manual-import',
          label: 'Manual import',
          icon: <FolderInput className="h-3.5 w-3.5" />,
          highlight: true,
          disabled: busy,
          onSelect: () =>
            setImportScope({
              kind: 'download',
              downloadId: r.download_id ?? '',
              title: [r.artist, r.album ?? r.title].filter(Boolean).join(' \u2013 ') || 'Download',
            }),
        });
      }

      items.push({
        key: 'retry',
        label: 'Retry (search again)',
        icon: <RotateCcw className="h-3.5 w-3.5" />,
        disabled: busy,
        onSelect: () => void doRetry(r.id),
      });

      if (r.source === 'native') {
        items.push({
          key: 'blocklist',
          label: 'Blocklist and search again',
          icon: <Ban className="h-3.5 w-3.5" />,
          disabled: busy,
          onSelect: () => void doRetry(r.id, true),
        });
      }

      items.push({
        key: 'remove',
        label: 'Remove…',
        icon: <Trash2 className="h-3.5 w-3.5" />,
        disabled: busy || bulkBusy,
        onSelect: () => setRemovingTarget({ ids: [r.id], fromSelection: false }),
      });

      return items;
    },
    [busyId, bulkBusy, doRetry]
  );

  const rowAttention = useCallback(
    (r: ActivityQueueRecord) => {
      if (r.source === 'native' && r.needs_manual_import && r.download_id) {
        return {
          label: 'Manual import',
          onSelect: () =>
            setImportScope({
              kind: 'download',
              downloadId: r.download_id ?? '',
              title: [r.artist, r.album ?? r.title].filter(Boolean).join(' \u2013 ') || 'Download',
            }),
        };
      }
      return null;
    },
    []
  );

  const rowMenuLabel = useCallback(
    (r: ActivityQueueRecord) => `Actions for ${r.title || r.artist || 'item'}`,
    []
  );

  const rowDetails = useCallback((r: ActivityQueueRecord): React.ReactNode | null => {
    const details: { label: string; value: string }[] = [];
    if (r.album) details.push({ label: 'Album', value: r.album });
    if (r.protocol) details.push({ label: 'Protocol', value: r.protocol });
    if (r.indexer) details.push({ label: 'Indexer', value: r.indexer });
    if (r.client) details.push({ label: 'Client', value: r.client });
    if (r.size_bytes != null || r.sizeleft_bytes != null) {
      details.push({
        label: 'Size',
        value: `${formatBytes(r.sizeleft_bytes)} / ${formatBytes(r.size_bytes)}`,
      });
    }
    if (r.added_at) details.push({ label: 'Added', value: formatDateTime(r.added_at) });

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
          icon={<RotateCcw className="h-3.5 w-3.5" />}
          idleLabel="Retry selected"
          collapseLabel
          ariaLabel={`Retry ${selected.size} selected items in queue`}
          confirmLabel={`Retry ${selected.size}`}
          disabled={bulkBusy}
          onConfirm={() => void doRetryMany(Array.from(selected))}
        />
        <TapeDeckButton
          size="sm"
          variant="danger"
          icon={<Trash2 className="h-3.5 w-3.5" />}
          collapseLabel
          aria-label={`Remove ${selected.size} selected from queue`}
          title={`Remove ${selected.size} selected from queue`}
          disabled={bulkBusy}
          onClick={() => setRemovingTarget({ ids: Array.from(selected), fromSelection: true })}
        >
          Remove selected
        </TapeDeckButton>
      </>
    ) : null;

  const removingCount = removingTarget ? removingTarget.ids.length : 0;
  const isSingleRemove = removingCount === 1;

  return (
    <ListPanel
      title="Queue"
      description="Active and pending downloads. Refreshes every 5 seconds."
      mode={list.mode}
      total={list.total}
      toolbar={toolbar}
    >
      <FlatList
        ariaLabel="Download queue"
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
        rowAttention={rowAttention}
        rowDetails={rowDetails}
        rowTone={rowTone}
        emptyMessage="The queue is empty."
        emptyHint="Approved requests appear here while they download."
        rail={<ScrubberRail groups={NO_GROUPS} ariaLabel="Scroll queue" />}
      />
      <ManualImportModal
        scope={importScope}
        onClose={() => setImportScope(null)}
        onImported={() => {
          onToast('Files imported');
          void refresh();
        }}
      />
      <ConfirmDialog
        isOpen={removingTarget !== null}
        title={isSingleRemove ? 'Remove from queue' : `Remove ${removingCount} from queue`}
        confirmLabel="Remove"
        onConfirm={() => {
          if (removingTarget !== null) void doRemoveMany(removingTarget.ids, removingTarget.fromSelection);
        }}
        onCancel={() => setRemovingTarget(null)}
        busy={bulkBusy && removingTarget !== null}
      >
        <div className="space-y-3 py-1">
          <p className="text-xs font-mono text-neutral-300">
            {isSingleRemove
              ? 'Remove this item from the download queue?'
              : `Remove ${removingCount} items from the download queue?`}
          </p>
          <div className="flex flex-col gap-2.5">
            <TactileSwitch
              checked={removeFromClient}
              onChange={setRemoveFromClient}
              label="Remove from client"
            />
            <TactileSwitch
              checked={blocklist}
              onChange={setBlocklist}
              label="Blocklist release"
            />
          </div>
        </div>
      </ConfirmDialog>
    </ListPanel>
  );
};
