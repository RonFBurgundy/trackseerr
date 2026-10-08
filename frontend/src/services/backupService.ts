import { apiRequest } from './apiClient';
import type {
  BackupItem,
  BackupSettingsResponse,
  BackupSettingsUpdate,
  RestoreResponse,
  SuccessFlag,
} from '@/types/backup';

export async function getBackups(): Promise<BackupItem[]> {
  const res = await apiRequest<BackupItem[]>('/api/system/backups');
  return res || [];
}

export async function createManualBackup(): Promise<BackupItem> {
  return await apiRequest<BackupItem>('/api/system/backups', {
    method: 'POST',
  });
}

export async function deleteBackup(name: string): Promise<boolean> {
  const res = await apiRequest<SuccessFlag>(`/api/system/backups/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  });
  return Boolean(res?.success);
}

export async function getBackupSettings(): Promise<BackupSettingsResponse> {
  return await apiRequest<BackupSettingsResponse>('/api/system/backups/settings');
}

export async function updateBackupSettings(
  update: BackupSettingsUpdate
): Promise<BackupSettingsResponse> {
  return await apiRequest<BackupSettingsResponse>('/api/system/backups/settings', {
    method: 'PUT',
    body: update,
  });
}

export async function restoreBackup(name: string): Promise<RestoreResponse> {
  return await apiRequest<RestoreResponse>(
    `/api/system/backups/${encodeURIComponent(name)}/restore`,
    {
      method: 'POST',
    }
  );
}

export async function restoreUpload(file: File): Promise<RestoreResponse> {
  const formData = new FormData();
  formData.append('file', file, file.name);
  return await apiRequest<RestoreResponse>('/api/system/backups/restore-upload', {
    method: 'POST',
    body: formData,
  });
}

export function getBackupDownloadUrl(name: string): string {
  return `/api/system/backups/${encodeURIComponent(name)}/download`;
}

/**
 * Polls /api/system/status until the server finishes restarting and answers with HTTP 200.
 */
export async function pollUntilServerReady(
  maxWaitMs: number = 60000,
  pollIntervalMs: number = 1500
): Promise<boolean> {
  const startTime = Date.now();
  // Allow a short initial grace period for the server to initiate restart
  await new Promise((resolve) => setTimeout(resolve, 2000));

  while (Date.now() - startTime < maxWaitMs) {
    try {
      const res = await fetch('/api/system/status', {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (res.ok) {
        return true;
      }
    } catch {
      // Network failures are expected while the server is restarting
    }
    await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
  }
  return false;
}
