import { useCallback, useState } from 'react';
import type { ListKey } from './useVirtualPagedList';

export interface UseListSelectionReturn {
  selected: ReadonlySet<ListKey>;
  setSelected: (next: ReadonlySet<ListKey>) => void;
  clear: () => void;
}

/** Selection state for FlatList (controlled); kept in a hook so views stay declarative. */
export function useListSelection(): UseListSelectionReturn {
  const [selected, setSelectedState] = useState<ReadonlySet<ListKey>>(new Set());
  const setSelected = useCallback((next: ReadonlySet<ListKey>) => setSelectedState(new Set(next)), []);
  const clear = useCallback(() => setSelectedState(new Set()), []);
  return { selected, setSelected, clear };
}
