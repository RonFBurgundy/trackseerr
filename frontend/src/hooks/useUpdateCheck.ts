import { useState, useEffect, useCallback, useRef } from 'react';
import type { SystemUpdateInfo } from '@/types/models';
import { getSystemUpdate, updateSystemUpdateSettings, triggerScheduledTask } from '@/services/systemService';
import { errorMessage } from '@/services/apiClient';

export interface UseUpdateCheckReturn {
  update: SystemUpdateInfo | null;
  isLoading: boolean;
  isChecking: boolean;
  isSaving: boolean;
  error: string | null;
  updateAvailable: boolean;
  refresh: () => Promise<void>;
  checkNow: () => Promise<void>;
  setEnabled: (enabled: boolean) => Promise<void>;
}

export function useUpdateCheck(enabled: boolean = true): UseUpdateCheckReturn {
  const [update, setUpdate] = useState<SystemUpdateInfo | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isChecking, setIsChecking] = useState<boolean>(false);
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef<boolean>(true);

  const refresh = useCallback(async () => {
    if (!enabled) {
      setIsLoading(false);
      return;
    }
    try {
      const data = await getSystemUpdate();
      if (mountedRef.current) {
        setUpdate(data);
        setError(null);
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(errorMessage(err, 'Failed to fetch update status'));
      }
    } finally {
      if (mountedRef.current) {
        setIsLoading(false);
      }
    }
  }, [enabled]);

  const checkNow = useCallback(async () => {
    setIsChecking(true);
    try {
      await triggerScheduledTask('update_check');
      // Brief delay to allow async task to complete on backend, then fetch status
      await new Promise((resolve) => setTimeout(resolve, 800));
      await refresh();
    } catch (err) {
      if (mountedRef.current) {
        setError(errorMessage(err, 'Failed to run update check'));
      }
    } finally {
      if (mountedRef.current) {
        setIsChecking(false);
      }
    }
  }, [refresh]);

  const setEnabled = useCallback(async (nextEnabled: boolean) => {
    setIsSaving(true);
    try {
      const updated = await updateSystemUpdateSettings(nextEnabled);
      if (mountedRef.current) {
        setUpdate(updated);
        setError(null);
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(errorMessage(err, 'Failed to update setting'));
      }
    } finally {
      if (mountedRef.current) {
        setIsSaving(false);
      }
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    if (enabled) {
      void refresh();
    } else {
      setIsLoading(false);
    }
    return () => {
      mountedRef.current = false;
    };
  }, [enabled, refresh]);

  const updateAvailable = Boolean(update?.update_available);

  return {
    update,
    isLoading,
    isChecking,
    isSaving,
    error,
    updateAvailable,
    refresh,
    checkNow,
    setEnabled,
  };
}
