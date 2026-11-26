import { useCallback, useEffect, useState } from 'react';
import {
  linkMediaServerUser,
  listMediaServerLinks,
  unlinkMediaServerUser,
} from '@/services/mediaServerService';
import { errorMessage } from '@/services/apiClient';
import type { MediaServerLinksGroup } from '@/types/mediaServer';

export interface UseServerUserLinksReturn {
  groups: MediaServerLinksGroup[];
  isLoading: boolean;
  error: string | null;
  reload: () => Promise<void>;
  linkUser: (serverId: string, userId: string, serverUsername: string) => Promise<void>;
  unlinkUser: (serverId: string, userId: string) => Promise<void>;
}

/** Loads per-server user links and provides manual link/unlink actions (admin-only). */
export function useServerUserLinks(enabled: boolean): UseServerUserLinksReturn {
  const [groups, setGroups] = useState<MediaServerLinksGroup[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    if (!enabled) {
      setGroups([]);
      setError(null);
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      const data = await listMediaServerLinks();
      setGroups(data);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to load server user links'));
    } finally {
      setIsLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const linkUser = useCallback(
    async (serverId: string, userId: string, serverUsername: string) => {
      await linkMediaServerUser(serverId, userId, serverUsername);
      await reload();
    },
    [reload]
  );

  const unlinkUser = useCallback(
    async (serverId: string, userId: string) => {
      await unlinkMediaServerUser(serverId, userId);
      await reload();
    },
    [reload]
  );

  return { groups, isLoading, error, reload, linkUser, unlinkUser };
}
