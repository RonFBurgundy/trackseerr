import { useState, useEffect, useCallback, useRef } from 'react';
import type { Schema } from '@/types/apiSchema';
import type { MediaServerKind, ServerPlaylistSummary, ServerPlaylistTrack } from '@/types/mediaServer';
import {
  adoptServerPlaylist,
  getServerPlaylistItems,
  listServerPlaylists,
} from '@/services/serverPlaylistService';

export interface UseServerPlaylistsReturn {
  selectedUser: string | undefined;
  setSelectedUser: (username: string) => void;
  playlists: ServerPlaylistSummary[];
  isLoading: boolean;
  error: string | null;
  clearError: () => void;
  refresh: () => Promise<void>;

  selected: ServerPlaylistSummary | null;
  items: ServerPlaylistTrack[];
  isItemsLoading: boolean;
  isMutating: boolean;
  selectPlaylist: (playlist: ServerPlaylistSummary | null) => Promise<void>;
  adopt: (playlist: ServerPlaylistSummary) => Promise<Schema<'PlaylistRecord'> | null>;
}

function errMsg(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

export function useServerPlaylists(
  serverId: string,
  kind: MediaServerKind
): UseServerPlaylistsReturn {
  const [selectedUser, setSelectedUserState] = useState<string | undefined>(undefined);
  const [playlists, setPlaylists] = useState<ServerPlaylistSummary[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [selected, setSelected] = useState<ServerPlaylistSummary | null>(null);
  const [items, setItems] = useState<ServerPlaylistTrack[]>([]);
  const [isItemsLoading, setIsItemsLoading] = useState<boolean>(false);
  const [isMutating, setIsMutating] = useState<boolean>(false);

  // Guards against out-of-order responses when the server or user changes quickly.
  const listReq = useRef(0);
  const itemsReq = useRef(0);

  const effectiveUser = kind === 'jellyfin' ? selectedUser : undefined;

  const clearError = useCallback(() => setError(null), []);

  const refresh = useCallback(async () => {
    const req = ++listReq.current;
    setIsLoading(true);
    try {
      const list = await listServerPlaylists(serverId, effectiveUser);
      if (req !== listReq.current) return;
      setPlaylists(list);
      setError(null);
    } catch (err: unknown) {
      if (req !== listReq.current) return;
      setError(errMsg(err, 'Failed to load server playlists'));
    } finally {
      if (req === listReq.current) setIsLoading(false);
    }
  }, [serverId, effectiveUser]);

  useEffect(() => {
    setSelected(null);
    setItems([]);
    setPlaylists([]);
    void refresh();
  }, [refresh]);

  const setSelectedUser = useCallback((username: string) => {
    setSelected(null);
    setItems([]);
    setPlaylists([]);
    setSelectedUserState(username || undefined);
  }, []);

  const selectPlaylist = useCallback(
    async (playlist: ServerPlaylistSummary | null) => {
      setSelected(playlist);
      setItems([]);
      if (!playlist) return;
      const req = ++itemsReq.current;
      setIsItemsLoading(true);
      try {
        const list = await getServerPlaylistItems(serverId, playlist.id, effectiveUser);
        if (req !== itemsReq.current) return;
        setItems(list);
      } catch (err: unknown) {
        if (req !== itemsReq.current) return;
        setError(errMsg(err, 'Failed to load playlist tracks'));
      } finally {
        if (req === itemsReq.current) setIsItemsLoading(false);
      }
    },
    [serverId, effectiveUser]
  );

  const adopt = useCallback(
    async (playlist: ServerPlaylistSummary): Promise<Schema<'PlaylistRecord'> | null> => {
      setIsMutating(true);
      try {
        const created = await adoptServerPlaylist(serverId, playlist.id, effectiveUser);
        const linked: ServerPlaylistSummary = {
          ...playlist,
          adopted_playlist_id: created.id,
        };
        setPlaylists((prev) => prev.map((p) => (p.id === linked.id ? linked : p)));
        setSelected((prev) => (prev && prev.id === linked.id ? linked : prev));
        void refresh();
        return created;
      } catch (err: unknown) {
        setError(errMsg(err, 'Failed to adopt playlist'));
        return null;
      } finally {
        setIsMutating(false);
      }
    },
    [serverId, effectiveUser, refresh]
  );

  return {
    selectedUser,
    setSelectedUser,
    playlists,
    isLoading,
    error,
    clearError,
    refresh,
    selected,
    items,
    isItemsLoading,
    isMutating,
    selectPlaylist,
    adopt,
  };
}
