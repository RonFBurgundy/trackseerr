import { useCallback, useEffect, useState } from 'react';
import { errorMessage } from '@/services/apiClient';

export interface UseLoadedListReturn<T> {
  items: T[];
  setItems: React.Dispatch<React.SetStateAction<T[]>>;
  loading: boolean;
  reload: () => Promise<void>;
}

/** Fetches a list while `enabled` (and again on `reload`); a failure is reported once through `onToast`. */
export function useLoadedList<T>(
  enabled: boolean,
  fetcher: () => Promise<T[]>,
  failureLabel: string,
  onToast: (msg: string, tone?: 'ok' | 'error') => void
): UseLoadedListReturn<T> {
  const [items, setItems] = useState<T[]>([]);
  const [loading, setLoading] = useState<boolean>(false);

  const reload = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    setLoading(true);
    try {
      setItems(await fetcher());
    } catch (err: unknown) {
      onToast(errorMessage(err, failureLabel), 'error');
    } finally {
      setLoading(false);
    }
  }, [enabled, fetcher, failureLabel, onToast]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { items, setItems, loading, reload };
}
