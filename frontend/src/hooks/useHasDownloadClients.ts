import { useEffect, useState } from 'react';
import { getClientSettings } from '@/services/settingsService';

/**
 * Whether Trackseerr has its own download clients configured. `null` while loading or when the lookup failed, so
 * callers hide seed-cleanup UI only on a confirmed `false` (e.g. Lidarr mode, where Lidarr owns the clients).
 */
export function useHasDownloadClients(enabled: boolean = true): boolean | null {
  const [has, setHas] = useState<boolean | null>(null);
  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    getClientSettings()
      .then((clients) => {
        if (!cancelled) setHas(clients.length > 0);
      })
      .catch((err: unknown) => {
        console.warn('Download client lookup failed; seed cleanup stays visible', err);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return has;
}
