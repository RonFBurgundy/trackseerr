import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, errorMessage } from '@/services/apiClient';
import { removeOrphanTorrent, retryFailedCleanup } from '@/services/seedCleanupService';
import {
  deleteLibraryHealthMapping,
  dismissLibraryHealth,
  getLibraryHealth,
  setLibraryHealthMapping,
  setLibraryHealthWeekly,
  startLibraryHealthCheck,
} from '@/services/libraryHealthService';
import type { LibraryHealthDismissScope, LibraryHealthResponse } from '@/types/libraryHealth';
import { usePolling } from './usePolling';

const RUNNING_POLL_MS = 5_000;

export interface UseLibraryHealthOptions {
  /** Called whenever findings may have changed (so the nav badge can refresh). */
  onChanged: () => void;
  onToast: (message: string, tone?: 'ok' | 'error') => void;
}

export interface UseLibraryHealthReturn {
  data: LibraryHealthResponse | null;
  loading: boolean;
  error: string | null;
  /** True while the server reports a check in progress or a start request is in flight. */
  running: boolean;
  busy: boolean;
  refresh: () => Promise<void>;
  checkNow: () => Promise<void>;
  dismiss: (path: string, scope: LibraryHealthDismissScope) => Promise<void>;
  saveMapping: (serverPrefix: string, localPrefix: string) => Promise<void>;
  removeMapping: () => Promise<void>;
  setWeekly: (enabled: boolean) => Promise<void>;
  removeOrphan: (findingId: string, deleteFiles: boolean) => Promise<void>;
  retryFailed: (findingId: string) => Promise<void>;
}

/** Loads /api/library-health, polls every 5 s while a check runs, and wraps the mutating actions. */
export function useLibraryHealth({ onChanged, onToast }: UseLibraryHealthOptions): UseLibraryHealthReturn {
  const [data, setData] = useState<LibraryHealthResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState<boolean>(false);
  const [busy, setBusy] = useState<boolean>(false);
  const controllerRef = useRef<AbortController | null>(null);
  const wasRunningRef = useRef<boolean>(false);
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;
  const onToastRef = useRef(onToast);
  onToastRef.current = onToast;

  const refresh = useCallback(async (): Promise<void> => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      const res = await getLibraryHealth(controller.signal);
      if (controller.signal.aborted) return;
      setData(res);
      setError(null);
      if (wasRunningRef.current && !res.running) onChangedRef.current();
      wasRunningRef.current = res.running;
    } catch (err: unknown) {
      if (controller.signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) return;
      setError(errorMessage(err, 'Failed to load library health'));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return () => controllerRef.current?.abort();
  }, [refresh]);

  const running = (data?.running ?? false) || starting;
  usePolling(() => void refresh(), RUNNING_POLL_MS, data?.running ?? false);

  const checkNow = useCallback(async (): Promise<void> => {
    setStarting(true);
    try {
      await startLibraryHealthCheck();
      onToastRef.current('Library check started');
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 409) onToastRef.current('A check is already running');
      else onToastRef.current(errorMessage(err, 'Failed to start the check'), 'error');
    } finally {
      setStarting(false);
    }
    await refresh();
  }, [refresh]);

  const mutate = useCallback(
    async (action: () => Promise<unknown>, success: string, failure: string): Promise<void> => {
      setBusy(true);
      try {
        await action();
        onToastRef.current(success);
        await refresh();
        onChangedRef.current();
      } catch (err: unknown) {
        onToastRef.current(errorMessage(err, failure), 'error');
      } finally {
        setBusy(false);
      }
    },
    [refresh]
  );

  const dismiss = useCallback(
    (path: string, scope: LibraryHealthDismissScope) =>
      mutate(() => dismissLibraryHealth({ path, scope }), scope === 'folder' ? 'Folder dismissed' : 'File dismissed', 'Failed to dismiss'),
    [mutate]
  );

  const saveMapping = useCallback(
    (serverPrefix: string, localPrefix: string) =>
      mutate(
        () => setLibraryHealthMapping({ server_prefix: serverPrefix.trim(), local_prefix: localPrefix.trim() }),
        'Path mapping saved',
        'Failed to save the path mapping'
      ),
    [mutate]
  );

  const removeMapping = useCallback(
    () => mutate(() => deleteLibraryHealthMapping(), 'Path mapping removed', 'Failed to remove the path mapping'),
    [mutate]
  );

  const setWeekly = useCallback(
    (enabled: boolean) =>
      mutate(
        () => setLibraryHealthWeekly(enabled),
        enabled ? 'Weekly check enabled' : 'Weekly check disabled',
        'Failed to change the weekly check'
      ),
    [mutate]
  );

  const removeOrphan = useCallback(
    async (findingId: string, deleteFiles: boolean): Promise<void> => {
      setBusy(true);
      try {
        await removeOrphanTorrent(findingId, { delete_files: deleteFiles });
        onToastRef.current(deleteFiles ? 'Torrent and files removed' : 'Torrent removed');
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 404) onToastRef.current('That torrent is already gone; refreshing', 'error');
        else onToastRef.current(errorMessage(err, 'Failed to remove the torrent'), 'error');
      } finally {
        await refresh();
        onChangedRef.current();
        setBusy(false);
      }
    },
    [refresh]
  );

  const retryFailed = useCallback(
    async (findingId: string): Promise<void> => {
      setBusy(true);
      try {
        const res = await retryFailedCleanup(findingId);
        if (res.removed) onToastRef.current('Cleanup retried; torrent removed');
        else if (res.error) onToastRef.current(`Retry failed (attempt ${res.attempts}): ${res.error}`, 'error');
        else onToastRef.current(`Cleanup retried: ${res.status}`);
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 404) onToastRef.current('That item is already gone; refreshing', 'error');
        else onToastRef.current(errorMessage(err, 'Failed to retry cleanup'), 'error');
      } finally {
        await refresh();
        onChangedRef.current();
        setBusy(false);
      }
    },
    [refresh]
  );

  return {
    data,
    loading,
    error,
    running,
    busy,
    refresh,
    checkNow,
    dismiss,
    saveMapping,
    removeMapping,
    setWeekly,
    removeOrphan,
    retryFailed,
  };
}
