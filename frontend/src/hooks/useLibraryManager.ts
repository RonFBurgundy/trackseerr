import { useState, useEffect, useCallback } from 'react';
import type { LibraryManagerMode, LibraryManagerState } from '@/types/models';
import { getLibraryManager, setLibraryManager } from '@/services/settingsService';
import { errorMessage } from '@/services/apiClient';

export type SwitchResult = { ok: true; state: LibraryManagerState } | { ok: false; message: string };

export interface UseLibraryManagerReturn {
  state: LibraryManagerState | null;
  isLoading: boolean;
  isSwitching: boolean;
  loadError: string | null;
  refresh: () => Promise<void>;
  /** Never throws: 409/422 server messages come back in the result so the caller can toast them. */
  switchTo: (mode: LibraryManagerMode) => Promise<SwitchResult>;
}

export function useLibraryManager(enabled: boolean): UseLibraryManagerReturn {
  const [state, setState] = useState<LibraryManagerState | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [isSwitching, setIsSwitching] = useState<boolean>(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!enabled) return;
    setIsLoading(true);
    setLoadError(null);
    try {
      setState(await getLibraryManager());
    } catch (err: unknown) {
      setLoadError(errorMessage(err, 'Failed to load library manager status'));
    } finally {
      setIsLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const switchTo = useCallback(async (mode: LibraryManagerMode): Promise<SwitchResult> => {
    setIsSwitching(true);
    try {
      const next = await setLibraryManager(mode);
      setState(next);
      return { ok: true, state: next };
    } catch (err: unknown) {
      return { ok: false, message: errorMessage(err, 'Failed to switch library manager') };
    } finally {
      setIsSwitching(false);
    }
  }, []);

  return { state, isLoading, isSwitching, loadError, refresh, switchTo };
}
