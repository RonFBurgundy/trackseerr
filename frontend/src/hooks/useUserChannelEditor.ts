import { useCallback, useState } from 'react';
import type {
  UserNotificationChannelItem,
  UserNotificationChannelType,
} from '@/types/userNotifications';
import {
  USER_NOTIFICATION_CHANNEL_TYPES,
  USER_NOTIFICATION_EVENTS,
} from '@/types/userNotifications';
import {
  buildNotificationConfig,
  defaultNotificationValues,
  validateNotificationDraft,
  type NotificationChannelDraft,
  type NotificationEventId,
} from '@/types/notifications';
import {
  createUserChannel,
  testUserChannel,
  updateUserChannel,
} from '@/services/userNotificationsService';
import { errorMessage } from '@/services/apiClient';
import type { UserNotificationToast } from './useUserChannels';

export interface UseUserChannelEditorReturn {
  isOpen: boolean;
  editing: UserNotificationChannelItem | null;
  draft: NotificationChannelDraft;
  saving: boolean;
  testing: boolean;
  error: string | null;
  openNew: () => void;
  openEdit: (channel: UserNotificationChannelItem) => void;
  close: () => void;
  setName: (name: string) => void;
  setEnabled: (enabled: boolean) => void;
  setType: (type: UserNotificationChannelType) => void;
  setValue: (key: string, value: string | boolean) => void;
  toggleEvent: (event: NotificationEventId) => void;
  save: () => Promise<void>;
  runTest: () => Promise<void>;
}

function emptyUserChannelDraft(
  channelType: UserNotificationChannelType = 'discord'
): NotificationChannelDraft {
  return {
    name: '',
    channelType,
    enabled: true,
    events: USER_NOTIFICATION_EVENTS.map((e) => e.id),
    values: defaultNotificationValues(channelType),
  };
}

export function useUserChannelEditor(
  onSaved: () => Promise<void>,
  onToast: UserNotificationToast
): UseUserChannelEditorReturn {
  const [isOpen, setIsOpen] = useState<boolean>(false);
  const [editing, setEditing] = useState<UserNotificationChannelItem | null>(null);
  const [draft, setDraft] = useState<NotificationChannelDraft>(emptyUserChannelDraft());
  const [saving, setSaving] = useState<boolean>(false);
  const [testing, setTesting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const openNew = useCallback((): void => {
    setDraft(emptyUserChannelDraft());
    setEditing(null);
    setError(null);
    setIsOpen(true);
  }, []);

  const openEdit = useCallback(
    (channel: UserNotificationChannelItem): void => {
      const type = channel.channel_type as UserNotificationChannelType;
      if (!USER_NOTIFICATION_CHANNEL_TYPES.includes(type)) {
        onToast(`Unsupported channel type "${channel.channel_type}"`, 'error');
        return;
      }
      const stored = (channel.config ?? {}) as Record<string, unknown>;
      const values = defaultNotificationValues(type);
      for (const [k, v] of Object.entries(stored)) {
        if (typeof v === 'boolean') values[k] = v;
        else if (typeof v === 'string' || typeof v === 'number') values[k] = String(v);
      }
      setDraft({
        name: channel.name,
        channelType: type,
        enabled: channel.enabled,
        events: (channel.events ?? []).filter((e): e is NotificationEventId =>
          USER_NOTIFICATION_EVENTS.some((ev) => ev.id === e)
        ),
        values,
      });
      setEditing(channel);
      setError(null);
      setIsOpen(true);
    },
    [onToast]
  );

  const close = useCallback((): void => setIsOpen(false), []);

  const setName = useCallback((name: string): void => setDraft((p) => ({ ...p, name })), []);
  const setEnabled = useCallback((enabled: boolean): void => setDraft((p) => ({ ...p, enabled })), []);
  const setType = useCallback(
    (channelType: UserNotificationChannelType): void =>
      setDraft((p) => ({ ...p, channelType, values: defaultNotificationValues(channelType) })),
    []
  );
  const setValue = useCallback(
    (key: string, value: string | boolean): void =>
      setDraft((p) => ({ ...p, values: { ...p.values, [key]: value } })),
    []
  );
  const toggleEvent = useCallback(
    (event: NotificationEventId): void =>
      setDraft((p) => ({
        ...p,
        events: p.events.includes(event) ? p.events.filter((e) => e !== event) : [...p.events, event],
      })),
    []
  );

  const save = useCallback(async (): Promise<void> => {
    const invalid = validateNotificationDraft(draft, editing);
    if (invalid) {
      setError(invalid);
      return;
    }
    setError(null);
    setSaving(true);
    try {
      const config = buildNotificationConfig(draft.channelType, draft.values);
      const payload = {
        name: draft.name.trim(),
        channel_type: draft.channelType,
        enabled: draft.enabled,
        config,
        events: draft.events,
      };
      if (editing) {
        await updateUserChannel(editing.id, payload);
      } else {
        await createUserChannel(payload);
      }
      onToast(editing ? 'Channel updated' : 'Channel created');
      setIsOpen(false);
      await onSaved();
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to save channel'));
    } finally {
      setSaving(false);
    }
  }, [draft, editing, onSaved, onToast]);

  const runTest = useCallback(async (): Promise<void> => {
    const invalid = validateNotificationDraft(draft, editing);
    if (invalid) {
      setError(invalid);
      return;
    }
    setError(null);
    setTesting(true);
    try {
      const config = buildNotificationConfig(draft.channelType, draft.values);
      const res = await testUserChannel({
        channel_type: draft.channelType,
        config,
        ...(editing ? { channel_id: editing.id } : {}),
      });
      onToast(res.message, res.success ? 'ok' : 'error');
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Test failed'), 'error');
    } finally {
      setTesting(false);
    }
  }, [draft, editing, onToast]);

  return {
    isOpen,
    editing,
    draft,
    saving,
    testing,
    error,
    openNew,
    openEdit,
    close,
    setName,
    setEnabled,
    setType,
    setValue,
    toggleEvent,
    save,
    runTest,
  };
}
