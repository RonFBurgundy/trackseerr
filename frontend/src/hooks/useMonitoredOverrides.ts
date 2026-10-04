import { useCallback, useMemo, useState } from 'react';
import type { ListKey } from './useVirtualPagedList';

export interface MonitoredOverrides {
  /** The value to render: the pending optimistic value when one is set, else the server's. */
  resolve: <T extends boolean | null>(key: ListKey, serverValue: T) => boolean | T;
  set: (key: ListKey, value: boolean) => void;
  clear: (key: ListKey) => void;
}

/**
 * Optimistic monitored flags for rows of a paged list. The list cache is read-only, so a toggle is shown through
 * this overlay until the list has re-fetched the row.
 */
export function useMonitoredOverrides(): MonitoredOverrides {
  const [map, setMap] = useState<ReadonlyMap<ListKey, boolean>>(new Map());
  const set = useCallback((key: ListKey, value: boolean): void => {
    setMap((prev) => new Map(prev).set(key, value));
  }, []);
  const clear = useCallback((key: ListKey): void => {
    setMap((prev) => {
      if (!prev.has(key)) return prev;
      const next = new Map(prev);
      next.delete(key);
      return next;
    });
  }, []);
  const resolve = useCallback(
    <T extends boolean | null>(key: ListKey, serverValue: T): boolean | T => map.get(key) ?? serverValue,
    [map]
  );
  return useMemo(() => ({ resolve, set, clear }), [resolve, set, clear]);
}
