import { useState, useCallback } from 'react';
import type { LibraryManagerMode } from '@/types/models';
import type { UseLibraryManagerReturn } from '@/hooks/useLibraryManager';

export const MODE_LABEL: Record<LibraryManagerMode, string> = {
  native: 'TrackSeerr',
  lidarr: 'Lidarr',
};

export interface UseLibraryModeSwitchReturn {
  /** The mode awaiting confirmation, or null when no switch is open. */
  pendingMode: LibraryManagerMode | null;
  /** Opens the confirmation for `mode` in place (no navigation). */
  request: (mode: LibraryManagerMode) => void;
  cancel: () => void;
  /** Performs the pending switch through the existing library-manager API and reports via toast. */
  confirm: () => Promise<void>;
}

/** Shared state for the library-manager switch so every page that offers it uses one confirm flow. */
export function useLibraryModeSwitch(
  manager: UseLibraryManagerReturn,
  onToast: (msg: string, tone?: 'ok' | 'error') => void
): UseLibraryModeSwitchReturn {
  const [pendingMode, setPendingMode] = useState<LibraryManagerMode | null>(null);
  const { switchTo, refresh } = manager;

  const request = useCallback((mode: LibraryManagerMode) => setPendingMode(mode), []);
  const cancel = useCallback(() => setPendingMode(null), []);

  const confirm = useCallback(async () => {
    if (!pendingMode) return;
    const target = pendingMode;
    const res = await switchTo(target);
    if (res.ok) {
      onToast(`Library manager is now ${MODE_LABEL[target]}`);
    } else {
      // The server message is the explanation (409 in-flight / 422 unconfigured).
      onToast(res.message, 'error');
      void refresh();
    }
    setPendingMode(null);
  }, [pendingMode, switchTo, refresh, onToast]);

  return { pendingMode, request, cancel, confirm };
}
