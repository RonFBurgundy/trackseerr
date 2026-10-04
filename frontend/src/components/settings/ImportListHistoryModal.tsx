import React from 'react';
import { Loader2 } from 'lucide-react';
import { ObsidianModal, TabStrip, TapeDeckButton } from '@/components/ui';
import { StatusMessage } from '@/components/ui/FormField';
import { useImportListItems, IMPORT_ITEMS_PAGE_SIZE } from '@/hooks/useImportListItems';
import { LIST_MONITOR_MODE_LABELS, type ImportList, type ImportListItemStatus } from '@/types/importLists';

export interface ImportListHistoryModalProps {
  list: ImportList | null;
  onClose: () => void;
}

const FILTERS: ReadonlyArray<{ value: ImportListItemStatus | null; label: string }> = [
  { value: null, label: 'All' },
  { value: 'applied', label: 'Applied' },
  { value: 'pending', label: 'Pending' },
  { value: 'unresolved', label: 'Unresolved' },
  { value: 'failed', label: 'Failed' },
  { value: 'skipped', label: 'Skipped' },
];

const STATUS_TONE: Readonly<Record<ImportListItemStatus, string>> = {
  applied: 'text-[#22c55e]',
  pending: 'text-[#e5a00d]',
  unresolved: 'text-neutral-400',
  skipped: 'text-neutral-500',
  failed: 'text-[#ef4444]',
};

export const ImportListHistoryModal: React.FC<ImportListHistoryModalProps> = ({ list, onClose }) => {
  const h = useImportListItems(list?.id ?? null);
  const from = h.total === 0 ? 0 : h.offset + 1;
  const to = Math.min(h.offset + IMPORT_ITEMS_PAGE_SIZE, h.total);

  return (
    <ObsidianModal
      isOpen={list !== null}
      onClose={onClose}
      title="Import History"
      subtitle={list?.name}
      maxWidth="sm:max-w-4xl"
      footer={
        <>
          <TapeDeckButton disabled={h.offset === 0 || h.loading} onClick={h.prev}>
            Prev
          </TapeDeckButton>
          <span className="self-center text-xs font-mono text-neutral-400">
            {from}-{to} of {h.total}
          </span>
          <TapeDeckButton disabled={to >= h.total || h.loading} onClick={h.next}>
            Next
          </TapeDeckButton>
        </>
      }
    >
      <div className="space-y-4">
        <TabStrip aria-label="Filter by status">
          {FILTERS.map((f) => (
            <TapeDeckButton
              key={f.label}
              size="sm"
              active={h.status === f.value}
              aria-current={h.status === f.value ? 'page' : undefined}
              onClick={() => h.setStatus(f.value)}
            >
              {f.label}
            </TapeDeckButton>
          ))}
        </TabStrip>

        {h.error && <StatusMessage variant="error">{h.error}</StatusMessage>}

        {h.loading && (
          <div className="flex justify-center py-8">
            <Loader2 className="h-6 w-6 text-[#e5a00d] animate-spin" />
          </div>
        )}

        {!h.loading && !h.error && h.items.length === 0 && (
          <div className="text-center py-8 text-neutral-500 font-mono text-sm">No items.</div>
        )}

        {!h.loading && h.items.length > 0 && (
          <div className="overflow-x-auto border border-[#222222] rounded-[4px]">
            <table className="w-full text-xs font-mono">
              <thead className="bg-[#181818] text-neutral-400 uppercase text-[10px] tracking-wider">
                <tr>
                  <th className="text-left px-3 py-2">Kind</th>
                  <th className="text-left px-3 py-2">Artist</th>
                  <th className="text-left px-3 py-2">Album</th>
                  <th className="text-left px-3 py-2">Track</th>
                  <th className="text-left px-3 py-2">Status</th>
                  <th className="text-left px-3 py-2">Applied</th>
                  <th className="text-left px-3 py-2">Error</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#1f1f1f] text-neutral-300">
                {h.items.map((it) => (
                  <tr key={it.id}>
                    <td className="px-3 py-2 uppercase text-neutral-500">{it.kind}</td>
                    <td className="px-3 py-2 min-w-[8rem]">{it.artist_name ?? '-'}</td>
                    <td className="px-3 py-2 min-w-[8rem]">{it.album_title ?? '-'}</td>
                    <td className="px-3 py-2 min-w-[8rem]">{it.track_title ?? '-'}</td>
                    <td className={`px-3 py-2 uppercase ${STATUS_TONE[it.status]}`}>{it.status}</td>
                    <td className="px-3 py-2">{it.applied_level ? LIST_MONITOR_MODE_LABELS[it.applied_level] : '-'}</td>
                    <td className="px-3 py-2 min-w-[10rem] text-[#ef4444]">{it.error ?? ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </ObsidianModal>
  );
};
