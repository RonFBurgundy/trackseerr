import { useCallback, useState } from 'react';
import type { AlbumItem, CollectionItem } from '@/types/models';
import { addAlbumToCollection, getCollections } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';

export interface UseAddToCollectionReturn {
  album: AlbumItem | null;
  collections: CollectionItem[];
  loading: boolean;
  open: (album: AlbumItem) => Promise<void>;
  close: () => void;
  add: (collectionId: string, collectionName: string) => Promise<void>;
}

/** State for the "Add to Collection" picker. `onAdded` runs after the server accepted the album. */
export function useAddToCollection(
  onToast: (msg: string, tone?: 'ok' | 'error') => void,
  onAdded: () => void | Promise<void>
): UseAddToCollectionReturn {
  const [album, setAlbum] = useState<AlbumItem | null>(null);
  const [collections, setCollections] = useState<CollectionItem[]>([]);
  const [loading, setLoading] = useState<boolean>(false);

  const open = useCallback(
    async (target: AlbumItem): Promise<void> => {
      setAlbum(target);
      setLoading(true);
      try {
        setCollections(await getCollections());
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to load collections'), 'error');
      } finally {
        setLoading(false);
      }
    },
    [onToast]
  );

  const close = useCallback((): void => setAlbum(null), []);

  const add = useCallback(
    async (collectionId: string, collectionName: string): Promise<void> => {
      if (!album) return;
      try {
        const ok = await addAlbumToCollection(collectionId, album.id);
        if (ok) {
          onToast(`Added "${album.title}" to ${collectionName}`);
          setAlbum(null);
          await onAdded();
        } else {
          onToast('Failed to add album to collection', 'error');
        }
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Error adding album to collection'), 'error');
      }
    },
    [album, onToast, onAdded]
  );

  return { album, collections, loading, open, close, add };
}
