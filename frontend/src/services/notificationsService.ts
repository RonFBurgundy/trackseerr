import { apiRequest } from './apiClient';
import type { Schema } from '@/types/apiSchema';
import type {
  NotificationChannel,
  NotificationChannelPayload,
  NotificationTestPayload,
  NotificationTestResult,
} from '@/types/notifications';

const BASE = '/api/settings/notifications';

export async function getNotificationChannels(): Promise<NotificationChannel[]> {
  const res = await apiRequest<NotificationChannel[]>(BASE);
  return res || [];
}

export async function createNotificationChannel(payload: NotificationChannelPayload): Promise<NotificationChannel> {
  return apiRequest<NotificationChannel>(BASE, { method: 'POST', body: payload });
}

/** Omitted config keys keep their stored value server-side, so secrets are only sent when replaced. */
export async function updateNotificationChannel(
  id: string,
  payload: NotificationChannelPayload
): Promise<NotificationChannel> {
  return apiRequest<NotificationChannel>(`${BASE}/${encodeURIComponent(id)}`, { method: 'PUT', body: payload });
}

export async function deleteNotificationChannel(
  id: string
): Promise<Schema<'trackseerr__api__schemas__notifications__DeletedResponse'>> {
  return apiRequest<Schema<'trackseerr__api__schemas__notifications__DeletedResponse'>>(
    `${BASE}/${encodeURIComponent(id)}`,
    { method: 'DELETE' }
  );
}

/** Pass `channel_id` when testing a stored channel so omitted secrets resolve to the stored values. */
export async function testNotificationChannel(payload: NotificationTestPayload): Promise<NotificationTestResult> {
  return apiRequest<NotificationTestResult>(`${BASE}/test`, { method: 'POST', body: payload });
}
