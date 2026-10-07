import type { Schema } from '@/types/apiSchema';
import { apiRequest } from './apiClient';
import type {
  ScheduledTaskItem,
  SystemActivity,
  SystemResources,
  TaskRunItem,
  SystemLogItem,
  LidarrHealth,
} from '@/types/models';

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
