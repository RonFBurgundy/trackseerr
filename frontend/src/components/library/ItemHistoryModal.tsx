import React from 'react';
import {
  AlertTriangle,
  Download,
  File as FileIcon,
  Flag,
  HeartPulse,
  Loader2,
  SlidersHorizontal,
  type LucideIcon,
} from 'lucide-react';
import type { ItemHistoryEntity, ItemHistoryEvent } from '@/types/itemHistory';
import { useItemHistory } from '@/hooks/useItemHistory';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import {
  absoluteTime,
  detailRows,
  eventCategory,
  eventLabel,
  failureCount,
  historyRelativeTime,
  triggerPhrase,
  type HistoryCategory,
} from './itemHistoryFormat';

const CATEGORY_STYLE: Readonly<Record<HistoryCategory, { icon: LucideIcon; tone: string; label: string }>> = {
  acquisition: { icon: Download, tone: 'text-[#e5a00d] border-[#e5a00d]/40', label: 'Acquisition' },
  failure: { icon: AlertTriangle, tone: 'text-[#ef4444] border-[#ef4444]/40', label: 'Failure' },
  file: { icon: FileIcon, tone: 'text-[#22c55e] border-[#22c55e]/40', label: 'File' },
  monitoring: { icon: SlidersHorizontal, tone: 'text-[#a3a3a3] border-[#2a2a2a]', label: 'Monitoring' },
  issues: { icon: Flag, tone: 'text-[#e5a00d] border-[#2a2a2a]', label: 'Issue' },
  health: { icon: HeartPulse, tone: 'text-[#ef4444] border-[#2a2a2a]', label: 'Health' },
};

export interface ItemHistoryModalProps {
  isOpen: boolean;
  onClose: () => void;
  entity: ItemHistoryEntity;
  entityId: string;
  /** Shown as the modal subtitle. */
  title: string;
}

interface RowProps {
  event: ItemHistoryEvent;
  entity: ItemHistoryEntity;
}

const HistoryRow: React.FC<RowProps> = React.memo(({ event, entity }) => {
  const cat = CATEGORY_STYLE[eventCategory(event.event)];
  const Icon = cat.icon;
  const phrase = triggerPhrase(event);
  const failures = event.event === 'download_failed' ? failureCount(event.details) : null;
  const rows = detailRows(event.details);
  const scope: string[] = [];
  if (entity !== 'track' && event.track_title) scope.push(event.track_title);
  if (entity === 'artist' && event.album_title) scope.push(event.album_title);
  return (
    <li className="relative flex gap-3 pb-4 last:pb-0">
      <span className="absolute left-[13px] top-7 bottom-0 w-px bg-[#222222] [li:last-child_&]:hidden" aria-hidden="true" />
      <span
        className={`z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-[3px] border bg-[#0d0d0d] ${cat.tone}`}
        title={cat.label}
      >
        <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-mono text-xs font-bold text-white">{eventLabel(event.event)}</span>
          {failures !== null && (
            <span className="rounded-[2px] border border-[#ef4444]/40 px-1 font-mono text-[10px] text-[#ef4444]">
              failure #{failures}
            </span>
          )}
          <time
            dateTime={event.created_at}
            title={absoluteTime(event.created_at)}
            className="font-mono text-[11px] text-neutral-500"
          >
            {historyRelativeTime(event.created_at)} &middot; {absoluteTime(event.created_at)}
          </time>
        </div>
        {scope.length > 0 && (
          <p className="truncate font-mono text-[11px] text-neutral-400" title={scope.join(' / ')}>
            {scope.join(' · ')}
          </p>
        )}
        {phrase && <p className="font-mono text-[11px] text-[#e5a00d]/80">{phrase}</p>}
        {event.message && <p className="break-words text-xs text-neutral-300">{event.message}</p>}
        {rows.length > 0 && (
          <details className="mt-1 text-[11px]">
            <summary className="cursor-pointer select-none font-mono text-neutral-500 hover:text-neutral-300">
              Details
            </summary>
            <dl className="mt-1 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 rounded-[3px] border border-[#1f1f1f] bg-[#0d0d0d] p-2 font-mono">
              {rows.map(([k, v]) => (
                <React.Fragment key={k}>
                  <dt className="text-neutral-500">{k}</dt>
                  <dd className="break-all text-neutral-300">{v}</dd>
                </React.Fragment>
              ))}
            </dl>
          </details>
        )}
      </div>
    </li>
  );
});
HistoryRow.displayName = 'HistoryRow';

/** An item's audit trail as a newest-first timeline with keyset "load older". Fetches only while open. */
export const ItemHistoryModal: React.FC<ItemHistoryModalProps> = ({ isOpen, onClose, entity, entityId, title }) => {
  const history = useItemHistory(entity, isOpen ? entityId : null);
  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="History"
      subtitle={title}
      footer={
        <TapeDeckButton size="sm" onClick={onClose}>
          Close
        </TapeDeckButton>
      }
    >
      {history.isLoading ? (
        <div className="flex justify-center py-8">
          <Loader2 className="h-6 w-6 animate-spin text-[#e5a00d]" />
        </div>
      ) : history.error && history.events.length === 0 ? (
        <p role="alert" className="py-6 text-center font-mono text-xs text-red-300">
          {history.error}
        </p>
      ) : history.events.length === 0 ? (
        <p className="py-6 text-center font-mono text-xs text-neutral-500">No history recorded yet.</p>
      ) : (
        <>
          <ol className="list-none">
            {history.events.map((ev) => (
              <HistoryRow key={ev.id} event={ev} entity={entity} />
            ))}
          </ol>
          {history.error && (
            <p role="alert" className="pt-2 text-center font-mono text-xs text-red-300">
              {history.error}
            </p>
          )}
          {history.hasMore && (
            <div className="flex justify-center pt-3">
              <TapeDeckButton
                size="sm"
                disabled={history.isLoadingMore}
                onClick={history.loadOlder}
                icon={history.isLoadingMore ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
              >
                Load older
              </TapeDeckButton>
            </div>
          )}
        </>
      )}
    </ObsidianModal>
  );
};
