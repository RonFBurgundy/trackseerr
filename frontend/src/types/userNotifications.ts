import type { Schema } from './apiSchema';
import type { NotificationEventId } from './notifications';

export type UserNotificationItem = Schema<'UserNotificationItem'>;
export type NotificationInboxResponse = Schema<'NotificationInboxResponse'>;
export type UnreadCountResponse = Schema<'UnreadCountResponse'>;
export type MarkAllReadResponse = Schema<'MarkAllReadResponse'>;
export type NotificationActionResponse = Schema<'NotificationActionResponse'>;
export type NotificationPrefItem = Schema<'NotificationPrefItem'>;
export type NotificationPrefUpdatePayload = Schema<'NotificationPrefUpdatePayload'>;
export type NotificationPrefBulkUpdatePayload = Schema<'NotificationPrefBulkUpdatePayload'>;
export type PushConfigResponse = Schema<'PushConfigResponse'>;
export type PushSubscribePayload = Schema<'PushSubscribePayload'>;
export type PushSubscriptionResponse = Schema<'PushSubscriptionResponse'>;
export type PushUnsubscribePayload = Schema<'PushUnsubscribePayload'>;
export type UserNotificationChannelItem = Schema<'UserNotificationChannelItem'>;
export type UserNotificationChannelPayload = Schema<'UserNotificationChannelPayload'>;
export type UserTestNotificationPayload = Schema<'UserTestNotificationPayload'>;
export type TestNotificationResponse = Schema<'TestNotificationResponse'>;

export type UserNotificationChannelType = 'discord' | 'telegram' | 'pushover' | 'webhook';

export const USER_NOTIFICATION_CHANNEL_TYPES: readonly UserNotificationChannelType[] = [
  'discord',
  'telegram',
  'pushover',
  'webhook',
];

export const USER_NOTIFICATION_EVENTS: ReadonlyArray<{ id: NotificationEventId; label: string }> = [
  { id: 'request_approved', label: 'Request approved' },
  { id: 'request_rejected', label: 'Request rejected' },
  { id: 'download_started', label: 'Download started' },
  { id: 'item_available', label: 'Item available' },
  { id: 'download_failed', label: 'Download failed' },
  { id: 'issue_updated', label: 'Issue updated' },
  { id: 'issue_resolved', label: 'Issue resolved' },
];

export const USER_NOTIFICATION_EVENT_IDS = new Set<string>(
  USER_NOTIFICATION_EVENTS.map((e) => e.id)
);
