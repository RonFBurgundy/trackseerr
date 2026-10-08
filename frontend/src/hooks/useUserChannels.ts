import { useCallback, useEffect, useState } from 'react';
import type { UserNotificationChannelItem } from '@/types/userNotifications';
import {
  deleteUserChannel,
  getUserChannels,
  testUserChannel,
  updateUserChannel,
} from '@/services/userNotificationsService';
import { errorMessage } from '@/services/apiClient';

export type UserNotificationToast = (msg: string, tone?: 'ok' | 'error') => void;

export interface UseUserChannelsReturn {
  channels: UserNotificationChannelItem[];
  loading: boolean;
  testingIds: ReadonlySet<string>;
  refresh: () => Promise<void>;
  setEnabled: (channel: UserNotificationChannelItem, enabled: boolean) => Promise<void>;
  remove: (id: string) => Promise<void>;
  test: (channel: UserNotificationChannelItem) => Promise<void>;
}

export function useUserChannels(
  enabled: boolean,
  onToast: UserNotificationToast
): UseUserChannelsReturn {
  const [channels, setChannels] = useState<UserNotificationChannelItem[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [testingIds, setTestingIds] = useState<ReadonlySet<string>>(new Set<string>());

  const refresh = useCallback(async (): Promise<void> => {
    try {
      setChannels(await getUserChannels());
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to load notification channels'), 'error');
    }
  }, [onToast]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setLoading(true);
    getUserChannels()
      .then((list) => {
        if (!cancelled) setChannels(list);
      })
      .catch((err: unknown) => {
        if (!cancelled) onToast(errorMessage(err, 'Failed to load notification channels'), 'error');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, onToast]);

  const setEnabled = useCallback(
    async (channel: UserNotificationChannelItem, value: boolean): Promise<void> => {
      setChannels((prev) => prev.map((c) => (c.id === channel.id ? { ...c, enabled: value } : c)));
      try {
        await updateUserChannel(channel.id, {
          name: channel.name,
          channel_type: channel.channel_type,
          enabled: value,
          config: {},
          events: channel.events ?? [],
        });
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to update channel'), 'error');
        await refresh();
      }
    },
    [onToast, refresh]
  );

  const remove = useCallback(
    async (id: string): Promise<void> => {
      try {
        await deleteUserChannel(id);
        setChannels((prev) => prev.filter((c) => c.id !== id));
        onToast('Channel deleted');
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to delete channel'), 'error');
      }
    },
    [onToast]
  );

  const test = useCallback(
    async (channel: UserNotificationChannelItem): Promise<void> => {
      setTestingIds((prev) => new Set(prev).add(channel.id));
      try {
        const res = await testUserChannel({
          channel_id: channel.id,
          channel_type: channel.channel_type,
          config: {},
        });
        onToast(res.message, res.success ? 'ok' : 'error');
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Test failed'), 'error');
      } finally {
        setTestingIds((prev) => {
          const next = new Set(prev);
          next.delete(channel.id);
          return next;
        });
      }
    },
    [onToast]
  );

  return {
    channels,
    loading,
    testingIds,
    refresh,
    setEnabled,
    remove,
    test,
  };
}
