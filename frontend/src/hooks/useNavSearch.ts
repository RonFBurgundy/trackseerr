import { useCallback, useMemo, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { buildNavSearchIndex } from '@/components/layout/navSearchIndex';
import type { NavSearchEntry } from '@/components/layout/navSearchIndex';
import { searchNav } from '@/components/layout/navSearch';
import type { RouteAccess } from '@/components/layout/navTree';

export interface UseNavSearchReturn {
  query: string;
  setQuery: (q: string) => void;
  results: NavSearchEntry[];
  activeIndex: number;
  setActiveIndex: (i: number) => void;
  /** Arrow/Enter/Escape handling for the input. Escape clears a non-empty query and reports it handled. */
  onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => void;
}

/** Query state + ranking for the navigation search. `onSelect` fires for Enter and is also what rows call on tap. */
export function useNavSearch(access: RouteAccess, onSelect: (entry: NavSearchEntry) => void): UseNavSearchReturn {
  const { isAdmin, mfaEnrollmentRequired } = access;
  const index = useMemo(() => buildNavSearchIndex({ isAdmin, mfaEnrollmentRequired }), [isAdmin, mfaEnrollmentRequired]);
  const [query, setQueryState] = useState<string>('');
  const [activeIndex, setActiveIndex] = useState<number>(0);
  const results = useMemo(() => (query.trim() ? searchNav(query, index) : []), [query, index]);

  const setQuery = useCallback((q: string): void => {
    setQueryState(q);
    setActiveIndex(0);
  }, []);

  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>): void => {
      if (e.key === 'ArrowDown' && results.length > 0) {
        e.preventDefault();
        setActiveIndex((i) => (i + 1) % results.length);
      } else if (e.key === 'ArrowUp' && results.length > 0) {
        e.preventDefault();
        setActiveIndex((i) => (i - 1 + results.length) % results.length);
      } else if (e.key === 'Enter') {
        const hit = results[activeIndex];
        if (hit) {
          e.preventDefault();
          onSelect(hit);
        }
      } else if (e.key === 'Escape' && query.length > 0) {
        // First Escape clears the box; the hub only closes on the next one.
        e.preventDefault();
        e.stopPropagation();
        setQuery('');
      }
    },
    [results, activeIndex, onSelect, query, setQuery]
  );

  return { query, setQuery, results, activeIndex, setActiveIndex, onKeyDown };
}
