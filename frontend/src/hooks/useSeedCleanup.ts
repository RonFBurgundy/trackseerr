import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, errorMessage } from '@/services/apiClient';
import { getSeedCleanupStatus, runSeedCleanup } from '@/services/seedCleanupService';
import type { SeedCleanupStatus } from '@/types/seedCleanup';
import { usePolling } from './usePolling';

const RUNNING_POLL_MS = 5_000;

export interface UseSeedCleanupOptions {
  /** Called when a run finishes so findings and the nav badge can refresh. */
  onFinished: () => void;
  onToast: (message: string, tone?: 'ok' | 'error') => void;
}

export interface UseSeedCleanupReturn {
  status: SeedCleanupStatus | null;
  running: boolean;
  run: () => Promise<void>;
}

/** Loads /api/seed-cleanup/status, polls every 5 s only while a run is active, and starts runs. */
export function useSeedCleanup({ onFinished, onToast }: UseSeedCleanupOptions): UseSeedCleanupReturn {
  const [status, setStatus] = useState<SeedCleanupStatus | null>(null);
  const [starting, setStarting] = useState<boolean>(false);
  const controllerRef = useRef<AbortController | null>(null);
  const wasRunningRef = useRef<boolean>(false);
  const onFinishedRef = useRef(onFinished);
  onFinishedRef.current = onFinished;
  const onToastRef = useRef(onToast);
  onToastRef.current = onToast;

  const refresh = useCallback(async (): Promise<void> => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      const res = await getSeedCleanupStatus(controller.signal);
      if (controller.signal.aborted) return;
      setStatus(res);
      if (wasRunningRef.current && !res.running) {
        const err = res.last_run?.error;
        if (err) onToastRef.current(`Seed cleanup failed: ${err}`, 'error');
        else onToastRef.current('Seed cleanup finished');
        onFinishedRef.current();
      }
      wasRunningRef.current = res.running;
    } catch (err: unknown) {
      if (controller.signal.aborted || (err instanceof DOMException && err.name === 'AbortError')) return;
      console.warn('Seed cleanup status failed', err);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return () => controllerRef.current?.abort();
  }, [refresh]);

  usePolling(() => void refresh(), RUNNING_POLL_MS, status?.running ?? false);

  const run = useCallback(async (): Promise<void> => {
    setStarting(true);
    try {
      await runSeedCleanup();
      wasRunningRef.current = true;
      onToastRef.current('Seed cleanup started');
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 409) onToastRef.current('Seed cleanup is already running');
      else onToastRef.current(errorMessage(err, 'Failed to start seed cleanup'), 'error');
    } finally {
      setStarting(false);
    }
    await refresh();
  }, [refresh]);

  return { status, running: (status?.running ?? false) || starting, run };
}
