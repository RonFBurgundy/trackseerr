import { useCallback, useState } from 'react';
import { errorMessage } from '@/services/apiClient';
import { searchAlbum, searchArtist } from '@/services/libraryService';

export interface UseLidarrSearchReturn {
  /** Key of the search in flight (`artist:<id>` / `album:<id>`), or null. */
  busyKey: string | null;
  searchArtist: (artistId: number | string) => Promise<void>;
  searchAlbum: (albumId: number | string) => Promise<void>;
}

/** Lidarr-mode "Search" actions with toast feedback; callers only render the buttons in Lidarr mode. */
export function useLidarrSearch(onToast: (msg: string, tone?: 'ok' | 'error') => void): UseLidarrSearchReturn {
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const run = useCallback(
    async (key: string, call: () => Promise<{ message?: string }>, okMessage: string, failMessage: string): Promise<void> => {
      setBusyKey(key);
      try {
        const res = await call();
        onToast(res.message ?? okMessage);
      } catch (err: unknown) {
        onToast(errorMessage(err, failMessage), 'error');
      } finally {
        setBusyKey((cur) => (cur === key ? null : cur));
      }
    },
    [onToast]
  );

  const searchArtistAction = useCallback(
    (artistId: number | string): Promise<void> =>
      run(`artist:${artistId}`, () => searchArtist(artistId), 'Artist search queued in Lidarr', 'Failed to search for artist'),
    [run]
  );
  const searchAlbumAction = useCallback(
    (albumId: number | string): Promise<void> =>
      run(`album:${albumId}`, () => searchAlbum(albumId), 'Album search queued in Lidarr', 'Failed to search for album'),
    [run]
  );

  return { busyKey, searchArtist: searchArtistAction, searchAlbum: searchAlbumAction };
}
