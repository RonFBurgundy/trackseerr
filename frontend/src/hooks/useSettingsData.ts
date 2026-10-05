import { useState, useEffect, useCallback } from 'react';
import type {
  GeneralSettings,
  DownloadClientItem,
  IndexerItem,
  MediaManagementSettings,
  LidarrSettings,
} from '@/types/models';
import {
  getGeneralSettings,
  getMediaManagementSettings,
  getLidarrSettings,
  getClientSettings,
  getIndexerSettings,
} from '@/services/settingsService';

export interface UseSettingsDataReturn {
  isLoading: boolean;
  general: GeneralSettings | null;
  setGeneral: React.Dispatch<React.SetStateAction<GeneralSettings | null>>;
  media: MediaManagementSettings | null;
  setMedia: React.Dispatch<React.SetStateAction<MediaManagementSettings | null>>;
  lidarr: LidarrSettings | null;
  setLidarr: React.Dispatch<React.SetStateAction<LidarrSettings | null>>;
  clients: DownloadClientItem[];
  indexers: IndexerItem[];
  reload: () => Promise<void>;
}

/** Loads every admin settings document once; individual panels save through their own services and call `reload`. */
export function useSettingsData(enabled: boolean): UseSettingsDataReturn {
  const [isLoading, setIsLoading] = useState<boolean>(enabled);
  const [general, setGeneral] = useState<GeneralSettings | null>(null);
  const [media, setMedia] = useState<MediaManagementSettings | null>(null);
  const [lidarr, setLidarr] = useState<LidarrSettings | null>(null);
  const [clients, setClients] = useState<DownloadClientItem[]>([]);
  const [indexers, setIndexers] = useState<IndexerItem[]>([]);

  const reload = useCallback(async () => {
    if (!enabled) return;
    setIsLoading(true);
    try {
      const [gen, med, lid, cli, idx] = await Promise.all([
        getGeneralSettings().catch(() => null),
        getMediaManagementSettings().catch(() => null),
        getLidarrSettings().catch(() => null),
        getClientSettings().catch(() => []),
        getIndexerSettings().catch(() => []),
      ]);
      if (gen) setGeneral(gen);
      if (med) setMedia(med);
      if (lid) setLidarr(lid);
      setClients(cli);
      setIndexers(idx);
    } finally {
      setIsLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    if (enabled) void reload();
    else setIsLoading(false);
  }, [enabled, reload]);

  return { isLoading, general, setGeneral, media, setMedia, lidarr, setLidarr, clients, indexers, reload };
}
