import { useCallback, useState } from 'react';
import type { ListKey } from './useVirtualPagedList';

export interface UseBulkSelectionReturn {
  /** Selection mode is on (checkboxes visible, tiles select instead of open). */
  active: boolean;
  /** Explicitly picked keys. Empty while `allMatching` is true. */
  selected: ReadonlySet<ListKey>;
  /** "Select all" meaning every record server-side, not just the loaded rows. */
  allMatching: boolean;
  /** Number of picked items, or `total` while `allMatching`. */
  count: (total: number) => number;
  isSelected: (key: ListKey) => boolean;
  enter: () => void;
  exit: () => void;
  toggle: (key: ListKey) => void;
  /** Replaces the selection with these keys. */
  selectKeys: (keys: Iterable<ListKey>) => void;
  selectAllMatching: () => void;
  clear: () => void;
}

/** Selection state shared by the artists and albums bulk editors. */
export function useBulkSelection(): UseBulkSelectionReturn {
  const [active, setActive] = useState<boolean>(false);
  const [selected, setSelected] = useState<ReadonlySet<ListKey>>(new Set());
  const [allMatching, setAllMatching] = useState<boolean>(false);

  const clear = useCallback((): void => {
    setSelected(new Set());
    setAllMatching(false);
  }, []);
  const enter = useCallback((): void => setActive(true), []);
  const exit = useCallback((): void => {
    setActive(false);
    setSelected(new Set());
    setAllMatching(false);
  }, []);
  const toggle = useCallback(
    (key: ListKey): void => {
      // With "all" selected the server takes no exclusions, so individual rows are locked until Clear.
      if (allMatching) return;
      setSelected((prev) => {
        const next = new Set(prev);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        return next;
      });
    },
    [allMatching]
  );
  const selectKeys = useCallback((keys: Iterable<ListKey>): void => {
    setAllMatching(false);
    setSelected(new Set(keys));
  }, []);
  const selectAllMatching = useCallback((): void => {
    setSelected(new Set());
    setAllMatching(true);
  }, []);
  const count = useCallback((total: number): number => (allMatching ? total : selected.size), [allMatching, selected]);
  const isSelected = useCallback((key: ListKey): boolean => allMatching || selected.has(key), [allMatching, selected]);

  return { active, selected, allMatching, count, isSelected, enter, exit, toggle, selectKeys, selectAllMatching, clear };
}
