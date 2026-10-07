import type { Schema } from '@/types/apiSchema';
import { apiRequest, getAuthToken } from './apiClient';
import type {
  ScheduledTaskItem,
  SystemEventsResponse,
  SystemLogItem,
  LogFileItem,
  LogSettings,
  LogSettingsUpdate,
  SystemQueueResponse,
  LidarrHealth,
} from '@/types/models';

export interface GetSystemEventsParams {
  page?: number;
  page_size?: number;
  event_type?: string;
  severity?: string;
  search?: string;
}

export interface GetSystemLogsParams {
  level?: string;
  search?: string;
  limit?: number;
}

export async function getSystemEvents(
  params: GetSystemEventsParams = {}
): Promise<SystemEventsResponse> {
  const query = new URLSearchParams();
  if (params.page !== undefined) query.set('page', params.page.toString());
  if (params.page_size !== undefined) query.set('page_size', params.page_size.toString());
  if (params.event_type) query.set('event_type', params.event_type);
  if (params.severity && params.severity !== 'all') query.set('severity', params.severity);
  if (params.search) query.set('search', params.search);

  const qs = query.toString();
  const url = qs ? `/api/system/events?${qs}` : '/api/system/events';
  return await apiRequest<SystemEventsResponse>(url);
}

export async function clearSystemEvents(): Promise<Schema<'SuccessFlag'>> {
  return await apiRequest<Schema<'SuccessFlag'>>('/api/system/events', {
    method: 'DELETE',
  });
}

export async function getSystemLogs(
  params: GetSystemLogsParams = {}
): Promise<SystemLogItem[]> {
  const query = new URLSearchParams();
  if (params.level && params.level !== 'all') query.set('level', params.level);
  if (params.search) query.set('search', params.search);
  if (params.limit !== undefined) query.set('limit', params.limit.toString());

  const qs = query.toString();
  const url = qs ? `/api/system/logs?${qs}` : '/api/system/logs';
  const res = await apiRequest<SystemLogItem[]>(url);
  return res || [];
}

export async function clearSystemLogs(): Promise<Schema<'SuccessFlag'>> {
  return await apiRequest<Schema<'SuccessFlag'>>('/api/system/logs', {
    method: 'DELETE',
  });
}

export function getSystemLogDownloadUrl(): string {
  return '/api/system/logs/download';
}

export async function getLogFiles(): Promise<LogFileItem[]> {
  return (await apiRequest<LogFileItem[]>('/api/system/logs/files')) || [];
}

export async function getLogSettings(): Promise<LogSettings> {
  return await apiRequest<LogSettings>('/api/system/logs/settings');
}

export async function updateLogSettings(update: LogSettingsUpdate): Promise<LogSettings> {
  return await apiRequest<LogSettings>('/api/system/logs/settings', { method: 'PUT', body: update });
}

/** Fetches one log file as a Blob (the endpoint needs the auth header, so a plain link cannot be used). */
export async function fetchLogFileBlob(name: string): Promise<Blob> {
  const token = getAuthToken();
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`/api/system/logs/files/${encodeURIComponent(name)}`, { headers });
  if (!res.ok) throw new Error(`Download failed with status ${res.status}`);
  return await res.blob();
}

export async function getScheduledTasks(): Promise<ScheduledTaskItem[]> {
  const res = await apiRequest<ScheduledTaskItem[]>('/api/system/tasks');
  return res || [];
}

export async function triggerScheduledTask(
  taskId: string
): Promise<Schema<'TaskActionResult'>> {
  return await apiRequest<Schema<'TaskActionResult'>>(
    `/api/system/tasks/${taskId}/run`,
    {
      method: 'POST',
    }
  );
}

export async function cancelScheduledTask(
  taskId: string
): Promise<Schema<'TaskActionResult'>> {
  return await apiRequest<Schema<'TaskActionResult'>>(
    `/api/system/tasks/${taskId}/cancel`,
    {
      method: 'POST',
    }
  );
}

export async function getSystemQueue(): Promise<SystemQueueResponse> {
  const res = await apiRequest<SystemQueueResponse | null>('/api/system/queue');
  return {
    running: res?.running ?? [],
    queued: res?.queued ?? [],
    recent: res?.recent ?? [],
  };
}

export async function getLidarrHealth(): Promise<LidarrHealth> {
  return apiRequest<LidarrHealth>('/api/system/lidarr-health');
}
