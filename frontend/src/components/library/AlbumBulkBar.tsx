import React from 'react';
import { Eye, EyeOff, Loader2, X } from 'lucide-react';
import { ActionBar, MachinedCard, TapeDeckButton } from '@/components/ui';

export interface AlbumBulkBarProps {
  count: number;
  busy: boolean;
  /** Plural noun for the labels. Default `albums`. */
  noun?: string;
  /** True while the full filtered selection is being fetched. */
  selectBusy?: boolean;
  /** Label for the select-everything key, e.g. "All 120 albums". */
  selectLabel: string;
  onSelectAll: () => void;
  onClear: () => void;
  onDone: () => void;
  onApply: (monitored: boolean) => void;
}

/** Monitor/Unmonitor bar for a selection of albums or tracks (explicit ids; "all" is resolved client-side by paging the filtered list). */
export const AlbumBulkBar: React.FC<AlbumBulkBarProps> = ({
  count,
  busy,
  noun = 'albums',
  selectBusy = false,
  selectLabel,
  onSelectAll,
  onClear,
  onDone,
  onApply,
}) => {
  const disabled = count === 0 || busy || selectBusy;
  return (
    <MachinedCard className="p-3 space-y-3" aria-label={`Bulk edit ${noun}`}>
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <span className="text-xs font-mono font-bold uppercase text-[#e5a00d]">{count.toLocaleString()} selected</span>
        <ActionBar align="end">
          <TapeDeckButton
            size="sm"
            onClick={onSelectAll}
            disabled={busy || selectBusy}
            icon={selectBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
          >
            {selectLabel}
          </TapeDeckButton>
          <TapeDeckButton size="sm" onClick={onClear} disabled={count === 0 || busy}>
            Clear
          </TapeDeckButton>
          <TapeDeckButton size="sm" onClick={onDone} icon={<X className="h-3.5 w-3.5" />}>
            Done
          </TapeDeckButton>
        </ActionBar>
      </div>
      <ActionBar>
        <TapeDeckButton
          size="sm"
          variant="amber"
          disabled={disabled}
          onClick={() => onApply(true)}
          icon={busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Eye className="h-3.5 w-3.5" />}
        >
          Monitor
        </TapeDeckButton>
        <TapeDeckButton size="sm" disabled={disabled} onClick={() => onApply(false)} icon={<EyeOff className="h-3.5 w-3.5" />}>
          Unmonitor
        </TapeDeckButton>
      </ActionBar>
    </MachinedCard>
  );
};
