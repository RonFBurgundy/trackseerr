import { useCallback, useState } from 'react';
import { bulkEditTracks } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';

export interface UseTrackBulkEditReturn {
  busy: boolean;
  /** Resolves true when the server applied the change. */
  apply: (trackIds: ReadonlyArray<string | number>, monitored: boolean) => Promise<boolean>;
}

/** Native-library bulk monitor/unmonitor for tracks (the route answers 409 in Lidarr mode). */
export function useTrackBulkEdit(onToast: (msg: string, tone?: 'ok' | 'error') => void): UseTrackBulkEditReturn {
  const [busy, setBusy] = useState<boolean>(false);

  const apply = useCallback(
    async (trackIds: ReadonlyArray<string | number>, monitored: boolean): Promise<boolean> => {
      if (trackIds.length === 0) return false;
      setBusy(true);
      try {
        const res = await bulkEditTracks({ track_ids: trackIds.map(String), monitored });
        onToast(
          `${res.tracks_updated.toLocaleString()} ${res.tracks_updated === 1 ? 'track' : 'tracks'} ${monitored ? 'monitored' : 'unmonitored'}`
        );
        return true;
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Bulk edit failed'), 'error');
        return false;
      } finally {
        setBusy(false);
      }
    },
    [onToast]
  );

  return { busy, apply };
}
