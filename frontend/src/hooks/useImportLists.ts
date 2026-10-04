import { useCallback, useEffect, useState } from 'react';
import { importListToInput, type ImportList, type ProviderMeta } from '@/types/importLists';
import {
  getImportLists,
  getImportListProviders,
  updateImportList,
  deleteImportList,
  syncImportList,
} from '@/services/importListsService';
import { errorMessage } from '@/services/apiClient';
import { usePolling } from './usePolling';

export type ImportListToast = (msg: string, tone?: 'ok' | 'error') => void;

export interface UseImportListsReturn {
  lists: ImportList[];
  providers: ProviderMeta[];
  loading: boolean;
  syncingIds: ReadonlySet<string>;
  refresh: () => Promise<void>;
  syncNow: (id: string) => Promise<void>;
  setEnabled: (list: ImportList, enabled: boolean) => Promise<void>;
  remove: (id: string) => Promise<void>;
}

export function useImportLists(enabled: boolean, onToast: ImportListToast): UseImportListsReturn {
  const [lists, setLists] = useState<ImportList[]>([]);
  const [providers, setProviders] = useState<ProviderMeta[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [syncingIds, setSyncingIds] = useState<ReadonlySet<string>>(new Set<string>());

  const refresh = useCallback(async (): Promise<void> => {
    try {
      setLists(await getImportLists());
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to load import lists'), 'error');
    }
  }, [onToast]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setLoading(true);
    Promise.all([getImportLists(), getImportListProviders()])
      .then(([l, p]) => {
        if (cancelled) return;
        setLists(l);
        setProviders(p);
      })
      .catch((err: unknown) => {
        if (!cancelled) onToast(errorMessage(err, 'Failed to load import lists'), 'error');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, onToast]);

  // Pick up background sync results while the tab is open.
  usePolling(() => void refresh(), 15000, enabled);

  const syncNow = useCallback(
    async (id: string): Promise<void> => {
      setSyncingIds((prev) => new Set(prev).add(id));
      try {
        await syncImportList(id);
        onToast('Sync queued');
        window.setTimeout(() => void refresh(), 2500);
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to queue sync'), 'error');
      } finally {
        setSyncingIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    },
    [onToast, refresh]
  );

  const setEnabled = useCallback(
    async (list: ImportList, value: boolean): Promise<void> => {
      setLists((prev) => prev.map((l) => (l.id === list.id ? { ...l, enabled: value } : l)));
      try {
        await updateImportList(list.id, { ...importListToInput(list), enabled: value });
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to update import list'), 'error');
        await refresh();
      }
    },
    [onToast, refresh]
  );

  const remove = useCallback(
    async (id: string): Promise<void> => {
      try {
        await deleteImportList(id);
        setLists((prev) => prev.filter((l) => l.id !== id));
        onToast('Import list deleted');
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to delete import list'), 'error');
      }
    },
    [onToast]
  );

  return { lists, providers, loading, syncingIds, refresh, syncNow, setEnabled, remove };
}
