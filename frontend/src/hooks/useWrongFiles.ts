import { useCallback, useEffect, useRef, useState } from 'react';
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

export interface UseWrongFilesReturn {
  items: WrongFileItem[];
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
  startCheck: () => Promise<void>;
  remove: (fileId: string) => void;
  deleteFile: (fileId: string) => Promise<boolean>;
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

  const wasRunning = useRef<boolean>(false);

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
      if (wasRunning.current && !next.running) reload();
      wasRunning.current = next.running;
      setStatus(next);
    },
    [reload]
  );

  useEffect(() => {
    if (!enabled) {
      wasRunning.current = false;
      setStatus(null);
      setCheckError(null);
      setActionError(null);
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

  const remove = useCallback((fileId: string): void => {
    setItems((prev) => prev.filter((item) => item.file_id !== fileId));
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

  return {
    items,
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
    startCheck,
    remove,
    deleteFile,
    reload,
  };
}
