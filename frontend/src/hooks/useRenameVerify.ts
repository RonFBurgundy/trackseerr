import { useCallback, useEffect, useRef, useState } from 'react';
import {
  getRenameVerifyStatus,
  startRenameVerify,
  type RenameVerifyStatus,
} from '@/services/renameService';
import { ApiError, errorMessage } from '@/services/apiClient';
import { usePolling } from './usePolling';

const POLL_MS = 1500;

export interface UseRenameVerifyOptions {
  enabled: boolean;
  /** Called once each time a run that was in progress finishes. */
  onFinished: () => void;
}

export interface UseRenameVerifyReturn {
  status: RenameVerifyStatus | null;
  starting: boolean;
  error: string | null;
  start: (fileIds: string[]) => Promise<void>;
}

export function useRenameVerify({ enabled, onFinished }: UseRenameVerifyOptions): UseRenameVerifyReturn {
  const [status, setStatus] = useState<RenameVerifyStatus | null>(null);
  const [starting, setStarting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const wasRunning = useRef<boolean>(false);
  const onFinishedRef = useRef<() => void>(onFinished);
  onFinishedRef.current = onFinished;

  const applyStatus = useCallback((next: RenameVerifyStatus): void => {
    if (wasRunning.current && !next.running) onFinishedRef.current();
    wasRunning.current = next.running;
    setStatus(next);
  }, []);

  useEffect(() => {
    if (!enabled) {
      wasRunning.current = false;
      setStatus(null);
      setError(null);
      return undefined;
    }
    let cancelled = false;
    getRenameVerifyStatus()
      .then((next) => {
        if (!cancelled) applyStatus(next);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorMessage(err, 'Failed to load verification status'));
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, applyStatus]);

  const poll = useCallback((): void => {
    getRenameVerifyStatus()
      .then(applyStatus)
      .catch((err: unknown) => setError(errorMessage(err, 'Failed to read verification progress')));
  }, [applyStatus]);

  usePolling(poll, POLL_MS, enabled && status?.running === true);

  const start = useCallback(
    async (fileIds: string[]): Promise<void> => {
      setStarting(true);
      setError(null);
      try {
        applyStatus(await startRenameVerify(fileIds));
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 409) {
          setError('A verification is already running');
          try {
            applyStatus(await getRenameVerifyStatus());
          } catch (inner: unknown) {
            setError(errorMessage(inner, 'Failed to read verification progress'));
          }
        } else {
          setError(errorMessage(err, 'Failed to start verification'));
        }
      } finally {
        setStarting(false);
      }
    },
    [applyStatus]
  );

  return { status, starting, error, start };
}
