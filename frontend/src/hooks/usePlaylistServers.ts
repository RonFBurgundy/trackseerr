import { useCallback, useEffect, useState } from 'react';
import { listMediaServers } from '@/services/mediaServerService';
import type { MediaServerItem } from '@/types/mediaServer';

export interface PlaylistServersState {
  playlistServers: MediaServerItem[];
  reload: () => Promise<void>;
}

/** Loads enabled media servers configured to receive playlists (admin-only). */
export function usePlaylistServers(isAdmin: boolean): PlaylistServersState {
  const [playlistServers, setPlaylistServers] = useState<MediaServerItem[]>([]);

  const reload = useCallback(async () => {
    if (!isAdmin) {
      setPlaylistServers([]);
      return;
    }
    try {
      const rows = await listMediaServers();
      setPlaylistServers(rows.filter((s) => s.enabled && s.use_playlists));
    } catch {
      setPlaylistServers([]);
    }
  }, [isAdmin]);

  useEffect(() => {
    let cancelled = false;
    if (!isAdmin) {
      setPlaylistServers([]);
      return () => {
        cancelled = true;
      };
    }
    listMediaServers()
      .then((rows) => {
        if (!cancelled) {
          setPlaylistServers(rows.filter((s) => s.enabled && s.use_playlists));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setPlaylistServers([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [isAdmin]);

  return { playlistServers, reload };
}
