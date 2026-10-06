import { useCallback, useEffect, useRef, useState } from 'react';
import type { ItemHistoryEntity, ItemHistoryEvent, ItemHistoryOrigin } from '@/types/itemHistory';
import { getItemHistory } from '@/services/itemHistoryService';
import { errorMessage } from '@/services/apiClient';

const PAGE_SIZE = 50;

export interface UseItemHistoryReturn {
  events: ItemHistoryEvent[];
  origin: ItemHistoryOrigin | null;
  isLoading: boolean;
  isLoadingMore: boolean;
  error: string | null;
  hasMore: boolean;
  loadOlder: () => void;
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

/**
 * An item's audit trail, newest first, with keyset "load older" paging. Pass `id === null` to stay idle (nothing is
 * fetched until the history is actually opened). In-flight requests are aborted on id change and unmount.
 */
export function useItemHistory(entity: ItemHistoryEntity, id: string | null): UseItemHistoryReturn {
  const [events, setEvents] = useState<ItemHistoryEvent[]>([]);
  const [origin, setOrigin] = useState<ItemHistoryOrigin | null>(null);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [isLoadingMore, setIsLoadingMore] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    controllerRef.current?.abort();
    setEvents([]);
    setOrigin(null);
    setNextBefore(null);
    setError(null);
    setIsLoadingMore(false);
    if (id === null) {
      setIsLoading(false);
      return undefined;
    }
    const controller = new AbortController();
    controllerRef.current = controller;
    setIsLoading(true);
    getItemHistory(entity, id, { limit: PAGE_SIZE }, controller.signal)
      .then((res) => {
        setEvents(res.events);
        setOrigin(res.origin);
        setNextBefore(res.next_before);
        setIsLoading(false);
      })
      .catch((err: unknown) => {
        if (isAbort(err) || controller.signal.aborted) return;
        setError(errorMessage(err, 'Failed to load history'));
        setIsLoading(false);
      });
    return () => controller.abort();
  }, [entity, id]);

  const loadOlder = useCallback((): void => {
    if (id === null || nextBefore === null || isLoadingMore) return;
    const controller = new AbortController();
    controllerRef.current = controller;
    setIsLoadingMore(true);
    setError(null);
    getItemHistory(entity, id, { limit: PAGE_SIZE, before: nextBefore }, controller.signal)
      .then((res) => {
        setEvents((prev) => [...prev, ...res.events]);
        setNextBefore(res.next_before);
        setIsLoadingMore(false);
      })
      .catch((err: unknown) => {
        if (isAbort(err) || controller.signal.aborted) return;
        setError(errorMessage(err, 'Failed to load older history'));
        setIsLoadingMore(false);
      });
  }, [entity, id, nextBefore, isLoadingMore]);

  return { events, origin, isLoading, isLoadingMore, error, hasMore: nextBefore !== null, loadOlder };
}

export interface UseItemOriginReturn {
  origin: ItemHistoryOrigin | null;
  isLoading: boolean;
}

/** Just the item's origin (the earliest event) for the discreet caption; one tiny request, aborted on change. */
export function useItemOrigin(entity: ItemHistoryEntity, id: string | null): UseItemOriginReturn {
  const [origin, setOrigin] = useState<ItemHistoryOrigin | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);

  useEffect(() => {
    setOrigin(null);
    if (id === null) {
      setIsLoading(false);
      return undefined;
    }
    const controller = new AbortController();
    setIsLoading(true);
    getItemHistory(entity, id, { limit: 1 }, controller.signal)
      .then((res) => {
        setOrigin(res.origin);
        setIsLoading(false);
      })
      .catch((err: unknown) => {
        if (isAbort(err) || controller.signal.aborted) return;
        console.warn('Loading item origin failed', err);
        setIsLoading(false);
      });
    return () => controller.abort();
  }, [entity, id]);

  return { origin, isLoading };
}
