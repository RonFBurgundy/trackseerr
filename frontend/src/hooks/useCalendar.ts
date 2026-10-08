import { useCallback, useEffect, useMemo, useState } from 'react';
import type { CalendarItem } from '@/types/calendar';
import { getCalendar } from '@/services/calendarService';

export interface UseCalendarOptions {
  enabled?: boolean;
}

export interface UseCalendarReturn {
  currentDate: Date;
  year: number;
  month: number;
  monthLabel: string;
  items: CalendarItem[];
  itemsByDate: Map<string, CalendarItem[]>;
  isLoading: boolean;
  error: string | null;
  showUnmonitored: boolean;
  startDate: string;
  endDate: string;
  goToPrevMonth: () => void;
  goToNextMonth: () => void;
  goToToday: () => void;
  setShowUnmonitored: (value: boolean) => void;
  toggleUnmonitored: () => void;
  refetch: () => Promise<void>;
}

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

export function toDateKey(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

export function getMonthGridRange(year: number, month: number): { start: Date; end: Date; startStr: string; endStr: string } {
  // First day of month
  const first = new Date(year, month, 1);
  // Last day of month
  const last = new Date(year, month + 1, 0);

  // Sunday start: 0 (Sun) .. 6 (Sat)
  const startDay = first.getDay();
  const start = new Date(year, month, 1 - startDay);

  const endDay = last.getDay();
  const end = new Date(year, month + 1, 0 + (6 - endDay));

  return {
    start,
    end,
    startStr: toDateKey(start),
    endStr: toDateKey(end),
  };
}

export function useCalendar(options: UseCalendarOptions = {}): UseCalendarReturn {
  const { enabled = true } = options;
  const [currentDate, setCurrentDate] = useState<Date>(() => new Date());
  const [showUnmonitored, setShowUnmonitored] = useState<boolean>(false);
  const [items, setItems] = useState<CalendarItem[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const year = currentDate.getFullYear();
  const month = currentDate.getMonth();

  const monthLabel = useMemo(() => {
    return currentDate.toLocaleString('default', { month: 'long', year: 'numeric' });
  }, [currentDate]);

  const { startStr, endStr } = useMemo(() => {
    return getMonthGridRange(year, month);
  }, [year, month]);

  const fetchItems = useCallback(
    async (signal?: AbortSignal) => {
      if (!enabled) return;
      setIsLoading(true);
      setError(null);
      try {
        const data = await getCalendar(
          {
            start: startStr,
            end: endStr,
            unmonitored: showUnmonitored,
          },
          signal
        );
        if (!signal?.aborted) {
          setItems(data);
        }
      } catch (err: unknown) {
        if (!signal?.aborted) {
          const msg = err instanceof Error ? err.message : 'Failed to load calendar';
          setError(msg);
        }
      } finally {
        if (!signal?.aborted) {
          setIsLoading(false);
        }
      }
    },
    [enabled, startStr, endStr, showUnmonitored]
  );

  useEffect(() => {
    const controller = new AbortController();
    void fetchItems(controller.signal);
    return () => {
      controller.abort();
    };
  }, [fetchItems]);

  const itemsByDate = useMemo(() => {
    const map = new Map<string, CalendarItem[]>();
    for (const item of items) {
      const key = item.release_date;
      const list = map.get(key);
      if (list) {
        list.push(item);
      } else {
        map.set(key, [item]);
      }
    }
    return map;
  }, [items]);

  const goToPrevMonth = useCallback(() => {
    setCurrentDate((d) => new Date(d.getFullYear(), d.getMonth() - 1, 1));
  }, []);

  const goToNextMonth = useCallback(() => {
    setCurrentDate((d) => new Date(d.getFullYear(), d.getMonth() + 1, 1));
  }, []);

  const goToToday = useCallback(() => {
    setCurrentDate(new Date());
  }, []);

  const toggleUnmonitored = useCallback(() => {
    setShowUnmonitored((prev) => !prev);
  }, []);

  const refetch = useCallback(async () => {
    await fetchItems();
  }, [fetchItems]);

  return {
    currentDate,
    year,
    month,
    monthLabel,
    items,
    itemsByDate,
    isLoading,
    error,
    showUnmonitored,
    startDate: startStr,
    endDate: endStr,
    goToPrevMonth,
    goToNextMonth,
    goToToday,
    setShowUnmonitored,
    toggleUnmonitored,
    refetch,
  };
}
