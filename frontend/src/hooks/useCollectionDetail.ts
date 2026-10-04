import { useCallback, useEffect, useState } from 'react';
import type { CollectionItem } from '@/types/models';
import { errorMessage } from '@/services/apiClient';
import { getCollectionDetail, removeAlbumFromCollection } from '@/services/libraryService';

export interface UseCollectionDetailReturn {
  collection: CollectionItem | null;
  loading: boolean;
  removeAlbum: (albumId: number | string) => Promise<void>;
}

/** Detail of one collection; idle while `collectionId` is null. */
export function useCollectionDetail(
  collectionId: string | null,
  onToast: (msg: string, tone?: 'ok' | 'error') => void,
  onChanged: () => void | Promise<void>
): UseCollectionDetailReturn {
  const [collection, setCollection] = useState<CollectionItem | null>(null);
  const [loading, setLoading] = useState<boolean>(false);

  useEffect(() => {
    setCollection(null);
    if (!collectionId) {
      setLoading(false);
      return undefined;
    }
    let cancelled = false;
    setLoading(true);
    getCollectionDetail(collectionId)
      .then((data) => {
        if (!cancelled) setCollection(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) onToast(errorMessage(err, 'Failed to load collection details'), 'error');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [collectionId, onToast]);

  const removeAlbum = useCallback(
    async (albumId: number | string): Promise<void> => {
      if (!collectionId) return;
      try {
        const ok = await removeAlbumFromCollection(collectionId, albumId);
        if (!ok) {
          onToast('Failed to remove album', 'error');
          return;
        }
        setCollection((prev) =>
          prev
            ? {
                ...prev,
                albums: prev.albums?.filter((a) => a.id !== albumId),
                album_count: Math.max(0, (prev.album_count || 1) - 1),
              }
            : prev
        );
        await onChanged();
        onToast('Album removed from collection');
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Error removing album from collection'), 'error');
      }
    },
    [collectionId, onToast, onChanged]
  );

  return { collection, loading, removeAlbum };
}
