import { useCallback, useEffect, useState } from 'react';
import type { NotificationChannel } from '@/types/notifications';
import {
  getNotificationChannels,
  updateNotificationChannel,
  deleteNotificationChannel,
  testNotificationChannel,
} from '@/services/notificationsService';
import { errorMessage } from '@/services/apiClient';

export type NotificationToast = (msg: string, tone?: 'ok' | 'error') => void;

export interface UseNotificationChannelsReturn {
  channels: NotificationChannel[];
  loading: boolean;
  testingIds: ReadonlySet<string>;
  refresh: () => Promise<void>;
  /** Toggles a channel. Sends no config, so the server keeps every stored secret. */
  setEnabled: (channel: NotificationChannel, enabled: boolean) => Promise<void>;
  remove: (id: string) => Promise<void>;
  /** Tests a stored channel with its stored credentials and reports the outcome as a toast. */
  test: (channel: NotificationChannel) => Promise<void>;
}

export function useNotificationChannels(enabled: boolean, onToast: NotificationToast): UseNotificationChannelsReturn {
  const [channels, setChannels] = useState<NotificationChannel[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [testingIds, setTestingIds] = useState<ReadonlySet<string>>(new Set<string>());

  const refresh = useCallback(async (): Promise<void> => {
    try {
      setChannels(await getNotificationChannels());
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to load notification channels'), 'error');
    }
  }, [onToast]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setLoading(true);
    getNotificationChannels()
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
    async (channel: NotificationChannel, value: boolean): Promise<void> => {
      setChannels((prev) => prev.map((c) => (c.id === channel.id ? { ...c, enabled: value } : c)));
      try {
        await updateNotificationChannel(channel.id, {
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
        await deleteNotificationChannel(id);
        setChannels((prev) => prev.filter((c) => c.id !== id));
        onToast('Channel deleted');
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to delete channel'), 'error');
      }
    },
    [onToast]
  );

  const test = useCallback(
    async (channel: NotificationChannel): Promise<void> => {
      setTestingIds((prev) => new Set(prev).add(channel.id));
      try {
        const res = await testNotificationChannel({
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

  return { channels, loading, testingIds, refresh, setEnabled, remove, test };
}

