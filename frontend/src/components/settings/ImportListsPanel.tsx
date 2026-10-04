import React, { useCallback, useState } from 'react';
import { Plus, RefreshCw, Pencil, History, Trash2, Loader2 } from 'lucide-react';
import { TapeDeckButton, MachinedCard, TactileSwitch, ActionBar, ConfirmDangerButton } from '@/components/ui';
import { useImportLists } from '@/hooks/useImportLists';
import { useImportListEditor } from '@/hooks/useImportListEditor';
import { useMonitoringDefaults } from '@/hooks/useMonitoringDefaults';
import { useQualityProfiles } from '@/hooks/useQualityProfiles';
import type { LibraryManagerMode } from '@/types/models';
import { LIST_MONITOR_MODE_LABELS, type ImportList } from '@/types/importLists';
import { ImportListEditorModal } from './ImportListEditorModal';
import { ImportListHistoryModal } from './ImportListHistoryModal';
import { ItunesImportCard } from './ItunesImportCard';

export interface ImportListsPanelProps {
  libraryMode: LibraryManagerMode;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

const COUNT_LABELS: ReadonlyArray<{ key: keyof ImportList['item_counts']; label: string }> = [
  { key: 'applied', label: 'applied' },
  { key: 'pending', label: 'pending' },
  { key: 'unresolved', label: 'unresolved' },
  { key: 'failed', label: 'failed' },
  { key: 'skipped', label: 'skipped' },
];

export const ImportListsPanel: React.FC<ImportListsPanelProps> = ({ libraryMode, onToast }) => {
  const toast = useCallback((msg: string, tone: 'ok' | 'error' = 'ok') => onToast(msg, tone), [onToast]);
  const lists = useImportLists(true, toast);
  const defaults = useMonitoringDefaults();
  const quality = useQualityProfiles(true, toast);
  const editor = useImportListEditor(lists.providers, defaults.addMonitorOption, lists.refresh, toast);
  const [historyList, setHistoryList] = useState<ImportList | null>(null);

  const providerLabel = (id: string): string => lists.providers.find((p) => p.provider === id)?.label ?? id;

  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div className="text-xs font-mono text-neutral-400">
          Pull artists, albums and tracks from external services on a schedule.
        </div>
        <ActionBar align="end">
          <TapeDeckButton
            variant="amber"
            icon={<Plus className="h-4 w-4" />}
            disabled={lists.providers.length === 0}
            onClick={editor.openNew}
          >
            Add List
          </TapeDeckButton>
        </ActionBar>
      </div>

      {lists.loading && (
        <div className="flex justify-center py-12">
          <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
        </div>
      )}

      {!lists.loading && lists.lists.length === 0 && (
        <div className="text-center py-12 text-neutral-500 font-mono text-sm">No import lists configured.</div>
      )}

      <div className="grid grid-cols-1 gap-3">
        {lists.lists.map((l) => (
          <MachinedCard key={l.id} className="p-4 space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h4 className="font-bold text-base text-white truncate" title={l.name}>
                  {l.name}
                </h4>
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] font-mono text-neutral-400">
                  <span className="px-2 py-0.5 rounded-[2px] bg-[#1a1a1a] border border-[#2a2a2a] uppercase text-neutral-300">
                    {providerLabel(l.provider)}
                  </span>
                  <span>Mode: {LIST_MONITOR_MODE_LABELS[l.monitor_mode]}</span>
                </div>
              </div>
              <TactileSwitch
                checked={l.enabled}
                onChange={(v) => void lists.setEnabled(l, v)}
                title={l.enabled ? 'Disable list' : 'Enable list'}
              />
            </div>

            <div className="text-[11px] font-mono space-y-1">
              <div className="flex flex-wrap items-center gap-2 text-neutral-400">
                <span>{l.last_synced_at ? `Last sync: ${new Date(l.last_synced_at).toLocaleString()}` : 'Never synced'}</span>
                {l.last_status && (
                  <span className={`uppercase ${l.last_status === 'ok' ? 'text-[#22c55e]' : 'text-[#ef4444]'}`}>
                    {l.last_status}
                  </span>
                )}
              </div>
              {l.last_status === 'error' && l.last_error && (
                <div className="text-[#ef4444] break-words" role="alert">
                  {l.last_error}
                </div>
              )}
              <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-neutral-500">
                {COUNT_LABELS.map((c) => (
                  <span key={c.key}>
                    {l.item_counts[c.key]} {c.label}
                  </span>
                ))}
              </div>
            </div>

            <ActionBar bay className="pt-1">
              <TapeDeckButton
                size="sm"
                disabled={lists.syncingIds.has(l.id)}
                onClick={() => void lists.syncNow(l.id)}
                icon={<RefreshCw className={`h-3.5 w-3.5 ${lists.syncingIds.has(l.id) ? 'animate-spin' : ''}`} />}
              >
                Sync now
              </TapeDeckButton>
              <TapeDeckButton size="sm" onClick={() => editor.openEdit(l)} icon={<Pencil className="h-3.5 w-3.5" />}>
                Edit
              </TapeDeckButton>
              <TapeDeckButton size="sm" onClick={() => setHistoryList(l)} icon={<History className="h-3.5 w-3.5" />}>
                History
              </TapeDeckButton>
              <ConfirmDangerButton
                idleLabel="Delete"
                ariaLabel={`Delete ${l.name}`}
                icon={<Trash2 className="h-3.5 w-3.5" />}
                onConfirm={() => void lists.remove(l.id)}
              />
            </ActionBar>
          </MachinedCard>
        ))}
      </div>

      <ItunesImportCard />

      <ImportListEditorModal
        editor={editor}
        providers={lists.providers}
        profiles={quality.profiles}
        libraryMode={libraryMode}
      />
      <ImportListHistoryModal list={historyList} onClose={() => setHistoryList(null)} />
    </div>
  );
};
