import { apiRequest } from './apiClient';
import type { CalendarItem } from '@/types/calendar';

export interface CalendarQuery {
  start?: string;
  end?: string;
  unmonitored?: boolean;
}

export function getCalendar(query?: CalendarQuery, signal?: AbortSignal): Promise<CalendarItem[]> {
  const params = new URLSearchParams();
  if (query?.start) params.set('start', query.start);
  if (query?.end) params.set('end', query.end);
  if (query?.unmonitored !== undefined) params.set('unmonitored', String(query.unmonitored));
  const qs = params.toString();
  const url = qs ? `/api/calendar?${qs}` : '/api/calendar';
  return apiRequest<CalendarItem[]>(url, { signal });
}

export function getCalendarFeedUrl(): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : '';
  return `${origin}/api/calendar/feed.ics`;
}
