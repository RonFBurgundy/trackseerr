import { useCallback, useEffect, useState } from 'react';
import type { LogFileItem } from '@/types/models';
import { fetchLogFileBlob, getLogFiles } from '@/services/systemService';
import { errorMessage } from '@/services/apiClient';

export interface UseLogFilesReturn {
  files: LogFileItem[];
  isLoading: boolean;
  error: string | null;
  downloadingName: string | null;
  refresh: () => Promise<void>;
  download: (name: string) => Promise<void>;
}

/** Log file listing for the download modal; loads while `enabled` is true (the modal is open). */
export function useLogFiles(enabled: boolean): UseLogFilesReturn {
  const [files, setFiles] = useState<LogFileItem[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [downloadingName, setDownloadingName] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      setFiles(await getLogFiles());
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to list log files'));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) void refresh();
  }, [enabled, refresh]);

  const download = useCallback(async (name: string) => {
    setDownloadingName(name);
    setError(null);
    try {
      const blob = await fetchLogFileBlob(name);
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      window.URL.revokeObjectURL(url);
    } catch (err: unknown) {
      setError(errorMessage(err, `Failed to download ${name}`));
    } finally {
      setDownloadingName(null);
    }
  }, []);

  return { files, isLoading, error, downloadingName, refresh, download };
}
