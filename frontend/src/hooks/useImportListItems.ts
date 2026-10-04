import { useCallback, useEffect, useState } from 'react';
import type { ImportListItemOut, ImportListItemStatus } from '@/types/importLists';
import { getImportListItems } from '@/services/importListsService';
import { errorMessage } from '@/services/apiClient';

export const IMPORT_ITEMS_PAGE_SIZE = 50;

export interface UseImportListItemsReturn {
  items: ImportListItemOut[];
  total: number;
  offset: number;
  loading: boolean;
  error: string | null;
  status: ImportListItemStatus | null;
  setStatus: (status: ImportListItemStatus | null) => void;
  next: () => void;
  prev: () => void;
}

/** Paged item history for one import list; inactive while `listId` is null. */
export function useImportListItems(listId: string | null): UseImportListItemsReturn {
  const [items, setItems] = useState<ImportListItemOut[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [offset, setOffset] = useState<number>(0);
  const [status, setStatusState] = useState<ImportListItemStatus | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setOffset(0);
    setStatusState(null);
    setItems([]);
    setTotal(0);
  }, [listId]);

  useEffect(() => {
    if (!listId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getImportListItems(listId, status, IMPORT_ITEMS_PAGE_SIZE, offset)
      .then((page) => {
        if (cancelled) return;
        setItems(page.items);
        setTotal(page.total);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorMessage(err, 'Failed to load items'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [listId, status, offset]);

  const setStatus = useCallback((s: ImportListItemStatus | null): void => {
    setStatusState(s);
    setOffset(0);
  }, []);
  const next = useCallback((): void => setOffset((o) => o + IMPORT_ITEMS_PAGE_SIZE), []);
  const prev = useCallback((): void => setOffset((o) => Math.max(0, o - IMPORT_ITEMS_PAGE_SIZE)), []);

  return { items, total, offset, loading, error, status, setStatus, next, prev };
}
