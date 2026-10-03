import { useCallback, useEffect, useState } from 'react';
import type { RoleChangeNotice } from '@/types/deployment';
import { dismissRoleChangeNotice, getRoleChangeNotice } from '@/services/deploymentService';
import { errorMessage } from '@/services/apiClient';

export interface UseRoleChangeNoticeReturn {
  notice: RoleChangeNotice | null;
  isDismissing: boolean;
  error: string | null;
  dismiss: () => Promise<void>;
}

/** Loads the one-time role-change checklist (admin only); `notice` is null when nothing to show. */
export function useRoleChangeNotice(enabled: boolean): UseRoleChangeNoticeReturn {
  const [notice, setNotice] = useState<RoleChangeNotice | null>(null);
  const [isDismissing, setIsDismissing] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) {
      setNotice(null);
      return;
    }
    let cancelled = false;
    getRoleChangeNotice()
      .then((n) => {
        if (!cancelled) setNotice(n.active ? n : null);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorMessage(err, 'Failed to load role change notice'));
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  const dismiss = useCallback(async () => {
    setIsDismissing(true);
    setError(null);
    try {
      const next = await dismissRoleChangeNotice();
      setNotice(next.active ? next : null);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to dismiss notice'));
    } finally {
      setIsDismissing(false);
    }
  }, []);

  return { notice, isDismissing, error, dismiss };
}
