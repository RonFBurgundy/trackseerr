import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { WrongFileItem } from '@/types/models';
import type { RenameVerifyStatus } from '@/services/renameService';
import {
  deleteLibraryFile,
  getWrongFileCheckStatus,
  getWrongFiles,
  startWrongFileCheck,
} from '@/services/libraryService';
import { ApiError, errorMessage } from '@/services/apiClient';
import { usePolling } from './usePolling';

const POLL_MS = 1500;
const MISMATCHED_RELOAD_THROTTLE_MS = 30_000;

export type WrongFileKind = WrongFileItem['kind'];

export interface UseWrongFilesReturn {
  items: WrongFileItem[];
  filteredItems: WrongFileItem[];
  kindCounts: Record<WrongFileKind, number>;
  activeKinds: Set<WrongFileKind>;
  toggleKind: (kind: WrongFileKind) => void;
  clearKinds: () => void;
  total: number;
  checked: number;
  libraryFiles: number;
  isLoading: boolean;
  error: string | null;
  status: RenameVerifyStatus | null;
  starting: boolean;
  checkError: string | null;
  busyFileId: string | null;
  actionError: string | null;
  selectedIds: Set<string>;
  toggleSelect: (fileId: string) => void;
  selectAll: () => void;
  selectSafe: () => void;
  selectNone: () => void;
  startCheck: () => Promise<void>;
  remove: (fileId: string) => void;
  deleteFile: (fileId: string) => Promise<boolean>;
  deleteFiles: (ids: string[]) => Promise<{ deleted: number; failed: string[] }>;
  reload: () => void;
}

/** Loads mismatched-audio files and manages library-wide AcoustID checking while `enabled`. */
export function useWrongFiles(enabled: boolean): UseWrongFilesReturn {
  const [items, setItems] = useState<WrongFileItem[]>([]);
  const [checked, setChecked] = useState<number>(0);
  const [libraryFiles, setLibraryFiles] = useState<number>(0);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState<number>(0);

  const [status, setStatus] = useState<RenameVerifyStatus | null>(null);
  const [starting, setStarting] = useState<boolean>(false);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [busyFileId, setBusyFileId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [activeKinds, setActiveKinds] = useState<Set<WrongFileKind>>(
    () => new Set<WrongFileKind>(['different_song'])
  );

  const wasRunning = useRef<boolean>(false);
  const lastMismatchedRef = useRef<number>(0);
  const lastMismatchedReloadAtRef = useRef<number>(0);

  const reload = useCallback((): void => setReloadTick((n) => n + 1), []);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setIsLoading(true);
    setError(null);
    getWrongFiles(controller.signal)
      .then((res) => {
        if (controller.signal.aborted) return;
        setItems(res.items);
        setChecked(res.checked);
        setLibraryFiles(res.library_files);
        const validIds = new Set(res.items.map((item) => item.file_id));
        setSelectedIds((prev) => {
          let changed = false;
          const next = new Set<string>();
          for (const id of prev) {
            if (validIds.has(id)) next.add(id);
            else changed = true;
          }
          return changed ? next : prev;
        });
        setIsLoading(false);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(errorMessage(err, 'Could not load wrong files'));
        setIsLoading(false);
      });
    return () => controller.abort();
  }, [enabled, reloadTick]);

  const applyStatus = useCallback(
    (next: RenameVerifyStatus): void => {
      if (wasRunning.current && !next.running) {
        lastMismatchedRef.current = 0;
        lastMismatchedReloadAtRef.current = 0;
        reload();
      } else if (next.running) {
        if (!wasRunning.current) {
          lastMismatchedRef.current = next.mismatched;
          lastMismatchedReloadAtRef.current = Date.now();
        } else if (next.mismatched > lastMismatchedRef.current) {
          const now = Date.now();
          if (now - lastMismatchedReloadAtRef.current >= MISMATCHED_RELOAD_THROTTLE_MS) {
            lastMismatchedRef.current = next.mismatched;
            lastMismatchedReloadAtRef.current = now;
            reload();
          }
        }
      }
      wasRunning.current = next.running;
      setStatus(next);
    },
    [reload]
  );

  useEffect(() => {
    if (!enabled) {
      wasRunning.current = false;
      lastMismatchedRef.current = 0;
      lastMismatchedReloadAtRef.current = 0;
      setStatus(null);
      setCheckError(null);
      setActionError(null);
      setSelectedIds(new Set());
      setActiveKinds(new Set<WrongFileKind>(['different_song']));
      return undefined;
    }
    let cancelled = false;
    getWrongFileCheckStatus()
      .then((next) => {
        if (!cancelled) applyStatus(next);
      })
      .catch((err: unknown) => {
        if (!cancelled) setCheckError(errorMessage(err, 'Failed to load check status'));
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, applyStatus]);

  const poll = useCallback((): void => {
    getWrongFileCheckStatus()
      .then(applyStatus)
      .catch((err: unknown) => setCheckError(errorMessage(err, 'Failed to read check progress')));
  }, [applyStatus]);

  usePolling(poll, POLL_MS, enabled && status?.running === true);

  const startCheck = useCallback(async (): Promise<void> => {
    setStarting(true);
    setCheckError(null);
    try {
      applyStatus(await startWrongFileCheck());
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 409) {
        setCheckError('A check is already running');
        try {
          applyStatus(await getWrongFileCheckStatus());
        } catch (inner: unknown) {
          setCheckError(errorMessage(inner, 'Failed to read check progress'));
        }
      } else {
        setCheckError(errorMessage(err, 'Failed to start check'));
      }
    } finally {
      setStarting(false);
    }
  }, [applyStatus]);

  const kindCounts = useMemo<Record<WrongFileKind, number>>(() => {
    const counts: Record<WrongFileKind, number> = {
      different_song: 0,
      different_version: 0,
    };
    for (const item of items) {
      counts[item.kind] += 1;
    }
    return counts;
  }, [items]);

  const filteredItems = useMemo(
    () => (activeKinds.size === 0 ? items : items.filter((item) => activeKinds.has(item.kind))),
    [items, activeKinds]
  );

  const toggleKind = useCallback((kind: WrongFileKind): void => {
    setActiveKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  }, []);

  const clearKinds = useCallback((): void => {
    setActiveKinds(new Set());
  }, []);

  const toggleSelect = useCallback((fileId: string): void => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(fileId)) next.delete(fileId);
      else next.add(fileId);
      return next;
    });
  }, []);

  const selectAll = useCallback((): void => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const item of filteredItems) next.add(item.file_id);
      return next;
    });
  }, [filteredItems]);

  const selectSafe = useCallback((): void => {
    setSelectedIds(
      new Set(items.filter((item) => item.safe).map((item) => item.file_id))
    );
  }, [items]);

  const selectNone = useCallback((): void => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const item of filteredItems) next.delete(item.file_id);
      return next;
    });
  }, [filteredItems]);

  const remove = useCallback((fileId: string): void => {
    setItems((prev) => prev.filter((item) => item.file_id !== fileId));
    setSelectedIds((prev) => {
      if (!prev.has(fileId)) return prev;
      const next = new Set(prev);
      next.delete(fileId);
      return next;
    });
  }, []);

  const deleteFile = useCallback(
    async (fileId: string): Promise<boolean> => {
      setBusyFileId(fileId);
      setActionError(null);
      try {
        await deleteLibraryFile(fileId);
        remove(fileId);
        return true;
      } catch (err: unknown) {
        setActionError(errorMessage(err, 'Could not delete file'));
        return false;
      } finally {
        setBusyFileId(null);
      }
    },
    [remove]
  );

  const deleteFiles = useCallback(
    async (ids: string[]): Promise<{ deleted: number; failed: string[] }> => {
      let deleted = 0;
      const failed: string[] = [];
      for (const id of ids) {
        const ok = await deleteFile(id);
        if (ok) deleted += 1;
        else failed.push(id);
      }
      return { deleted, failed };
    },
    [deleteFile]
  );

  return {
    items,
    filteredItems,
    kindCounts,
    activeKinds,
    toggleKind,
    clearKinds,
    total: items.length,
    checked,
    libraryFiles,
    isLoading,
    error,
    status,
    starting,
    checkError,
    busyFileId,
    actionError,
    selectedIds,
    toggleSelect,
    selectAll,
    selectSafe,
    selectNone,
    startCheck,
    remove,
    deleteFile,
    deleteFiles,
    reload,
  };
}
