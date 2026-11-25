import { useCallback, useEffect, useState } from 'react';
import { listMediaServers } from '@/services/mediaServerService';
import type { MediaServerItem } from '@/types/mediaServer';

export interface PlaylistServersState {
  playlistServers: MediaServerItem[];
  enabledServers: MediaServerItem[];
  reload: () => Promise<void>;
}

/** Loads enabled media servers configured to receive playlists (admin-only). */
export function usePlaylistServers(isAdmin: boolean): PlaylistServersState {
  const [playlistServers, setPlaylistServers] = useState<MediaServerItem[]>([]);
  const [enabledServers, setEnabledServers] = useState<MediaServerItem[]>([]);

  const reload = useCallback(async () => {
    if (!isAdmin) {
      setPlaylistServers([]);
      setEnabledServers([]);
      return;
    }
    try {
      const rows = await listMediaServers();
      const enabled = rows.filter((s) => s.enabled);
      setEnabledServers(enabled);
      setPlaylistServers(enabled.filter((s) => s.use_playlists));
    } catch {
      setPlaylistServers([]);
      setEnabledServers([]);
    }
  }, [isAdmin]);

  useEffect(() => {
    let cancelled = false;
    if (!isAdmin) {
      setPlaylistServers([]);
      setEnabledServers([]);
      return () => {
        cancelled = true;
      };
    }
    listMediaServers()
      .then((rows) => {
        if (!cancelled) {
          const enabled = rows.filter((s) => s.enabled);
          setEnabledServers(enabled);
          setPlaylistServers(enabled.filter((s) => s.use_playlists));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setPlaylistServers([]);
          setEnabledServers([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [isAdmin]);

  return { playlistServers, enabledServers, reload };
}
