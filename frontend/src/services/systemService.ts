import type { Schema } from '@/types/apiSchema';
import { apiRequest, getAuthToken } from './apiClient';
import type {
  ScheduledTaskItem,
  SystemActivity,
  SystemResources,
  TaskRunItem,
  SystemLogItem,
  LogFileItem,
  LogSettings,
  LogSettingsUpdate,
  LidarrHealth,
} from '@/types/models';

export type SystemEventItem = Schema<'SystemEvent'>;

/** Newest lifecycle events (scan/download/sync/library changes), for the Logs panel's Events toggle. */
export async function getSystemEvents(pageSize: number): Promise<SystemEventItem[]> {
  const page = await apiRequest<Schema<'SystemEventsPage'>>(`/api/system/events?page=1&page_size=${pageSize}`);
  return page.items || [];
}

export interface GetSystemLogsParams {
  level?: string;
  search?: string;
  limit?: number;
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

export async function getLidarrHealth(): Promise<LidarrHealth> {
  return apiRequest<LidarrHealth>('/api/system/lidarr-health');
}

export async function updateTaskSchedule(
  taskId: string,
  intervalSeconds: number | null
): Promise<ScheduledTaskItem> {
  return await apiRequest<ScheduledTaskItem>(
    `/api/system/tasks/${encodeURIComponent(taskId)}/schedule`,
    { method: 'PUT', body: { interval_seconds: intervalSeconds } }
  );
}

export async function getTaskRuns(
  taskId: string,
  days: number = 7,
  limit: number = 200
): Promise<TaskRunItem[]> {
  const res = await apiRequest<TaskRunItem[] | null>(
    `/api/system/tasks/${encodeURIComponent(taskId)}/runs?days=${days}&limit=${limit}`
  );
  return res ?? [];
}

export async function getSystemActivity(): Promise<SystemActivity> {
  const res = await apiRequest<SystemActivity | null>('/api/system/activity');
  return { running: res?.running ?? [], recent: res?.recent ?? [] };
}

export async function getSystemResources(): Promise<SystemResources> {
  return await apiRequest<SystemResources>('/api/system/resources');
}
