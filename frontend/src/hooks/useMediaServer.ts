import { useEffect, useState } from 'react';
import type { MediaServerCapabilities, MediaServerStatus, MediaServerType } from '@/types/mediaServer';
import { getMediaServerStatus } from '@/services/mediaServerService';

/** What an older server (no media-server endpoint) implies: Plex, with every feature on, as before. */
const LEGACY_CAPABILITIES: MediaServerCapabilities = {
  playlists: true,
  users: true,
  mixes: true,
  library_refresh: true,
  file_paths: true,
};

const MEDIA_SERVER_LABELS: Record<MediaServerType, string> = {
  plex: 'Plex',
  subsonic: 'Subsonic server',
  jellyfin: 'Jellyfin server',
  none: 'media server',
};

export interface UseMediaServerReturn {
  /** False until the first answer (or failure) arrives; gate sign-in choices on it to avoid flicker. */
  isLoaded: boolean;
  status: MediaServerStatus | null;
  type: MediaServerType;
  /** True when a media server is configured, so playlist push makes sense. */
  hasMediaServer: boolean;
  /** True only for Plex: Plex sign-in and Plex-only features (Plex playlists, home users) depend on it. */
  isPlex: boolean;
  /** Human name for generic copy ("Plex", "Subsonic server", "Jellyfin server", "media server"). */
  label: string;
  capabilities: MediaServerCapabilities;
}

/** Loads the active media server once; features hide themselves when none is connected. */
export function useMediaServer(): UseMediaServerReturn {
  const [status, setStatus] = useState<MediaServerStatus | null>(null);
  const [isLoaded, setIsLoaded] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    void getMediaServerStatus().then((next) => {
      if (cancelled) return;
      setStatus(next);
      setIsLoaded(true);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const type: MediaServerType = status?.type ?? 'plex';
  return {
    isLoaded,
    status,
    type,
    hasMediaServer: type !== 'none',
    isPlex: type === 'plex',
    label: MEDIA_SERVER_LABELS[type],
    capabilities: status?.capabilities ?? LEGACY_CAPABILITIES,
  };
}
