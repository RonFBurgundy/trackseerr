import { useCallback, useState } from 'react';
import { setArtistTags } from '@/services/tagService';
import { errorMessage } from '@/services/apiClient';

export interface UseArtistTagsReturn {
  saving: boolean;
  /** Replaces the artist's tags. Resolves the saved ids, or null (after an error toast) on failure. */
  save: (tagIds: number[]) => Promise<number[] | null>;
}

/** Saves one artist's tag set via `PUT /api/library/artists/{id}/tags`. */
export function useArtistTags(
  artistId: number | string,
  onToast: (msg: string, tone?: 'ok' | 'error') => void
): UseArtistTagsReturn {
  const [saving, setSaving] = useState<boolean>(false);

  const save = useCallback(
    async (tagIds: number[]): Promise<number[] | null> => {
      setSaving(true);
      try {
        const res = await setArtistTags(artistId, tagIds);
        onToast('Tags saved');
        return res.tags;
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to save tags'), 'error');
        return null;
      } finally {
        setSaving(false);
      }
    },
    [artistId, onToast]
  );

  return { saving, save };
}
