import { useCallback, useEffect, useMemo, useState } from 'react';
import type { DuplicateChoice, DuplicateResolveChoice, DuplicateResolveResponse } from '@/types/models';
import { getDuplicateChoices, resolveDuplicateChoice } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';

export type DuplicateKind = DuplicateChoice['kind'];

export interface BatchSummary {
  resolved: number;
  skipped: number;
  errors: number;
  errorMessages: string[];
}

export interface BatchProgress {
  done: number;
  total: number;
}

export interface UseDuplicateChoicesReturn {
  items: DuplicateChoice[];
  filteredItems: DuplicateChoice[];
  total: number;
  isLoading: boolean;
  error: string | null;
  /** Id of the choice being resolved, if any. */
  resolvingId: number | null;
  /** Server message from the last failed resolve (for example a 409). */
  resolveError: string | null;
  selectedIds: ReadonlySet<number>;
  toggleSelect: (id: number) => void;
  selectAll: () => void;
  selectNone: () => void;
  selectSafe: () => void;
  activeKinds: ReadonlySet<DuplicateKind>;
  toggleKind: (kind: DuplicateKind) => void;
  clearKinds: () => void;
  kindCounts: Record<DuplicateKind, number>;
  batchProgress: BatchProgress | null;
  /** Resolves one choice and drops it from `items` and `selectedIds`. Rejects with the original error after setting `resolveError`. */
  resolve: (id: number, choice: DuplicateResolveChoice) => Promise<DuplicateResolveResponse>;
  /** Resolves `ids` sequentially, skipping ambiguous items for quality/keep_existing/keep_incoming choices. */
  resolveMany: (ids: number[], choice: DuplicateResolveChoice) => Promise<BatchSummary>;
  reload: () => void;
}

/** Loads the open duplicate-copy choices while `enabled`; resolving one removes it locally. */
export function useDuplicateChoices(enabled: boolean): UseDuplicateChoicesReturn {
  const [items, setItems] = useState<DuplicateChoice[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [resolvingId, setResolvingId] = useState<number | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(() => new Set());
  const [activeKinds, setActiveKinds] = useState<Set<DuplicateKind>>(() => new Set());
  const [batchProgress, setBatchProgress] = useState<BatchProgress | null>(null);
  const [reloadTick, setReloadTick] = useState<number>(0);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setIsLoading(true);
    setError(null);
    getDuplicateChoices(controller.signal)
      .then((res) => {
        if (controller.signal.aborted) return;
        setItems(res.items);
        setSelectedIds(new Set());
        setActiveKinds(new Set());
        setIsLoading(false);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(errorMessage(err, 'Could not load duplicates'));
        setIsLoading(false);
      });
    return () => controller.abort();
  }, [enabled, reloadTick]);

  const kindCounts = useMemo<Record<DuplicateKind, number>>(() => {
    const counts: Record<DuplicateKind, number> = { duplicate_files: 0, ambiguous: 0 };
    for (const item of items) {
      counts[item.kind] += 1;
    }
    return counts;
  }, [items]);

  const filteredItems = useMemo(
    () => (activeKinds.size === 0 ? items : items.filter((item) => activeKinds.has(item.kind))),
    [items, activeKinds]
  );

  const toggleSelect = useCallback((id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const selectAll = useCallback(() => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const item of filteredItems) next.add(item.id);
      return next;
    });
  }, [filteredItems]);

  const selectNone = useCallback(() => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const item of filteredItems) next.delete(item.id);
      return next;
    });
  }, [filteredItems]);

  const selectSafe = useCallback(() => {
    setSelectedIds(
      new Set(
        filteredItems
          .filter((item) => item.kind === 'duplicate_files' && item.pairs.length > 0)
          .map((item) => item.id)
      )
    );
  }, [filteredItems]);

  const toggleKind = useCallback((kind: DuplicateKind) => {
    setActiveKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  }, []);

  const clearKinds = useCallback(() => {
    setActiveKinds(new Set());
  }, []);

  const resolve = useCallback(
    async (id: number, choice: DuplicateResolveChoice): Promise<DuplicateResolveResponse> => {
      setResolvingId(id);
      setResolveError(null);
      try {
        const res = await resolveDuplicateChoice(id, choice);
        setItems((prev) => prev.filter((c) => c.id !== id));
        setSelectedIds((prev) => {
          if (!prev.has(id)) return prev;
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
        return res;
      } catch (err: unknown) {
        setResolveError(errorMessage(err, 'Could not resolve this duplicate'));
        throw err;
      } finally {
        setResolvingId(null);
      }
    },
    []
  );

  const resolveMany = useCallback(
    async (ids: number[], choice: DuplicateResolveChoice): Promise<BatchSummary> => {
      if (ids.length === 0) {
        return { resolved: 0, skipped: 0, errors: 0, errorMessages: [] };
      }

      const byId = new Map<number, DuplicateChoice>(items.map((item) => [item.id, item]));
      const skipAmbiguous =
        choice === 'prefer_quality' || choice === 'keep_existing' || choice === 'keep_incoming';

      let resolved = 0;
      let skipped = 0;
      let errors = 0;
      const errorMessages: string[] = [];
      const resolvedIds: number[] = [];

      setResolveError(null);
      setBatchProgress({ done: 0, total: ids.length });

      try {
        for (let i = 0; i < ids.length; i += 1) {
          const id = ids[i];
          const item = byId.get(id);
          if (item?.kind === 'ambiguous' && skipAmbiguous) {
            skipped += 1;
          } else {
            try {
              await resolveDuplicateChoice(id, choice);
              resolved += 1;
              resolvedIds.push(id);
            } catch (err: unknown) {
              errors += 1;
              const label = item ? `${item.source_title} → ${item.holder_title}` : `#${id}`;
              errorMessages.push(`${label}: ${errorMessage(err, 'Could not resolve this duplicate')}`);
            }
          }
          setBatchProgress({ done: i + 1, total: ids.length });
        }
      } finally {
        if (resolvedIds.length > 0) {
          const resolvedSet = new Set(resolvedIds);
          setItems((prev) => prev.filter((c) => !resolvedSet.has(c.id)));
          setSelectedIds((prev) => {
            const next = new Set(prev);
            for (const id of resolvedSet) next.delete(id);
            return next;
          });
        }
        setBatchProgress(null);
      }

      return { resolved, skipped, errors, errorMessages };
    },
    [items]
  );

  const reload = useCallback(() => setReloadTick((n) => n + 1), []);

  return {
    items,
    filteredItems,
    total: items.length,
    isLoading,
    error,
    resolvingId,
    resolveError,
    selectedIds,
    toggleSelect,
    selectAll,
    selectNone,
    selectSafe,
    activeKinds,
    toggleKind,
    clearKinds,
    kindCounts,
    batchProgress,
    resolve,
    resolveMany,
    reload,
  };
}
