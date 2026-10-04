import { useCallback, useMemo, useState } from 'react';
import type { SystemEventItem } from '@/types/models';
import { getSystemEvents, clearSystemEvents } from '@/services/systemService';
import { errorMessage } from '@/services/apiClient';
import { useVirtualPagedList, type FetchPage, type VirtualPagedList } from './useVirtualPagedList';

export const EVENTS_PAGE_SIZE = 50;

export interface UseSystemEventsReturn {
  list: VirtualPagedList<SystemEventItem>;
  isClearing: boolean;
  /** Failure of the clear action (list load errors live on `list.error`). */
  clearError: string | null;
  severity: string;
  eventType: string;
  searchInput: string;
  setSearchInput: (v: string) => void;
  setSeverity: (v: string) => void;
  setEventType: (v: string) => void;
  submitSearch: () => void;
  clear: () => Promise<void>;
}

const getKey = (e: SystemEventItem): number => e.id;

/** System events, newest first, as a virtualized list: filters reset it, there are no page numbers. */
export function useSystemEvents(): UseSystemEventsReturn {
  const [severity, setSeverity] = useState<string>('all');
  const [eventType, setEventType] = useState<string>('all');
  const [searchInput, setSearchInput] = useState<string>('');
  const [activeSearch, setActiveSearch] = useState<string>('');
  const [isClearing, setIsClearing] = useState<boolean>(false);
  const [clearError, setClearError] = useState<string | null>(null);

  const filters = useMemo(
    () => ({
      severity: severity !== 'all' ? severity : '',
      event_type: eventType !== 'all' ? eventType : '',
      search: activeSearch.trim(),
    }),
    [severity, eventType, activeSearch]
  );

  const fetchPage = useCallback<FetchPage<SystemEventItem>>(async (req) => {
    const res = await getSystemEvents({
      page: req.page,
      page_size: req.pageSize,
      event_type: req.filters.event_type || undefined,
      severity: req.filters.severity || undefined,
      search: req.filters.search || undefined,
    });
    return { items: res.items || [], total: res.total || 0 };
  }, []);

  const list = useVirtualPagedList<SystemEventItem>(fetchPage, {
    pageSize: EVENTS_PAGE_SIZE,
    sortKey: 'created_at',
    sortDir: 'desc',
    filters,
    getKey,
  });
  const { reload } = list;

  const submitSearch = useCallback(() => setActiveSearch(searchInput), [searchInput]);

  const clear = useCallback(async () => {
    setIsClearing(true);
    setClearError(null);
    try {
      await clearSystemEvents();
      reload();
    } catch (err: unknown) {
      setClearError(errorMessage(err, 'Failed to clear system events'));
    } finally {
      setIsClearing(false);
    }
  }, [reload]);

  return {
    list,
    isClearing,
    clearError,
    severity,
    eventType,
    searchInput,
    setSearchInput,
    setSeverity,
    setEventType,
    submitSearch,
    clear,
  };
}
