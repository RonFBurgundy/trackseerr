import { useState, useEffect, useCallback } from 'react';
import type { SystemEventItem } from '@/types/models';
import { getSystemEvents, clearSystemEvents } from '@/services/systemService';
import { errorMessage } from '@/services/apiClient';

export const EVENTS_PAGE_SIZE = 50;

export interface UseSystemEventsReturn {
  events: SystemEventItem[];
  total: number;
  page: number;
  totalPages: number;
  isLoading: boolean;
  error: string | null;
  isClearing: boolean;
  severity: string;
  eventType: string;
  searchInput: string;
  setSearchInput: (v: string) => void;
  setSeverity: (v: string) => void;
  setEventType: (v: string) => void;
  setPage: React.Dispatch<React.SetStateAction<number>>;
  submitSearch: () => void;
  refresh: () => Promise<void>;
  clear: () => Promise<void>;
}

export function useSystemEvents(): UseSystemEventsReturn {
  const [events, setEvents] = useState<SystemEventItem[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [page, setPage] = useState<number>(1);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [severity, setSeverityState] = useState<string>('all');
  const [eventType, setEventTypeState] = useState<string>('all');
  const [searchInput, setSearchInput] = useState<string>('');
  const [activeSearch, setActiveSearch] = useState<string>('');
  const [isClearing, setIsClearing] = useState<boolean>(false);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await getSystemEvents({
        page,
        page_size: EVENTS_PAGE_SIZE,
        event_type: eventType !== 'all' ? eventType : undefined,
        severity: severity !== 'all' ? severity : undefined,
        search: activeSearch.trim() || undefined,
      });
      setEvents(res.items || []);
      setTotal(res.total || 0);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to fetch system events'));
    } finally {
      setIsLoading(false);
    }
  }, [page, eventType, severity, activeSearch]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const setSeverity = (v: string) => {
    setPage(1);
    setSeverityState(v);
  };
  const setEventType = (v: string) => {
    setPage(1);
    setEventTypeState(v);
  };
  const submitSearch = () => {
    setPage(1);
    setActiveSearch(searchInput);
  };

  const clear = useCallback(async () => {
    setIsClearing(true);
    try {
      await clearSystemEvents();
      setPage(1);
      await refresh();
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to clear system events'));
    } finally {
      setIsClearing(false);
    }
  }, [refresh]);

  return {
    events,
    total,
    page,
    totalPages: Math.max(1, Math.ceil(total / EVENTS_PAGE_SIZE)),
    isLoading,
    error,
    isClearing,
    severity,
    eventType,
    searchInput,
    setSearchInput,
    setSeverity,
    setEventType,
    setPage,
    submitSearch,
    refresh,
    clear,
  };
}
