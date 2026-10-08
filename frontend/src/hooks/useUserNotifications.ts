import { useCallback, useEffect, useRef, useState } from 'react';
import type { NotificationInboxResponse } from '@/types/userNotifications';
import {
  deleteNotification as apiDeleteNotification,
  getInbox as apiGetInbox,
  getUnreadCount as apiGetUnreadCount,
  markAllNotificationsRead as apiMarkAllRead,
  markNotificationRead as apiMarkRead,
} from '@/services/userNotificationsService';

export const USER_NOTIFICATIONS_CHANGED_EVENT = 'trackseerr:user-notifications-changed';

export function dispatchUserNotificationsChanged(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(USER_NOTIFICATIONS_CHANGED_EVENT));
  }
}

export interface UseUserNotificationsReturn {
  unreadCount: number;
  inbox: NotificationInboxResponse | null;
  loading: boolean;
  page: number;
  fetchInbox: (page?: number, pageSize?: number) => Promise<void>;
  refreshUnreadCount: () => Promise<void>;
  markRead: (id: string) => Promise<void>;
  markAllRead: () => Promise<void>;
  removeNotification: (id: string) => Promise<void>;
}

export function useUserNotifications(enabled: boolean): UseUserNotificationsReturn {
  const [unreadCount, setUnreadCount] = useState<number>(0);
  const [inbox, setInbox] = useState<NotificationInboxResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [page, setPage] = useState<number>(1);
  const isMountedRef = useRef<boolean>(true);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const refreshUnreadCount = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    try {
      const count = await apiGetUnreadCount();
      if (isMountedRef.current) {
        setUnreadCount(count);
      }
    } catch (err: unknown) {
      // Silently catch count refresh failures to avoid console spam when background polling
      console.debug('Failed to refresh notification unread count:', err);
    }
  }, [enabled]);

  const fetchInbox = useCallback(
    async (targetPage: number = 1, pageSize: number = 20): Promise<void> => {
      if (!enabled) return;
      setLoading(true);
      try {
        const resp = await apiGetInbox(targetPage, pageSize);
        if (isMountedRef.current) {
          setInbox(resp);
          setPage(resp.page);
          setUnreadCount(resp.unread_count);
        }
      } catch (err: unknown) {
        console.warn('Failed to fetch user notification inbox:', err);
      } finally {
        if (isMountedRef.current) {
          setLoading(false);
        }
      }
    },
    [enabled]
  );

  const markRead = useCallback(
    async (id: string): Promise<void> => {
      try {
        await apiMarkRead(id);
        if (isMountedRef.current) {
          setInbox((prev) => {
            if (!prev) return null;
            return {
              ...prev,
              unread_count: Math.max(0, prev.unread_count - 1),
              items: prev.items.map((item) =>
                item.id === id ? { ...item, read_at: new Date().toISOString() } : item
              ),
            };
          });
          setUnreadCount((c) => Math.max(0, c - 1));
        }
        dispatchUserNotificationsChanged();
      } catch (err: unknown) {
        console.warn('Failed to mark notification as read:', err);
      }
    },
    []
  );

  const markAllRead = useCallback(async (): Promise<void> => {
    try {
      await apiMarkAllRead();
      if (isMountedRef.current) {
        const now = new Date().toISOString();
        setInbox((prev) => {
          if (!prev) return null;
          return {
            ...prev,
            unread_count: 0,
            items: prev.items.map((item) => ({ ...item, read_at: item.read_at || now })),
          };
        });
        setUnreadCount(0);
      }
      dispatchUserNotificationsChanged();
    } catch (err: unknown) {
      console.warn('Failed to mark all notifications as read:', err);
    }
  }, []);

  const removeNotification = useCallback(
    async (id: string): Promise<void> => {
      try {
        await apiDeleteNotification(id);
        if (isMountedRef.current) {
          setInbox((prev) => {
            if (!prev) return null;
            const target = prev.items.find((item) => item.id === id);
            const wasUnread = target && !target.read_at;
            return {
              ...prev,
              total: Math.max(0, prev.total - 1),
              unread_count: wasUnread ? Math.max(0, prev.unread_count - 1) : prev.unread_count,
              items: prev.items.filter((item) => item.id !== id),
            };
          });
        }
        void refreshUnreadCount();
        dispatchUserNotificationsChanged();
      } catch (err: unknown) {
        console.warn('Failed to delete notification:', err);
      }
    },
    [refreshUnreadCount]
  );

  // Poll unread-count every 60s and on window focus/visibility
  useEffect(() => {
    if (!enabled) {
      setUnreadCount(0);
      setInbox(null);
      return;
    }

    void refreshUnreadCount();

    const intervalId = window.setInterval(() => {
      void refreshUnreadCount();
    }, 60000);

    const onFocus = (): void => {
      void refreshUnreadCount();
    };

    const onVisibilityChange = (): void => {
      if (document.visibilityState === 'visible') {
        void refreshUnreadCount();
      }
    };

    const onChanged = (): void => {
      void refreshUnreadCount();
    };

    window.addEventListener('focus', onFocus);
    window.addEventListener(USER_NOTIFICATIONS_CHANGED_EVENT, onChanged);
    document.addEventListener('visibilitychange', onVisibilityChange);

    return () => {
      window.clearInterval(intervalId);
      window.removeEventListener('focus', onFocus);
      window.removeEventListener(USER_NOTIFICATIONS_CHANGED_EVENT, onChanged);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [enabled, refreshUnreadCount]);

  return {
    unreadCount,
    inbox,
    loading,
    page,
    fetchInbox,
    refreshUnreadCount,
    markRead,
    markAllRead,
    removeNotification,
  };
}
