import React, { useId, useState } from 'react';
import { Loader2, X } from 'lucide-react';
import { ConfirmDialog, TapeDeckButton } from '@/components/ui';
import { BulkEditSheet, BulkField, bulkSelectClass } from './BulkEditSheet';

type MonitoredChoice = '' | 'true' | 'false';

function isMonitoredChoice(v: string): v is MonitoredChoice {
  return v === '' || v === 'true' || v === 'false';
}

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
  /** Sends the change; resolves true when the server applied it. The selection stays so edits can be chained. */
  onApply: (monitored: boolean) => Promise<boolean>;
}

/**
 * Mass editor for a selection of albums or tracks (explicit ids; "all" is resolved client-side by paging
 * the filtered list). The backend bulk endpoints for both accept only `monitored`: no quality profile.
 */
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
  const uid = useId();
  const [monitored, setMonitored] = useState<MonitoredChoice>('');
  const [confirming, setConfirming] = useState<boolean>(false);
  const singular = noun.endsWith('s') ? noun.slice(0, -1) : noun;
  const targetLabel = `${count.toLocaleString()} ${count === 1 ? singular : noun}`;

  const handleConfirm = async (): Promise<void> => {
    if (monitored === '') return;
    if (await onApply(monitored === 'true')) setMonitored('');
    setConfirming(false);
  };

  return (
    <>
      <BulkEditSheet
        ariaLabel={`Bulk edit ${noun}`}
        countLabel={`${count.toLocaleString()} selected`}
        applyDisabled={count === 0 || monitored === '' || selectBusy}
        busy={busy}
        onApply={() => setConfirming(true)}
        headerActions={
          <>
            <TapeDeckButton
              type="button"
              size="sm"
              onClick={onSelectAll}
              disabled={busy || selectBusy}
              icon={selectBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
            >
              {selectLabel}
            </TapeDeckButton>
            <TapeDeckButton type="button" size="sm" onClick={onClear} disabled={count === 0 || busy}>
              Clear selection
            </TapeDeckButton>
            <TapeDeckButton type="button" size="sm" onClick={onDone} icon={<X className="h-3.5 w-3.5" />}>
              Done
            </TapeDeckButton>
          </>
        }
      >
        <BulkField id={`${uid}-monitored`} label="Monitored">
          <select
            id={`${uid}-monitored`}
            name="bulk-monitored"
            value={monitored}
            disabled={busy}
            onChange={(e) => {
              if (isMonitoredChoice(e.target.value)) setMonitored(e.target.value);
            }}
            className={bulkSelectClass}
          >
            <option value="">No change</option>
            <option value="true">Monitored</option>
            <option value="false">Unmonitored</option>
          </select>
        </BulkField>
      </BulkEditSheet>
      <ConfirmDialog
        isOpen={confirming}
        title="Apply bulk edit"
        confirmLabel="Apply"
        busy={busy}
        onConfirm={() => void handleConfirm()}
        onCancel={() => setConfirming(false)}
      >
        <p className="font-mono text-xs text-white">
          {`Set ${targetLabel} to ${monitored === 'true' ? 'Monitored' : 'Unmonitored'}`}
        </p>
      </ConfirmDialog>
    </>
  );
};
