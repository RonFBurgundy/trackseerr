import React, { useCallback, useMemo, useState } from 'react';
import { AlertOctagon, ArrowUpCircle, Ban, CheckCircle2, Download, Trash2, XCircle } from 'lucide-react';
import type { ActivityHistoryEvent, ActivityHistoryRecord, ListSortDir } from '@/types/activity';
import { getActivityHistory, getActivityHistoryIndex, markHistoryFailed } from '@/services/activityService';
import { errorMessage } from '@/services/apiClient';
import { pagedFetcher, useVirtualPagedList } from '@/hooks/useVirtualPagedList';
import { useGroupIndex } from '@/hooks/useGroupIndex';
import { ConfirmDangerButton, TapeDeckButton, TabStrip } from '@/components/ui';
import { FlatList, ListPanel, ScrubberRail, formatDateTime, orDash, type FlatListColumn } from '@/components/lists';
import type { ActivityPanelProps } from './ActivityQueuePanel';
import { LibraryLink } from './LibraryLink';

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

const EVENT_STYLE: Readonly<Record<string, { cls: string; icon: React.ReactNode }>> = {
  grabbed: { cls: 'text-blue-300 border-blue-800 bg-blue-950/50', icon: <Download className="h-3 w-3" /> },
  imported: { cls: 'text-green-300 border-green-800 bg-green-950/50', icon: <CheckCircle2 className="h-3 w-3" /> },
  failed: { cls: 'text-red-300 border-red-800 bg-red-950/50', icon: <XCircle className="h-3 w-3" /> },
  deleted: { cls: 'text-neutral-300 border-neutral-700 bg-neutral-900', icon: <Trash2 className="h-3 w-3" /> },
  blocklisted: { cls: 'text-orange-300 border-orange-800 bg-orange-950/50', icon: <Ban className="h-3 w-3" /> },
  upgraded: { cls: 'text-[#e5a00d] border-[#e5a00d]/40 bg-[#e5a00d]/10', icon: <ArrowUpCircle className="h-3 w-3" /> },
};

const EventBadge: React.FC<{ event: string | null | undefined; message: string | null | undefined }> = ({ event, message }) => {
  const style = (event ? EVENT_STYLE[event] : undefined) ?? EVENT_STYLE.deleted;
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
  const list = useVirtualPagedList<ActivityHistoryRecord>(fetchHistory, { sortKey, sortDir, filters, getKey });
  const { refresh } = list;
  const index = useGroupIndex(getActivityHistoryIndex, { sortKey, sortDir, filters, total: list.total });

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
      { key: 'date', label: 'Date', sortable: true, width: '150px', mobile: 'meta', render: (r) => formatDateTime(r.date) },
      { key: 'event', label: 'Event', width: '120px', mobile: 'end', render: (r) => <EventBadge event={r.event} message={r.message} /> },
      { key: 'artist', label: 'Artist', width: 'minmax(0,1fr)', mobile: 'title', render: (r) => <LibraryLink kind="artist" id={r.artist_id} label={r.artist} /> },
      { key: 'album', label: 'Album', width: 'minmax(0,1fr)', mobile: 'sub', render: (r) => <LibraryLink kind="album" id={r.album_id} label={r.album} /> },
      { key: 'track', label: 'Track', width: 'minmax(0,1.1fr)', mobile: 'sub', render: (r) => orDash(r.track) },
      { key: 'quality', label: 'Quality', width: '80px', mobile: 'meta', render: (r) => orDash(r.quality) },
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
    <TabStrip aria-label="Filter by event">
      <TapeDeckButton size="sm" active={event === ''} onClick={() => setEvent('')}>
        All
      </TapeDeckButton>
      {EVENTS.map((e) => (
        <TapeDeckButton key={e.id} size="sm" active={event === e.id} onClick={() => setEvent(e.id)}>
          {e.label}
        </TapeDeckButton>
      ))}
    </TabStrip>
  );

  return (
    <ListPanel title="History" description="Grabs, imports, failures and upgrades." mode={list.mode} total={list.total} toolbar={toolbar}>
      <FlatList
        ariaLabel="Download history"
        columns={columns}
        list={list}
        getKey={getKey}
        sortKey={sortKey}
        sortDir={sortDir}
        onSortChange={onSortChange}
        rowActions={rowActions}
        actionsWidth="112px"
        actionsLabel=""
        mobileLayout="compact"
        emptyMessage={event ? `No ${event} events.` : 'No history yet.'}
        rail={<ScrubberRail groups={index.groups} ariaLabel="Jump to date" />}
      />
    </ListPanel>
  );
};
