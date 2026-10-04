import { useCallback, useState } from 'react';
import { bulkEditAlbums } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';

export interface UseAlbumBulkEditReturn {
  busy: boolean;
  /** Resolves true when the server applied the change. */
  apply: (albumIds: ReadonlyArray<string | number>, monitored: boolean) => Promise<boolean>;
}

export function useAlbumBulkEdit(onToast: (msg: string, tone?: 'ok' | 'error') => void): UseAlbumBulkEditReturn {
  const [busy, setBusy] = useState<boolean>(false);

  const apply = useCallback(
    async (albumIds: ReadonlyArray<string | number>, monitored: boolean): Promise<boolean> => {
      if (albumIds.length === 0) return false;
      setBusy(true);
      try {
        const res = await bulkEditAlbums({ album_ids: albumIds.map(String), monitored });
        onToast(`${res.albums_updated.toLocaleString()} ${res.albums_updated === 1 ? 'album' : 'albums'} ${monitored ? 'monitored' : 'unmonitored'}`);
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
