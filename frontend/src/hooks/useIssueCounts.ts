import { useCallback, useEffect, useRef, useState } from 'react';
import { getOpenIssueCount, getUnreadIssueCount, ISSUES_CHANGED_EVENT } from '@/services/issueService';

export interface UseIssueCountsReturn {
  /** Own issues with admin activity not yet seen. */
  unread: number;
  /** Admin only: issues waiting for attention. */
  open: number;
  /** Re-reads both counts now (call after any action that changes an issue). */
  refresh: () => Promise<void>;
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

/**
 * Nav badge counts for issues. There is no interval: the counts load on mount, when the window regains focus or the
 * tab becomes visible, when any issue changes (`ISSUES_CHANGED_EVENT`), and whenever a caller invokes `refresh` after an action.
 */
export function useIssueCounts(enabled: boolean, isAdmin: boolean): UseIssueCountsReturn {
  const [unread, setUnread] = useState<number>(0);
  const [open, setOpen] = useState<number>(0);
  const controllerRef = useRef<AbortController | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const { signal } = controller;
    try {
      const count = await getUnreadIssueCount(signal);
      if (!signal.aborted) setUnread(count);
    } catch (err: unknown) {
      if (!isAbort(err)) console.warn('Unread issue count refresh failed', err);
    }
    if (!isAdmin) return;
    try {
      const counts = await getOpenIssueCount(signal);
      if (!signal.aborted) setOpen(counts.count + (counts.in_progress ?? 0));
    } catch (err: unknown) {
      if (!isAbort(err)) console.warn('Open issue count refresh failed', err);
    }
  }, [enabled, isAdmin]);

  useEffect(() => {
    if (!enabled) {
      setUnread(0);
      setOpen(0);
      return undefined;
    }
    void refresh();
    const onRefresh = (): void => void refresh();
    const onVisible = (): void => {
      if (document.visibilityState === 'visible') void refresh();
    };
    window.addEventListener('focus', onRefresh);
    window.addEventListener(ISSUES_CHANGED_EVENT, onRefresh);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.removeEventListener('focus', onRefresh);
      window.removeEventListener(ISSUES_CHANGED_EVENT, onRefresh);
      document.removeEventListener('visibilitychange', onVisible);
      controllerRef.current?.abort();
    };
  }, [enabled, refresh]);

  return { unread, open, refresh };
}
