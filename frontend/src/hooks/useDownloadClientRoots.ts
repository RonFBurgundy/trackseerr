import { useCallback, useEffect, useState } from 'react';
import { errorMessage } from '@/services/apiClient';
import { getDownloadClientRoots } from '@/services/settingsService';
import type { DownloadClientRoots } from '@/types/models';

export interface UseDownloadClientRootsReturn {
  clients: DownloadClientRoots[];
  loading: boolean;
  /** Last load failure, or null. */
  error: string | null;
  /** Re-asks every download client for its folders (bypasses the server cache). */
  refresh: () => Promise<void>;
}

/** The completed-download folders each download client reports, read-only. */
export function useDownloadClientRoots(): UseDownloadClientRootsReturn {
  const [clients, setClients] = useState<DownloadClientRoots[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (force: boolean): Promise<void> => {
    setLoading(true);
    try {
      setClients(await getDownloadClientRoots(force));
      setError(null);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not load download folders'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  const refresh = useCallback((): Promise<void> => load(true), [load]);

  return { clients, loading, error, refresh };
}
