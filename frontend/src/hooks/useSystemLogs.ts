import { useState, useEffect, useMemo, useCallback } from 'react';
import type { SystemLogItem } from '@/types/models';
import { getSystemLogs, clearSystemLogs, getSystemLogDownloadUrl } from '@/services/systemService';
import { getAuthToken, errorMessage } from '@/services/apiClient';

const MAX_LOG_LINES = 1000;

export interface UseSystemLogsReturn {
  logs: SystemLogItem[];
  filteredLogs: SystemLogItem[];
  isConnected: boolean;
  levelFilter: string;
  setLevelFilter: (v: string) => void;
  searchTerm: string;
  setSearchTerm: (v: string) => void;
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
      a.download = 'trackseerr.log';
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

  const filteredLogs = useMemo(() => {
    return logs.filter((log) => {
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
  }, [logs, levelFilter, searchTerm]);

  return {
    logs,
    filteredLogs,
    isConnected,
    levelFilter,
    setLevelFilter,
    searchTerm,
    setSearchTerm,
    isClearing,
    isDownloading,
    actionError,
    clear,
    download,
  };
}
