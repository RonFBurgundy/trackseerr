import React, { useCallback, useMemo, useState } from 'react';
import { AlertOctagon, ArrowUpCircle, Ban, CheckCircle2, Download, Trash2, XCircle } from 'lucide-react';
import type { ActivityHistoryEvent, ActivityHistoryRecord, ListSortDir } from '@/types/activity';
import { getActivityHistory, markHistoryFailed } from '@/services/activityService';
import { errorMessage } from '@/services/apiClient';
import { pagedFetcher, useInfiniteList } from '@/hooks/useInfiniteList';
import { ConfirmDangerButton, TapeDeckButton, TapeTransportBay } from '@/components/ui';
import { FlatList, ListPanel, formatDateTime, orDash, type FlatListColumn } from '@/components/lists';
import type { ActivityPanelProps } from './ActivityQueuePanel';

const fetchHistory = pagedFetcher(getActivityHistory);
const getKey = (r: ActivityHistoryRecord): string | number => r.id;

const EVENTS: Array<{ id: ActivityHistoryEvent; label: string }> = [
  { id: 'grabbed', label: 'Grabbed' },
  { id: 'imported', label: 'Imported' },
  { id: 'failed', label: 'Failed' },
  { id: 'deleted', label: 'Deleted' },
  { id: 'blocklisted', label: 'Blocklisted' },
  { id: 'upgraded', label: 'Upgraded' },
];

const EVENT_STYLE: Record<ActivityHistoryEvent, { cls: string; icon: React.ReactNode }> = {
  grabbed: { cls: 'text-blue-300 border-blue-800 bg-blue-950/50', icon: <Download className="h-3 w-3" /> },
  imported: { cls: 'text-green-300 border-green-800 bg-green-950/50', icon: <CheckCircle2 className="h-3 w-3" /> },
  failed: { cls: 'text-red-300 border-red-800 bg-red-950/50', icon: <XCircle className="h-3 w-3" /> },
  deleted: { cls: 'text-neutral-300 border-neutral-700 bg-neutral-900', icon: <Trash2 className="h-3 w-3" /> },
  blocklisted: { cls: 'text-orange-300 border-orange-800 bg-orange-950/50', icon: <Ban className="h-3 w-3" /> },
  upgraded: { cls: 'text-[#e5a00d] border-[#e5a00d]/40 bg-[#e5a00d]/10', icon: <ArrowUpCircle className="h-3 w-3" /> },
};

const EventBadge: React.FC<{ event: ActivityHistoryEvent; message: string | null }> = ({ event, message }) => {
  const style = EVENT_STYLE[event] ?? EVENT_STYLE.deleted;
  return (
    <span
      title={message ?? undefined}
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[2px] border text-[10px] font-bold uppercase ${style.cls}`}
    >
      {style.icon}
      {event}
    </span>
  );
};

export const ActivityHistoryPanel: React.FC<ActivityPanelProps> = ({ onToast }) => {
  const [sortKey, setSortKey] = useState<string>('date');
  const [sortDir, setSortDir] = useState<ListSortDir>('desc');
  const [event, setEvent] = useState<string>('');
  const [busyId, setBusyId] = useState<string | number | null>(null);

  const filters = useMemo(() => ({ event }), [event]);
  const list = useInfiniteList<ActivityHistoryRecord>(fetchHistory, { sortKey, sortDir, filters, getKey });
  const { refresh } = list;

  const onSortChange = useCallback((key: string, dir: ListSortDir) => {
    setSortKey(key);
    setSortDir(dir);
  }, []);

  const markFailed = useCallback(
    async (id: string | number) => {
      setBusyId(id);
      try {
        const res = await markHistoryFailed(id);
        if (!res.success) {
          onToast(res.message || 'Failed to mark as failed', 'error');
          return;
        }
        onToast(res.message || 'Marked as failed');
        void refresh();
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to mark as failed'), 'error');
      } finally {
        setBusyId(null);
      }
    },
    [refresh, onToast]
  );

  const columns = useMemo<FlatListColumn<ActivityHistoryRecord>[]>(
    () => [
      { key: 'date', label: 'Date', sortable: true, width: '150px', render: (r) => formatDateTime(r.date) },
      { key: 'event', label: 'Event', width: '120px', render: (r) => <EventBadge event={r.event} message={r.message} /> },
      { key: 'artist', label: 'Artist', width: 'minmax(0,1fr)', render: (r) => orDash(r.artist) },
      { key: 'album', label: 'Album', width: 'minmax(0,1fr)', render: (r) => orDash(r.album) },
      { key: 'title', label: 'Title', width: 'minmax(0,1.1fr)', render: (r) => orDash(r.title) },
      { key: 'quality', label: 'Quality', width: '80px', render: (r) => orDash(r.quality) },
      { key: 'indexer', label: 'Indexer', width: '100px', xlOnly: true, hideOnMobile: true, render: (r) => orDash(r.indexer) },
      { key: 'client', label: 'Client', width: '100px', xlOnly: true, hideOnMobile: true, render: (r) => orDash(r.client) },
    ],
    []
  );

  const rowActions = useCallback(
    (r: ActivityHistoryRecord): React.ReactNode =>
      r.event === 'grabbed' && r.can_mark_failed ? (
        <ConfirmDangerButton
          icon={<AlertOctagon className="h-3.5 w-3.5" />}
          ariaLabel="Mark as failed"
          confirmLabel="Mark failed"
          disabled={busyId === r.id}
          onConfirm={() => void markFailed(r.id)}
        />
      ) : null,
    [busyId, markFailed]
  );

  const toolbar = (
    <TapeTransportBay className="flex items-center gap-1 overflow-x-auto" aria-label="Filter by event">
      <TapeDeckButton size="sm" active={event === ''} onClick={() => setEvent('')}>
        All
      </TapeDeckButton>
      {EVENTS.map((e) => (
        <TapeDeckButton key={e.id} size="sm" active={event === e.id} onClick={() => setEvent(e.id)}>
          {e.label}
        </TapeDeckButton>
      ))}
    </TapeTransportBay>
  );

  return (
    <ListPanel title="History" description="Grabs, imports, failures and upgrades." mode={list.mode} total={list.total} toolbar={toolbar}>
      <FlatList
        ariaLabel="Download history"
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
        actionsLabel=""
        emptyMessage={event ? `No ${event} events.` : 'No history yet.'}
      />
    </ListPanel>
  );
};
