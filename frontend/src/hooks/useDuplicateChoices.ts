import { useCallback, useEffect, useState } from 'react';
import type { DuplicateChoice, DuplicateResolveChoice, DuplicateResolveResponse } from '@/types/models';
import { getDuplicateChoices, resolveDuplicateChoice } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';

export interface UseDuplicateChoicesReturn {
  items: DuplicateChoice[];
  total: number;
  isLoading: boolean;
  error: string | null;
  /** Id of the choice being resolved, if any. */
  resolvingId: number | null;
  /** Server message from the last failed resolve (for example a 409). */
  resolveError: string | null;
  /** Resolves one choice and drops it from `items`. Rejects with the original error after setting `resolveError`. */
  resolve: (id: number, choice: DuplicateResolveChoice) => Promise<DuplicateResolveResponse>;
  reload: () => void;
}

/** Loads the open duplicate-copy choices while `enabled`; resolving one removes it locally. */
export function useDuplicateChoices(enabled: boolean): UseDuplicateChoicesReturn {
  const [items, setItems] = useState<DuplicateChoice[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [resolvingId, setResolvingId] = useState<number | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);
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
        setIsLoading(false);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(errorMessage(err, 'Could not load duplicates'));
        setIsLoading(false);
      });
    return () => controller.abort();
  }, [enabled, reloadTick]);

  const resolve = useCallback(
    async (id: number, choice: DuplicateResolveChoice): Promise<DuplicateResolveResponse> => {
      setResolvingId(id);
      setResolveError(null);
      try {
        const res = await resolveDuplicateChoice(id, choice);
        setItems((prev) => prev.filter((c) => c.id !== id));
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

  const reload = useCallback(() => setReloadTick((n) => n + 1), []);

  return { items, total: items.length, isLoading, error, resolvingId, resolveError, resolve, reload };
}
