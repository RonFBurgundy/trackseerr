import { useCallback, useEffect, useState } from 'react';
import type { AccountMediaServer } from '@/types/account';
import { listServerLogins, removeServerLogin, saveServerLogin } from '@/services/accountService';
import { errorMessage } from '@/services/apiClient';

export interface UseServerLoginsReturn {
  servers: AccountMediaServer[];
  isLoading: boolean;
  isSaving: boolean;
  error: string | null;
  save: (serverId: string, username: string, password: string) => Promise<AccountMediaServer | null>;
  remove: (serverId: string) => Promise<boolean>;
}

export function useServerLogins(): UseServerLoginsReturn {
  const [servers, setServers] = useState<AccountMediaServer[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setIsLoading(true);
    listServerLogins()
      .then((items) => {
        if (!active) return;
        setServers(items);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setError(errorMessage(err, 'Failed to load media server logins'));
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const save = useCallback(
    async (serverId: string, username: string, password: string): Promise<AccountMediaServer | null> => {
      setIsSaving(true);
      setError(null);
      try {
        const updated = await saveServerLogin(serverId, username, password);
        setServers((prev) => prev.map((s) => (s.server_id === serverId ? updated : s)));
        return updated;
      } catch (err: unknown) {
        const msg = errorMessage(err, 'Failed to save media server login');
        setError(msg);
        throw new Error(msg);
      } finally {
        setIsSaving(false);
      }
    },
    []
  );

  const remove = useCallback(async (serverId: string): Promise<boolean> => {
    setIsSaving(true);
    setError(null);
    try {
      await removeServerLogin(serverId);
      setServers((prev) =>
        prev.map((s) => (s.server_id === serverId ? { ...s, username: '', saved: false } : s))
      );
      return true;
    } catch (err: unknown) {
      const msg = errorMessage(err, 'Failed to remove media server login');
      setError(msg);
      throw new Error(msg);
    } finally {
      setIsSaving(false);
    }
  }, []);

  return {
    servers,
    isLoading,
    isSaving,
    error,
    save,
    remove,
  };
}
