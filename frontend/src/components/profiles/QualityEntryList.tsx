import React, { useId, useState } from 'react';
import { Link2, Unlink } from 'lucide-react';
import { SortableList, SortControls, TapeDeckButton } from '@/components/ui';
import type { SortableRowState } from '@/components/ui';
import type { QualityEntry } from '@/types/qualityProfiles';
import { entryLabel } from '@/hooks/useQualityProfileDraft';
import { compactInputClass } from '@/components/settings/formClasses';

export interface QualityEntryListProps {
  entries: QualityEntry[];
  titleOf: (quality: string) => string;
  selected: readonly string[];
  onReorder: (next: QualityEntry[]) => void;
  onToggleAllowed: (index: number) => void;
  onToggleSelected: (quality: string) => void;
  onRenameGroup: (index: number, name: string) => void;
  onCreateGroup: (name: string) => string | null;
  onUngroup: (index: number) => void;
}

const checkClass = 'h-5 w-5 shrink-0 accent-[#e5a00d]';

interface RowProps {
  entry: QualityEntry;
  state: SortableRowState;
  titleOf: (quality: string) => string;
  selected: boolean;
  onToggleAllowed: () => void;
  onToggleSelected: () => void;
  onRenameGroup: (name: string) => void;
  onUngroup: () => void;
}

const EntryRow: React.FC<RowProps> = ({ entry, state, titleOf, selected, onToggleAllowed, onToggleSelected, onRenameGroup, onUngroup }) => {
  const uid = useId();
  const label = entry.type === 'group' ? entry.name || 'group' : titleOf(entry.quality);
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-[var(--border-subtle)] px-2 py-1 last:border-b-0">
      {entry.type === 'quality' ? (
        <input
          id={`${uid}-select`}
          name={`select-${entry.quality}`}
          type="checkbox"
          checked={selected}
          onChange={onToggleSelected}
          aria-label={`Select ${label} for grouping`}
          className={checkClass}
        />
      ) : (
        <span className="inline-flex h-5 w-5 shrink-0 items-center justify-center text-[var(--accent-amber)]" aria-hidden="true">
          <Link2 className="h-3.5 w-3.5" />
        </span>
      )}
      <div className="min-w-0 flex-1 basis-24">
        {entry.type === 'group' ? (
          <>
            <input
              id={`${uid}-name`}
              name={`group-name-${state.index}`}
              type="text"
              aria-label={`Group name for ${label}`}
              value={entry.name}
              maxLength={60}
              autoComplete="off"
              onChange={(e) => onRenameGroup(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') e.preventDefault();
              }}
              className={compactInputClass}
            />
            <p className="mt-0.5 truncate text-[11px] font-mono text-[var(--text-muted)]">{entry.items.map(titleOf).join(' + ')}</p>
          </>
        ) : (
          <span className="text-[13px] text-white">{label}</span>
        )}
      </div>
      <label htmlFor={`${uid}-allowed`} className="inline-flex items-center gap-1.5 text-[11px] font-mono text-neutral-300">
        <input
          id={`${uid}-allowed`}
          name={`allowed-${entryLabel(entry)}`}
          type="checkbox"
          checked={entry.allowed}
          onChange={onToggleAllowed}
          className={checkClass}
        />
        <span className="hidden sm:inline">Allowed</span>
        <span className="sm:hidden sr-only">Allowed</span>
      </label>
      {entry.type === 'group' && (
        <TapeDeckButton size="sm" aria-label={`Ungroup ${label}`} onClick={onUngroup} icon={<Unlink className="h-3.5 w-3.5" />} />
      )}
      <SortControls state={state} label={label} />
    </div>
  );
};

/** Ordered quality list (top = best): drag or arrow-key reorder, allowed checkboxes, group/ungroup. */
export const QualityEntryList: React.FC<QualityEntryListProps> = ({
  entries,
  titleOf,
  selected,
  onReorder,
  onToggleAllowed,
  onToggleSelected,
  onRenameGroup,
  onCreateGroup,
  onUngroup,
}) => {
  const groupNameId = useId();
  const [groupName, setGroupName] = useState<string>('');
  const [groupError, setGroupError] = useState<string | null>(null);

  const group = (): void => {
    const err = onCreateGroup(groupName);
    setGroupError(err);
    if (!err) setGroupName('');
  };

  return (
    <div className="space-y-2">
      <div className="rounded-[4px] border border-[var(--border-default)] bg-[var(--bg-card)]">
        <SortableList
          ariaLabel="Qualities, best first"
          items={entries}
          getKey={(e) => (e.type === 'group' ? `group:${e.items[0] ?? ''}` : `quality:${e.quality}`)}
          onReorder={onReorder}
          renderItem={(entry, state) => (
            <EntryRow
              entry={entry}
              state={state}
              titleOf={titleOf}
              selected={entry.type === 'quality' && selected.includes(entry.quality)}
              onToggleAllowed={() => onToggleAllowed(state.index)}
              onToggleSelected={() => entry.type === 'quality' && onToggleSelected(entry.quality)}
              onRenameGroup={(name) => onRenameGroup(state.index, name)}
              onUngroup={() => onUngroup(state.index)}
            />
          )}
        />
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-0 flex-1 basis-40">
          <label htmlFor={groupNameId} className="mb-1 block text-[11px] font-mono text-neutral-300">
            New group name
          </label>
          <input
            id={groupNameId}
            name="new-group-name"
            type="text"
            maxLength={60}
            autoComplete="off"
            placeholder="e.g. Lossless"
            value={groupName}
            onChange={(e) => setGroupName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                group();
              }
            }}
            className={compactInputClass}
          />
        </div>
        <TapeDeckButton size="sm" disabled={selected.length < 2} onClick={group} icon={<Link2 className="h-3.5 w-3.5" />}>
          Group selected ({selected.length})
        </TapeDeckButton>
      </div>
      {groupError && (
        <p role="alert" className="text-[11px] font-mono text-[var(--status-error)]">
          {groupError}
        </p>
      )}
    </div>
  );
};
