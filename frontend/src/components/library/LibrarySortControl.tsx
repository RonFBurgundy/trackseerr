import React, { useId } from 'react';
import { ArrowDown, ArrowUp } from 'lucide-react';
import type { ListSortDir } from '@/types/activity';
import type { LibrarySortOption } from '@/hooks/useLibraryCatalog';
import { TapeDeckButton } from '@/components/ui';

export interface LibrarySortControlProps {
  options: ReadonlyArray<LibrarySortOption>;
  sortKey: string;
  sortDir: ListSortDir;
  /** A new key starts at its default direction; the arrow key flips the current one. */
  onChange: (key: string, dir?: ListSortDir) => void;
}

/** Compact sort: a key-styled select plus a direction key. Two small controls, so it fits the one-row toolbar. */
export const LibrarySortControl: React.FC<LibrarySortControlProps> = ({ options, sortKey, sortDir, onChange }) => {
  const id = useId();
  const DirIcon = sortDir === 'asc' ? ArrowUp : ArrowDown;
  return (
    <div className="flex shrink-0 items-stretch gap-1" role="group" aria-label="Sort">
      <select
        id={`${id}-sort`}
        name="library-sort"
        aria-label="Sort by"
        value={sortKey}
        onChange={(e) => onChange(e.target.value)}
        className="tape-deck-btn min-h-[44px] sm:min-h-[32px] w-[84px] sm:w-auto rounded-[3px] px-2 text-xs font-mono font-semibold uppercase text-neutral-200 focus:border-[var(--accent-amber)] focus:outline-none"
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
