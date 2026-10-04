import React, { useCallback, useMemo, useState } from 'react';
import { AlertTriangle, RotateCcw, Trash2, X } from 'lucide-react';
import type { ActivityQueueRecord, ListSortDir } from '@/types/activity';
import { getActivityQueue, removeActivityQueueItem, retryActivityQueueItem } from '@/services/activityService';
import { errorMessage } from '@/services/apiClient';
import { pagedFetcher, useInfiniteList } from '@/hooks/useInfiniteList';
import { usePolling } from '@/hooks/usePolling';
import { TapeDeckButton } from '@/components/ui';
import {
  FlatList,
  ListPanel,
  ProgressMeter,
  formatBytes,
  formatDateTime,
  formatEta,
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
    </span>
  );
};

/** Live download queue; refreshes in place every 5s while the tab is visible. */
export const ActivityQueuePanel: React.FC<ActivityPanelProps> = ({ onToast }) => {
  const [sortKey, setSortKey] = useState<string>('added_at');
  const [sortDir, setSortDir] = useState<ListSortDir>('desc');
  const [removingId, setRemovingId] = useState<string | number | null>(null);
  const [removeFromClient, setRemoveFromClient] = useState<boolean>(true);
  const [blocklist, setBlocklist] = useState<boolean>(false);
  const [busyId, setBusyId] = useState<string | number | null>(null);

  const list = useInfiniteList<ActivityQueueRecord>(fetchQueue, { sortKey, sortDir, getKey });
  const { refresh, removeItems } = list;
  usePolling(() => void refresh(), 5000);

  const onSortChange = useCallback((key: string, dir: ListSortDir) => {
    setSortKey(key);
    setSortDir(dir);
  }, []);

  const doRemove = useCallback(
    async (id: string | number) => {
      setBusyId(id);
      try {
        const res = await removeActivityQueueItem(id, { removeFromClient, blocklist });
        if (!res.success) {
          onToast(res.message || 'Failed to remove queue item', 'error');
          return;
        }
        removeItems(new Set([id]));
        setRemovingId(null);
        onToast(res.message || (blocklist ? 'Removed and blocklisted' : 'Removed from queue'));
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to remove queue item'), 'error');
      } finally {
        setBusyId(null);
      }
    },
    [removeFromClient, blocklist, removeItems, onToast]
  );

  const doRetry = useCallback(
    async (id: string | number) => {
      setBusyId(id);
      try {
        const res = await retryActivityQueueItem(id);
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
      { key: 'artist', label: 'Artist', sortable: true, width: 'minmax(0,1.1fr)', render: (r) => orDash(r.artist) },
      { key: 'album', label: 'Album', width: 'minmax(0,1.1fr)', render: (r) => orDash(r.album) },
      { key: 'title', label: 'Title', sortable: true, width: 'minmax(0,1.2fr)', render: (r) => orDash(r.title) },
      { key: 'quality', label: 'Quality', width: '80px', render: (r) => orDash(r.quality) },
      { key: 'protocol', label: 'Protocol', width: '70px', xlOnly: true, hideOnMobile: true, render: (r) => orDash(r.protocol) },
      { key: 'indexer', label: 'Indexer', width: '100px', xlOnly: true, hideOnMobile: true, render: (r) => orDash(r.indexer) },
      { key: 'client', label: 'Client', width: '100px', xlOnly: true, hideOnMobile: true, render: (r) => orDash(r.client) },
      {
        key: 'progress',
        label: 'Progress',
        sortable: true,
        width: '120px',
        render: (r) => <ProgressMeter value={r.progress} tone={r.stalled ? 'warning' : 'default'} />,
      },
      {
        key: 'size_bytes',
        label: 'Size',
        sortable: true,
        width: '100px',
        render: (r) => (
          <span title="size left / total">
            {formatBytes(r.sizeleft_bytes)} / {formatBytes(r.size_bytes)}
          </span>
        ),
      },
      { key: 'eta', label: 'ETA', width: '64px', render: (r) => formatEta(r.eta_seconds) },
      { key: 'status', label: 'Status', sortable: true, width: '110px', render: (r) => <StatusCell record={r} /> },
      { key: 'added_at', label: 'Added', sortable: true, width: '140px', render: (r) => formatDateTime(r.added_at) },
    ],
    []
  );

  const rowActions = useCallback(
    (r: ActivityQueueRecord): React.ReactNode => {
      const busy = busyId === r.id;
      if (removingId === r.id) {
        return (
          <div className="flex flex-col gap-1.5 w-full lg:items-end">
            <label className="flex items-center gap-1.5 text-[10px] font-mono text-neutral-300 min-h-[32px]">
              <input
                type="checkbox"
                checked={removeFromClient}
                onChange={(e) => setRemoveFromClient(e.target.checked)}
                className="h-3.5 w-3.5 accent-[#e5a00d]"
              />
              Remove from client
            </label>
            <label className="flex items-center gap-1.5 text-[10px] font-mono text-neutral-300 min-h-[32px]">
              <input
                type="checkbox"
                checked={blocklist}
                onChange={(e) => setBlocklist(e.target.checked)}
                className="h-3.5 w-3.5 accent-[#e5a00d]"
              />
              Blocklist release
            </label>
            <div className="flex gap-1.5">
              <TapeDeckButton size="sm" variant="danger" disabled={busy} onClick={() => void doRemove(r.id)}>
                Remove
              </TapeDeckButton>
              <TapeDeckButton size="sm" disabled={busy} onClick={() => setRemovingId(null)} icon={<X className="h-3 w-3" />} aria-label="Cancel remove" />
            </div>
          </div>
        );
      }
      return (
        <>
          <TapeDeckButton
            size="sm"
            disabled={busy}
            onClick={() => void doRetry(r.id)}
            icon={<RotateCcw className="h-3.5 w-3.5" />}
            aria-label="Retry"
            title="Retry (search again)"
          />
          <TapeDeckButton
            size="sm"
            variant="danger"
            disabled={busy}
            onClick={() => setRemovingId(r.id)}
            icon={<Trash2 className="h-3.5 w-3.5" />}
            aria-label="Remove from queue"
            title="Remove"
          />
        </>
      );
    },
    [busyId, removingId, removeFromClient, blocklist, doRemove, doRetry]
  );

  return (
    <ListPanel title="Queue" description="Active and pending downloads. Refreshes every 5 seconds." mode={list.mode} total={list.total}>
      <FlatList
        ariaLabel="Download queue"
        columns={columns}
        items={list.items}
        total={list.total}
        loading={list.loading}
        error={list.error}
        hasMore={list.hasMore}
        onLoadMore={list.loadMore}
        onReload={list.reload}
        getKey={getKey}
        sortKey={sortKey}
        sortDir={sortDir}
        onSortChange={onSortChange}
        rowActions={rowActions}
        actionsWidth="112px"
        rowTone={rowTone}
        emptyMessage="The queue is empty."
        emptyHint="Approved requests appear here while they download."
      />
    </ListPanel>
  );
};
