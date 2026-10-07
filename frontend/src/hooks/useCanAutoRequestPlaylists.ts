import { useEffect, useState } from 'react';
import { getCanAutoRequestPlaylists } from '@/services/listeningService';

/** Whether the signed-in user may let playlists acquire music automatically (admin or the permission bit). */
export function useCanAutoRequestPlaylists(enabled: boolean): boolean {
  const [allowed, setAllowed] = useState<boolean>(false);

  useEffect(() => {
    if (!enabled) {
      setAllowed(false);
      return;
    }
    let cancelled = false;
    getCanAutoRequestPlaylists()
      .then((value) => {
        if (!cancelled) setAllowed(value);
      })
      .catch(() => {
        // Unknown means not allowed: the server enforces the permission regardless.
        if (!cancelled) setAllowed(false);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  return allowed;
}
