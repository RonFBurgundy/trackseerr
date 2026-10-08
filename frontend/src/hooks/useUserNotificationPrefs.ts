import { useCallback, useEffect, useState } from 'react';
import type { NotificationPrefItem } from '@/types/userNotifications';
import { USER_NOTIFICATION_EVENTS } from '@/types/userNotifications';
import {
  getUserPrefs,
  updateUserPref,
} from '@/services/userNotificationsService';
import { errorMessage } from '@/services/apiClient';

export interface UseUserNotificationPrefsReturn {
  prefs: NotificationPrefItem[];
  loading: boolean;
  savingKey: string | null;
  error: string | null;
  refresh: () => Promise<void>;
  togglePref: (event: string, kind: 'in_app' | 'push') => Promise<void>;
}

export function useUserNotificationPrefs(
  enabled: boolean,
  onToast?: (msg: string, tone?: 'ok' | 'error') => void
): UseUserNotificationPrefsReturn {
  const [prefs, setPrefs] = useState<NotificationPrefItem[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const normalizePrefs = useCallback((stored: NotificationPrefItem[]): NotificationPrefItem[] => {
    const map = new Map<string, NotificationPrefItem>();
    for (const p of stored) {
      map.set(p.event, p);
    }
    return USER_NOTIFICATION_EVENTS.map((e) => {
      const existing = map.get(e.id);
      return {
        event: e.id,
        in_app: existing ? existing.in_app : true,
        push: existing ? existing.push : true,
      };
    });
  }, []);

  const refresh = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    try {
      const data = await getUserPrefs();
      setPrefs(normalizePrefs(data));
      setError(null);
    } catch (err: unknown) {
      const msg = errorMessage(err, 'Failed to load notification preferences');
      setError(msg);
      onToast?.(msg, 'error');
    }
  }, [enabled, normalizePrefs, onToast]);

  useEffect(() => {
    if (!enabled) {
      setPrefs([]);
      return;
    }
    let cancelled = false;
    setLoading(true);
    getUserPrefs()
      .then((data) => {
        if (!cancelled) {
          setPrefs(normalizePrefs(data));
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const msg = errorMessage(err, 'Failed to load notification preferences');
          setError(msg);
          onToast?.(msg, 'error');
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [enabled, normalizePrefs, onToast]);

  const togglePref = useCallback(
    async (event: string, kind: 'in_app' | 'push'): Promise<void> => {
      const current = prefs.find((p) => p.event === event);
      if (!current) return;

      const nextVal = !current[kind];
      const updatedItem: NotificationPrefItem = {
        ...current,
        [kind]: nextVal,
      };

      // Optimistic update
      setPrefs((prev) => prev.map((p) => (p.event === event ? updatedItem : p)));
      setSavingKey(`${event}:${kind}`);

      try {
        await updateUserPref(event, {
          in_app: updatedItem.in_app,
          push: updatedItem.push,
        });
      } catch (err: unknown) {
        // Rollback
        setPrefs((prev) => prev.map((p) => (p.event === event ? current : p)));
        const msg = errorMessage(err, 'Failed to update preference');
        setError(msg);
        onToast?.(msg, 'error');
      } finally {
        setSavingKey(null);
      }
    },
    [prefs, onToast]
  );

  return {
    prefs,
    loading,
    savingKey,
    error,
    refresh,
    togglePref,
  };
}
