import type { Schema } from './apiSchema';

/** A stored channel as the API returns it: secret config values arrive masked. */
export type NotificationChannel = Schema<'NotificationChannelItem'>;
export type NotificationChannelPayload = Schema<'NotificationChannelPayload'>;
export type NotificationTestPayload = Schema<'TestNotificationPayload'>;
export type NotificationTestResult = Schema<'TestNotificationResponse'>;

/** Mirrors backend `NotificationChannelType` (the schema declares a bare string). */
export type NotificationChannelType = 'discord' | 'telegram' | 'pushover' | 'webhook' | 'email';

export const NOTIFICATION_CHANNEL_TYPES: readonly NotificationChannelType[] = [
  'discord',
  'telegram',
  'pushover',
  'webhook',
  'email',
];

export const NOTIFICATION_CHANNEL_TYPE_LABELS: Record<NotificationChannelType, string> = {
  discord: 'Discord',
  telegram: 'Telegram',
  pushover: 'Pushover',
  webhook: 'Webhook',
  email: 'Email',
};

/** Mirrors backend `NotificationEvent` (the schema declares `string[]`). */
export type NotificationEventId =
  | 'request_created'
  | 'request_approved'
  | 'request_rejected'
  | 'download_started'
  | 'item_available'
  | 'download_failed'
  | 'issue_reported'
  | 'issue_updated'
  | 'issue_resolved';

export const NOTIFICATION_EVENTS: ReadonlyArray<{ id: NotificationEventId; label: string }> = [
  { id: 'request_created', label: 'Request created' },
  { id: 'request_approved', label: 'Request approved' },
  { id: 'request_rejected', label: 'Request rejected' },
  { id: 'download_started', label: 'Download started' },
  { id: 'item_available', label: 'Item available' },
  { id: 'download_failed', label: 'Download failed' },
  { id: 'issue_reported', label: 'Issue reported' },
  { id: 'issue_updated', label: 'Issue updated' },
  { id: 'issue_resolved', label: 'Issue resolved' },
];

export const NOTIFICATION_EVENT_LABELS: Record<string, string> = Object.fromEntries(
  NOTIFICATION_EVENTS.map((e) => [e.id, e.label])
);

export type NotificationFieldKind = 'text' | 'secret' | 'number' | 'boolean';

export interface NotificationFieldDef {
  key: string;
  label: string;
  kind: NotificationFieldKind;
  required: boolean;
  placeholder?: string;
  /** Initial value for a new channel. */
  defaultValue?: string | boolean;
}

/** Config keys each channel type needs, matching the dispatcher's `_send_to_channel`. */
export const NOTIFICATION_FIELDS: Record<NotificationChannelType, readonly NotificationFieldDef[]> = {
  discord: [{ key: 'webhook_url', label: 'Webhook URL', kind: 'secret', required: true, placeholder: 'https://discord.com/api/webhooks/...' }],
  telegram: [
    { key: 'bot_token', label: 'Bot token', kind: 'secret', required: true },
    { key: 'chat_id', label: 'Chat ID', kind: 'text', required: true },
  ],
  pushover: [
    { key: 'user_key', label: 'User key', kind: 'secret', required: true },
    { key: 'app_token', label: 'Application token', kind: 'secret', required: true },
  ],
  webhook: [
    { key: 'webhook_url', label: 'Webhook URL', kind: 'secret', required: true, placeholder: 'https://example.com/hook' },
    { key: 'secret_header', label: 'Secret header (optional)', kind: 'secret', required: false },
  ],
  email: [
    { key: 'smtp_host', label: 'SMTP host', kind: 'text', required: true },
    { key: 'smtp_port', label: 'SMTP port', kind: 'number', required: false, defaultValue: '587' },
    { key: 'username', label: 'Username', kind: 'text', required: false },
    { key: 'password', label: 'Password', kind: 'secret', required: false },
    { key: 'use_tls', label: 'Use STARTTLS', kind: 'boolean', required: false, defaultValue: true },
    { key: 'use_ssl', label: 'Use SSL', kind: 'boolean', required: false, defaultValue: false },
    { key: 'from_addr', label: 'From address', kind: 'text', required: true },
    { key: 'to_addr', label: 'To address', kind: 'text', required: true },
  ],
};

export function isNotificationChannelType(value: string): value is NotificationChannelType {
  return (NOTIFICATION_CHANNEL_TYPES as readonly string[]).includes(value);
}

/** What the user has typed per config key. Secret fields stay '' until the user types a replacement. */
export type NotificationConfigValues = Record<string, string | boolean>;

export interface NotificationChannelDraft {
  name: string;
  channelType: NotificationChannelType;
  enabled: boolean;
  events: NotificationEventId[];
  values: NotificationConfigValues;
}

export function emptyNotificationDraft(channelType: NotificationChannelType = 'discord'): NotificationChannelDraft {
  return {
    name: '',
    channelType,
    enabled: true,
    events: NOTIFICATION_EVENTS.map((e) => e.id),
    values: defaultNotificationValues(channelType),
  };
}

export function defaultNotificationValues(channelType: NotificationChannelType): NotificationConfigValues {
  const values: NotificationConfigValues = {};
  for (const f of NOTIFICATION_FIELDS[channelType]) {
    values[f.key] = f.defaultValue ?? (f.kind === 'boolean' ? false : '');
  }
  return values;
}

const isEventId = (value: string): value is NotificationEventId => value in NOTIFICATION_EVENT_LABELS;

/** Draft for editing a stored channel. Masked secrets are NOT copied in: they stay blank, meaning "unchanged". */
export function draftFromChannel(channel: NotificationChannel): NotificationChannelDraft | null {
  if (!isNotificationChannelType(channel.channel_type)) return null;
  const channelType = channel.channel_type;
  const stored = channel.config ?? {};
  const values = defaultNotificationValues(channelType);
  for (const f of NOTIFICATION_FIELDS[channelType]) {
    if (f.kind === 'secret') continue;
    const v = stored[f.key];
    if (f.kind === 'boolean') {
      if (typeof v === 'boolean') values[f.key] = v;
    } else if (typeof v === 'string' || typeof v === 'number') {
      values[f.key] = String(v);
    }
  }
  return {
    name: channel.name,
    channelType,
    enabled: channel.enabled,
    events: (channel.events ?? []).filter(isEventId),
    values,
  };
}

export interface StoredNotificationChannelLike {
  config?: Record<string, unknown>;
}

/** True when the stored channel already holds a (masked) value for this secret key. */
export function hasStoredSecret(channel: StoredNotificationChannelLike | null, key: string): boolean {
  const v = channel?.config?.[key];
  return typeof v === 'string' && v !== '';
}

/**
 * Builds the `config` object for a create/update/test request.
 *
 * Secret fields are included only when the user typed a value. On update the backend fills every omitted key
 * from the stored config (`_merge_masked_config`), so omission means "keep the stored secret"; a masked
 * placeholder must never be echoed back (the backend rejects half-edited placeholders with HTTP 400).
 * Non-secret fields are always sent so clearing one persists.
 */
export function buildNotificationConfig(
  channelType: NotificationChannelType,
  values: NotificationConfigValues
): Record<string, string | number | boolean> {
  const config: Record<string, string | number | boolean> = {};
  for (const f of NOTIFICATION_FIELDS[channelType]) {
    const raw = values[f.key];
    if (f.kind === 'boolean') {
      config[f.key] = raw === true;
      continue;
    }
    const text = typeof raw === 'string' ? raw.trim() : '';
    if (f.kind === 'secret') {
      if (text !== '') config[f.key] = text;
    } else if (f.kind === 'number') {
      if (text !== '' && Number.isFinite(Number(text))) config[f.key] = Number(text);
    } else {
      config[f.key] = text;
    }
  }
  return config;
}

/** Returns an error message, or null. A required secret may be blank only when a stored value exists. */
export function validateNotificationDraft(
  draft: NotificationChannelDraft,
  existing: StoredNotificationChannelLike | null
): string | null {
  if (!draft.name.trim()) return 'Name is required';
  for (const f of NOTIFICATION_FIELDS[draft.channelType]) {
    if (!f.required) continue;
    const raw = draft.values[f.key];
    const typed = typeof raw === 'string' && raw.trim() !== '';
    if (typed) continue;
    if (f.kind === 'secret' && hasStoredSecret(existing, f.key)) continue;
    return `${f.label} is required`;
  }
  return null;
}

export function notificationDraftToPayload(
  draft: NotificationChannelDraft,
  id?: string | null
): NotificationChannelPayload {
  return {
    ...(id ? { id } : {}),
    name: draft.name.trim(),
    channel_type: draft.channelType,
    enabled: draft.enabled,
    config: buildNotificationConfig(draft.channelType, draft.values),
    events: draft.events,
  };
}
