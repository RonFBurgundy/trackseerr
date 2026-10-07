import React, { useId } from 'react';
import { ChevronRight, CornerDownLeft, Search, X } from 'lucide-react';
import { useNavSearch } from '@/hooks/useNavSearch';
import type { NavSearchEntry } from './navSearchIndex';
import type { RouteAccess } from './navTree';

export interface NavSearchProps {
  access: RouteAccess;
  onSelect: (entry: NavSearchEntry) => void;
  /** Reports whether a query is active so the hub can swap its tree for the results. */
  onActiveChange?: (active: boolean) => void;
}

/** Combobox: type to find any page or individual setting; arrows + Enter, or tap, to go. */
export const NavSearch: React.FC<NavSearchProps> = ({ access, onSelect, onActiveChange }) => {
  const inputId = useId();
  const listId = useId();
  const { query, setQuery, results, activeIndex, setActiveIndex, onKeyDown } = useNavSearch(access, onSelect);
  const active = query.trim().length > 0;

  const change = (value: string): void => {
    setQuery(value);
    onActiveChange?.(value.trim().length > 0);
  };

  return (
    <div className="pb-1.5">
      <label htmlFor={inputId} className="sr-only">
        Search pages and settings
      </label>
      <div className="tape-transport-bay flex items-center gap-2 px-2 h-9">
        <Search className="h-4 w-4 shrink-0 text-[var(--text-muted)]" aria-hidden="true" />
        <input
          id={inputId}
          name="nav-search"
          type="text"
          role="combobox"
          aria-expanded={active}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={active && results[activeIndex] ? `${listId}-${activeIndex}` : undefined}
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          placeholder="Jump to a page or setting"
          value={query}
          onChange={(e) => change(e.target.value)}
          onKeyDown={(e) => {
            onKeyDown(e);
            if (e.key === 'Escape' && query.length > 0) onActiveChange?.(false);
          }}
          className="min-w-0 flex-1 bg-transparent text-[13px] font-mono text-white placeholder:text-[var(--text-muted)] outline-none"
        />
        {query.length > 0 && (
          <button
            type="button"
            aria-label="Clear search"
            onClick={() => change('')}
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded-[3px] text-[var(--text-secondary)] hover:text-white"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      {active && (
        <ul id={listId} role="listbox" aria-label="Search results" className="mt-1.5 flex flex-col gap-0.5">
          {results.length === 0 && (
            <li className="px-2.5 py-2 text-[12px] font-mono text-[var(--text-muted)]">No matching page or setting</li>
          )}
          {results.map((entry, i) => (
            <li
              key={entry.key}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === activeIndex}
              onMouseEnter={() => setActiveIndex(i)}
              onClick={() => onSelect(entry)}
              className={`flex min-h-[40px] cursor-pointer items-center gap-2 rounded-[3px] border px-2.5 py-1 ${
                i === activeIndex ? 'border-[var(--accent-amber)]/50 bg-[#151515]' : 'border-[#1e1e1e] bg-[#121212]'
              }`}
            >
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] font-mono font-bold text-white">{entry.label}</div>
                <div className="flex min-w-0 items-center gap-0.5 text-[10px] font-mono text-[var(--text-secondary)]">
                  {entry.breadcrumb.slice(0, -1).map((crumb, n) => (
                    <React.Fragment key={`${crumb}-${n}`}>
                      {n > 0 && <ChevronRight className="h-2.5 w-2.5 shrink-0" aria-hidden="true" />}
                      <span className="truncate">{crumb}</span>
                    </React.Fragment>
                  ))}
                </div>
              </div>
              {i === activeIndex && <CornerDownLeft className="h-3.5 w-3.5 shrink-0 text-[var(--accent-amber)]" aria-hidden="true" />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};
