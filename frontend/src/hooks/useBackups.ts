import { useCallback, useEffect, useState } from 'react';
import type {
  BackupItem,
  BackupSettingsResponse,
  BackupSettingsUpdate,
} from '@/types/backup';
import {
  createManualBackup as apiCreateManualBackup,
  deleteBackup as apiDeleteBackup,
  getBackupSettings as apiGetBackupSettings,
  getBackups as apiGetBackups,
  pollUntilServerReady,
  restoreBackup as apiRestoreBackup,
  restoreUpload as apiRestoreUpload,
  updateBackupSettings as apiUpdateBackupSettings,
} from '@/services/backupService';

export interface UseBackupsResult {
  backups: BackupItem[];
  settings: BackupSettingsResponse | null;
  isLoading: boolean;
  isBackingUp: boolean;
  isRestoring: boolean;
  isUploading: boolean;
  isSavingSettings: boolean;
  isRestarting: boolean;
  error: string | null;
  fetchBackups: () => Promise<void>;
  createBackup: () => Promise<boolean>;
  deleteBackup: (name: string) => Promise<boolean>;
  saveSettings: (update: BackupSettingsUpdate) => Promise<boolean>;
  restore: (name: string) => Promise<boolean>;
  uploadAndRestore: (file: File) => Promise<boolean>;
}

export function useBackups(
  onToast?: (message: string, tone?: 'ok' | 'error') => void
): UseBackupsResult {
  const [backups, setBackups] = useState<BackupItem[]>([]);
  const [settings, setSettings] = useState<BackupSettingsResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isBackingUp, setIsBackingUp] = useState<boolean>(false);
  const [isRestoring, setIsRestoring] = useState<boolean>(false);
  const [isUploading, setIsUploading] = useState<boolean>(false);
  const [isSavingSettings, setIsSavingSettings] = useState<boolean>(false);
  const [isRestarting, setIsRestarting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const fetchBackups = useCallback(async (): Promise<void> => {
    setIsLoading(true);
    setError(null);
    try {
      const [backupsData, settingsData] = await Promise.all([
        apiGetBackups(),
        apiGetBackupSettings(),
      ]);
      setBackups(backupsData);
      setSettings(settingsData);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to load backups';
      setError(msg);
      if (onToast) onToast(msg, 'error');
    } finally {
      setIsLoading(false);
    }
  }, [onToast]);

  useEffect(() => {
    void fetchBackups();
  }, [fetchBackups]);

  const createBackup = useCallback(async (): Promise<boolean> => {
    setIsBackingUp(true);
    try {
      const item = await apiCreateManualBackup();
      setBackups((prev) => [item, ...prev.filter((b) => b.name !== item.name)]);
      if (onToast) onToast(`Backup ${item.name} created successfully`, 'ok');
      return true;
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to create backup';
      if (onToast) onToast(msg, 'error');
      return false;
    } finally {
      setIsBackingUp(false);
    }
  }, [onToast]);

  const deleteBackup = useCallback(
    async (name: string): Promise<boolean> => {
      try {
        const success = await apiDeleteBackup(name);
        if (success) {
          setBackups((prev) => prev.filter((b) => b.name !== name));
          if (onToast) onToast(`Backup ${name} deleted`, 'ok');
          return true;
        }
        if (onToast) onToast(`Could not delete backup ${name}`, 'error');
        return false;
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Failed to delete backup';
        if (onToast) onToast(msg, 'error');
        return false;
      }
    },
    [onToast]
  );

  const saveSettings = useCallback(
    async (update: BackupSettingsUpdate): Promise<boolean> => {
      setIsSavingSettings(true);
      try {
        const updated = await apiUpdateBackupSettings(update);
        setSettings(updated);
        if (onToast) onToast('Backup settings updated', 'ok');
        return true;
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Failed to update backup settings';
        if (onToast) onToast(msg, 'error');
        return false;
      } finally {
        setIsSavingSettings(false);
      }
    },
    [onToast]
  );

  const handleRestartSequence = useCallback(async (): Promise<void> => {
    setIsRestarting(true);
    const ready = await pollUntilServerReady(90000, 2000);
    if (ready) {
      window.location.reload();
    } else {
      setIsRestarting(false);
      if (onToast) {
        onToast('Server restart timed out. Please refresh the page manually.', 'error');
      }
    }
  }, [onToast]);

  const restore = useCallback(
    async (name: string): Promise<boolean> => {
      setIsRestoring(true);
      try {
        await apiRestoreBackup(name);
        if (onToast) onToast('Restore staged; restarting TrackSeerr...', 'ok');
        void handleRestartSequence();
        return true;
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Failed to stage restore';
        if (onToast) onToast(msg, 'error');
        setIsRestoring(false);
        return false;
      }
    },
    [onToast, handleRestartSequence]
  );

  const uploadAndRestore = useCallback(
    async (file: File): Promise<boolean> => {
      setIsUploading(true);
      try {
        await apiRestoreUpload(file);
        if (onToast) onToast('Backup uploaded and staged; restarting TrackSeerr...', 'ok');
        void handleRestartSequence();
        return true;
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Failed to upload backup';
        if (onToast) onToast(msg, 'error');
        setIsUploading(false);
        return false;
      }
    },
    [onToast, handleRestartSequence]
  );

  return {
    backups,
    settings,
    isLoading,
    isBackingUp,
    isRestoring,
    isUploading,
    isSavingSettings,
    isRestarting,
    error,
    fetchBackups,
    createBackup,
    deleteBackup,
    saveSettings,
    restore,
    uploadAndRestore,
  };
}
