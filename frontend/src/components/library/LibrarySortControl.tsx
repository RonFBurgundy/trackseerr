import React from 'react';
import { ArrowDown, ArrowUp } from 'lucide-react';
import type { ListSortDir } from '@/types/activity';
import type { LibrarySortOption } from '@/hooks/useLibraryCatalog';
import { TapeDeckButton, TapeTransportBay } from '@/components/ui';

export interface LibrarySortControlProps {
  options: ReadonlyArray<LibrarySortOption>;
  sortKey: string;
  sortDir: ListSortDir;
  /** A key click flips the direction when it is already active; the arrow button always flips it. */
  onChange: (key: string, dir?: ListSortDir) => void;
}

/** Sort selector: segmented tape-deck keys from `sm` up, a select on phones, and a direction key. */
export const LibrarySortControl: React.FC<LibrarySortControlProps> = ({ options, sortKey, sortDir, onChange }) => {
  const DirIcon = sortDir === 'asc' ? ArrowUp : ArrowDown;
  return (
    <div className="flex w-full sm:w-auto items-stretch gap-1.5" role="group" aria-label="Sort">
      <TapeTransportBay className="hidden sm:flex items-stretch gap-1">
        {options.map((o) => (
          <TapeDeckButton
            key={o.key}
            size="sm"
            active={o.key === sortKey}
            aria-pressed={o.key === sortKey}
            onClick={() => (o.key === sortKey ? undefined : onChange(o.key))}
          >
            {o.label}
          </TapeDeckButton>
        ))}
      </TapeTransportBay>
      <select
        aria-label="Sort by"
        value={sortKey}
        onChange={(e) => onChange(e.target.value)}
        className="sm:hidden flex-1 min-w-0 min-h-[44px] bg-[#141414] border border-[#2a2a2a] rounded-[3px] px-2 text-xs font-mono uppercase text-neutral-200 focus:border-[#e5a00d] focus:outline-none"
      >
        {options.map((o) => (
          <option key={o.key} value={o.key}>
            {o.label}
          </option>
        ))}
      </select>
      <TapeDeckButton
        size="sm"
        onClick={() => onChange(sortKey, sortDir === 'asc' ? 'desc' : 'asc')}
        aria-label={sortDir === 'asc' ? 'Ascending, switch to descending' : 'Descending, switch to ascending'}
        title={sortDir === 'asc' ? 'Ascending' : 'Descending'}
        icon={<DirIcon className="h-3.5 w-3.5" />}
      />
    </div>
  );
};
