import { apiRequest } from './apiClient';
import type {
  MarkAllReadResponse,
  NotificationInboxResponse,
  NotificationPrefBulkUpdatePayload,
  NotificationPrefItem,
  NotificationPrefUpdatePayload,
  PushConfigResponse,
  PushSubscribePayload,
  PushSubscriptionResponse,
  PushUnsubscribePayload,
  TestNotificationResponse,
  UnreadCountResponse,
  UserNotificationChannelItem,
  UserNotificationChannelPayload,
  UserTestNotificationPayload,
} from '@/types/userNotifications';
import type { Schema } from '@/types/apiSchema';

const BASE = '/api/account/notifications';

export async function getInbox(
  page: number = 1,
  pageSize: number = 20
): Promise<NotificationInboxResponse> {
  const query = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  return apiRequest<NotificationInboxResponse>(`${BASE}/inbox?${query.toString()}`);
}

export async function getUnreadCount(): Promise<number> {
  const res = await apiRequest<UnreadCountResponse>(`${BASE}/inbox/unread-count`);
  return res.unread_count;
}

export async function markNotificationRead(notificationId: string): Promise<void> {
  await apiRequest<unknown>(`${BASE}/inbox/${encodeURIComponent(notificationId)}/read`, {
    method: 'POST',
  });
}

export async function markAllNotificationsRead(): Promise<number> {
  const res = await apiRequest<MarkAllReadResponse>(`${BASE}/inbox/read-all`, {
    method: 'POST',
  });
  return res.marked_read;
}

export async function deleteNotification(notificationId: string): Promise<void> {
  await apiRequest<unknown>(`${BASE}/inbox/${encodeURIComponent(notificationId)}`, {
    method: 'DELETE',
  });
}

export async function getUserPrefs(): Promise<NotificationPrefItem[]> {
  const res = await apiRequest<NotificationPrefItem[]>(`${BASE}/prefs`);
  return res || [];
}

export async function updateUserPref(
  event: string,
  payload: NotificationPrefUpdatePayload
): Promise<NotificationPrefItem> {
  return apiRequest<NotificationPrefItem>(`${BASE}/prefs/${encodeURIComponent(event)}`, {
    method: 'PUT',
    body: payload,
  });
}

export async function updateUserPrefsBulk(
  payload: NotificationPrefBulkUpdatePayload
): Promise<NotificationPrefItem[]> {
  const res = await apiRequest<NotificationPrefItem[]>(`${BASE}/prefs`, {
    method: 'PUT',
    body: payload,
  });
  return res || [];
}

export async function getPushConfig(): Promise<PushConfigResponse> {
  return apiRequest<PushConfigResponse>(`${BASE}/push`);
}

export async function subscribePush(
  payload: PushSubscribePayload
): Promise<PushSubscriptionResponse> {
  return apiRequest<PushSubscriptionResponse>(`${BASE}/push/subscribe`, {
    method: 'POST',
    body: payload,
  });
}

export async function unsubscribePush(payload: PushUnsubscribePayload): Promise<void> {
  await apiRequest<unknown>(`${BASE}/push/subscription`, {
    method: 'DELETE',
    body: payload,
  });
}

export async function testPush(): Promise<TestNotificationResponse> {
  return apiRequest<TestNotificationResponse>(`${BASE}/push/test`, {
    method: 'POST',
  });
}

export async function getUserChannels(): Promise<UserNotificationChannelItem[]> {
  const res = await apiRequest<UserNotificationChannelItem[]>(`${BASE}/channels`);
  return res || [];
}

export async function createUserChannel(
  payload: UserNotificationChannelPayload
): Promise<UserNotificationChannelItem> {
  return apiRequest<UserNotificationChannelItem>(`${BASE}/channels`, {
    method: 'POST',
    body: payload,
  });
}

export async function updateUserChannel(
  id: string,
  payload: UserNotificationChannelPayload
): Promise<UserNotificationChannelItem> {
  return apiRequest<UserNotificationChannelItem>(`${BASE}/channels/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: payload,
  });
}

export async function deleteUserChannel(
  id: string
): Promise<Schema<'plex_playlist_sync__api__schemas__notifications__DeletedResponse'>> {
  return apiRequest<Schema<'plex_playlist_sync__api__schemas__notifications__DeletedResponse'>>(
    `${BASE}/channels/${encodeURIComponent(id)}`,
    { method: 'DELETE' }
  );
}

export async function testUserChannel(
  payload: UserTestNotificationPayload
): Promise<TestNotificationResponse> {
  return apiRequest<TestNotificationResponse>(`${BASE}/channels/test`, {
    method: 'POST',
    body: payload,
  });
}
