import { useCallback, useState } from 'react';
import {
  buildNotificationConfig,
  defaultNotificationValues,
  draftFromChannel,
  emptyNotificationDraft,
  notificationDraftToPayload,
  validateNotificationDraft,
  type NotificationChannel,
  type NotificationChannelDraft,
  type NotificationChannelType,
  type NotificationEventId,
} from '@/types/notifications';
import {
  createNotificationChannel,
  updateNotificationChannel,
  testNotificationChannel,
} from '@/services/notificationsService';
import { errorMessage } from '@/services/apiClient';
import type { NotificationToast } from './useNotificationChannels';

export interface UseNotificationChannelEditorReturn {
  isOpen: boolean;
  /** The stored channel being edited (null when adding). */
  editing: NotificationChannel | null;
  draft: NotificationChannelDraft;
  saving: boolean;
  testing: boolean;
  error: string | null;
  openNew: () => void;
  openEdit: (channel: NotificationChannel) => void;
  close: () => void;
  setName: (name: string) => void;
  setEnabled: (enabled: boolean) => void;
  setType: (type: NotificationChannelType) => void;
  setValue: (key: string, value: string | boolean) => void;
  toggleEvent: (event: NotificationEventId) => void;
  save: () => Promise<void>;
  runTest: () => Promise<void>;
}

export function useNotificationChannelEditor(
  onSaved: () => Promise<void>,
  onToast: NotificationToast
): UseNotificationChannelEditorReturn {
  const [isOpen, setIsOpen] = useState<boolean>(false);
  const [editing, setEditing] = useState<NotificationChannel | null>(null);
  const [draft, setDraft] = useState<NotificationChannelDraft>(emptyNotificationDraft());
  const [saving, setSaving] = useState<boolean>(false);
  const [testing, setTesting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const openNew = useCallback((): void => {
    setDraft(emptyNotificationDraft());
    setEditing(null);
    setError(null);
    setIsOpen(true);
  }, []);

  const openEdit = useCallback(
    (channel: NotificationChannel): void => {
      const next = draftFromChannel(channel);
      if (!next) {
        onToast(`Unsupported channel type "${channel.channel_type}"`, 'error');
        return;
      }
      setDraft(next);
      setEditing(channel);
      setError(null);
      setIsOpen(true);
    },
    [onToast]
  );

  const close = useCallback((): void => setIsOpen(false), []);

  const setName = useCallback((name: string): void => setDraft((p) => ({ ...p, name })), []);
  const setEnabled = useCallback((enabled: boolean): void => setDraft((p) => ({ ...p, enabled })), []);
  // The type is locked when editing, so a stored secret is never carried to a different destination type.
  const setType = useCallback(
    (channelType: NotificationChannelType): void =>
      setDraft((p) => ({ ...p, channelType, values: defaultNotificationValues(channelType) })),
    []
  );
  const setValue = useCallback(
    (key: string, value: string | boolean): void => setDraft((p) => ({ ...p, values: { ...p.values, [key]: value } })),
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
      const payload = notificationDraftToPayload(draft, editing?.id);
      if (editing) await updateNotificationChannel(editing.id, payload);
      else await createNotificationChannel(payload);
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
      const res = await testNotificationChannel({
        channel_type: draft.channelType,
        config: buildNotificationConfig(draft.channelType, draft.values),
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
    isOpen, editing, draft, saving, testing, error,
    openNew, openEdit, close, setName, setEnabled, setType, setValue, toggleEvent, save, runTest,
  };
}
