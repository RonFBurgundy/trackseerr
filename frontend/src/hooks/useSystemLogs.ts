import { useState, useEffect, useMemo, useCallback } from 'react';
import type { SystemLogItem } from '@/types/models';
import { getSystemLogs, clearSystemLogs, getSystemLogDownloadUrl, getSystemEvents, type SystemEventItem } from '@/services/systemService';
import { getAuthToken, errorMessage } from '@/services/apiClient';

const MAX_LOG_LINES = 1000;
const EVENT_POLL_MS = 15000;
const EVENT_FETCH_SIZE = 200;

const pad2 = (n: number): string => String(n).padStart(2, '0');

/** Events are stored as UTC; log lines are local time. Render the event in local time so the two interleave. */
function eventTimestamp(createdAt: string | null | undefined): string {
  if (!createdAt) return '';
  const parsed = new Date(`${createdAt.replace(' ', 'T')}${/[zZ]|[+-]\d\d:?\d\d$/.test(createdAt) ? '' : 'Z'}`);
  if (Number.isNaN(parsed.getTime())) return createdAt;
  return (
    `${parsed.getFullYear()}-${pad2(parsed.getMonth() + 1)}-${pad2(parsed.getDate())} ` +
    `${pad2(parsed.getHours())}:${pad2(parsed.getMinutes())}:${pad2(parsed.getSeconds())}`
  );
}

function eventLevel(severity: string | null | undefined): string {
  const s = (severity || 'info').toLowerCase();
  if (s === 'error') return 'ERROR';
  if (s === 'warn' || s === 'warning') return 'WARNING';
  return 'INFO';
}

/** A lifecycle event shaped as a log row so it renders and filters like one. */
export function eventToLogItem(e: SystemEventItem): SystemLogItem {
  return {
    id: `event-${e.id}`,
    timestamp: eventTimestamp(e.created_at),
    level: eventLevel(e.severity),
    name: `event:${e.event_type}`,
    message: e.source ? `[${e.source}] ${e.message}` : e.message,
  };
}

export interface UseSystemLogsReturn {
  logs: SystemLogItem[];
  filteredLogs: SystemLogItem[];
  isConnected: boolean;
  levelFilter: string;
  setLevelFilter: (v: string) => void;
  searchTerm: string;
  setSearchTerm: (v: string) => void;
  /** Merge lifecycle events (scan/download/sync/library events) into the log view. */
  showEvents: boolean;
  setShowEvents: (v: boolean) => void;
  isClearing: boolean;
  isDownloading: boolean;
  actionError: string | null;
  clear: () => Promise<void>;
  download: () => Promise<void>;
}

/** Initial snapshot plus a live SSE stream; the stream is closed when the consumer unmounts. */
export function useSystemLogs(): UseSystemLogsReturn {
  const [logs, setLogs] = useState<SystemLogItem[]>([]);
  const [isConnected, setIsConnected] = useState<boolean>(false);
  const [levelFilter, setLevelFilter] = useState<string>('all');
  const [searchTerm, setSearchTerm] = useState<string>('');
  const [showEvents, setShowEvents] = useState<boolean>(false);
  const [events, setEvents] = useState<SystemLogItem[]>([]);
  const [isClearing, setIsClearing] = useState<boolean>(false);
  const [isDownloading, setIsDownloading] = useState<boolean>(false);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    let subscribed = true;
    getSystemLogs({ limit: 300 })
      .then((initial) => {
        if (subscribed && initial) setLogs(initial);
      })
      .catch((err: unknown) => {
        console.warn('Initial system logs load failed:', err);
      });

    // Same-origin EventSource sends the HttpOnly session cookie; no token goes in the URL (proxy/access logs).
    const es = new EventSource('/api/system/logs/stream', { withCredentials: true });

    es.onopen = () => {
      if (subscribed) setIsConnected(true);
    };
    es.onmessage = (event: MessageEvent<string>) => {
      if (!subscribed) return;
      try {
        const item = JSON.parse(event.data) as SystemLogItem;
        if (item && item.message) {
          setLogs((prev) => {
            const next = [...prev, item];
            return next.length > MAX_LOG_LINES ? next.slice(next.length - MAX_LOG_LINES) : next;
          });
        }
      } catch {
        // Keep-alive ping or non-JSON frame: nothing to render.
      }
    };
    es.onerror = () => {
      if (subscribed) setIsConnected(false);
    };

    return () => {
      subscribed = false;
      es.close();
      setIsConnected(false);
    };
  }, []);

  useEffect(() => {
    if (!showEvents) {
      setEvents([]);
      return undefined;
    }
    let active = true;
    const load = (): void => {
      getSystemEvents(EVENT_FETCH_SIZE)
        .then((items) => {
          if (active) setEvents(items.map(eventToLogItem));
        })
        .catch((err: unknown) => {
          console.warn('System events load failed:', err);
        });
    };
    load();
    const timer = window.setInterval(load, EVENT_POLL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [showEvents]);

  const clear = useCallback(async () => {
    setIsClearing(true);
    setActionError(null);
    try {
      await clearSystemLogs();
    } catch (err: unknown) {
      console.warn('Failed to clear logs on server, clearing locally:', err);
    } finally {
      setLogs([]);
      setIsClearing(false);
    }
  }, []);

  const download = useCallback(async () => {
    setIsDownloading(true);
    setActionError(null);
    try {
      const token = getAuthToken();
      const headers: Record<string, string> = {};
      if (token) headers['Authorization'] = `Bearer ${token}`;
      const res = await fetch(getSystemLogDownloadUrl(), { headers });
      if (!res.ok) throw new Error(`Download failed with status ${res.status}`);
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'trackseerr.txt';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      window.URL.revokeObjectURL(url);
    } catch (err: unknown) {
      setActionError(errorMessage(err, 'Failed to download logs'));
    } finally {
      setIsDownloading(false);
    }
  }, []);

  const merged = useMemo(() => {
    if (!showEvents || events.length === 0) return logs;
    return [...logs, ...events].sort((a, b) => (a.timestamp < b.timestamp ? -1 : a.timestamp > b.timestamp ? 1 : 0));
  }, [logs, events, showEvents]);

  const filteredLogs = useMemo(() => {
    return merged.filter((log) => {
      if (levelFilter !== 'all') {
        const lvl = (log.level || '').toLowerCase();
        const target = levelFilter.toLowerCase();
        if (target === 'warning' && lvl !== 'warn' && lvl !== 'warning') return false;
        if (target !== 'warning' && lvl !== target) return false;
      }
      if (searchTerm.trim()) {
        const s = searchTerm.toLowerCase();
        return (log.message || '').toLowerCase().includes(s) || (log.name || '').toLowerCase().includes(s);
      }
      return true;
    });
  }, [merged, levelFilter, searchTerm]);

  return {
    logs,
    filteredLogs,
    isConnected,
    levelFilter,
    setLevelFilter,
    searchTerm,
    setSearchTerm,
    showEvents,
    setShowEvents,
    isClearing,
    isDownloading,
    actionError,
    clear,
    download,
  };
}
