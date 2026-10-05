import { useCallback, useEffect, useState } from 'react';
import type { ReleaseProfile, ReleaseProfileInput, ReleaseProfilePreview } from '@/types/releaseProfiles';
import {
  createReleaseProfile,
  deleteReleaseProfile,
  listReleaseProfiles,
  previewReleaseProfile,
  updateReleaseProfile,
} from '@/services/releaseProfileService';
import { errorMessage } from '@/services/apiClient';

export interface UseReleaseProfilesReturn {
  profiles: ReleaseProfile[];
  loading: boolean;
  /** Creates (no id) or updates a profile; resolves true on success. */
  save: (id: number | null, input: ReleaseProfileInput) => Promise<boolean>;
  /** Deletes a profile; resolves the number of artists that fell back to no profile, or null on failure. */
  remove: (id: number) => Promise<number | null>;
  reload: () => Promise<void>;
}

/** Release profile list and CRUD (native mode). Fetches while `enabled`; errors surface through `onToast`. */
export function useReleaseProfiles(
  enabled: boolean,
  onToast: (msg: string, tone?: 'ok' | 'error') => void
): UseReleaseProfilesReturn {
  const [profiles, setProfiles] = useState<ReleaseProfile[]>([]);
  const [loading, setLoading] = useState<boolean>(false);

  const reload = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    setLoading(true);
    try {
      setProfiles((await listReleaseProfiles()).profiles);
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to load release profiles'), 'error');
    } finally {
      setLoading(false);
    }
  }, [enabled, onToast]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const save = useCallback(
    async (id: number | null, input: ReleaseProfileInput): Promise<boolean> => {
      try {
        if (id === null) await createReleaseProfile(input);
        else await updateReleaseProfile(id, input);
        await reload();
        onToast(id === null ? 'Release profile created' : 'Release profile saved');
        return true;
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to save release profile'), 'error');
        return false;
      }
    },
    [reload, onToast]
  );

  const remove = useCallback(
    async (id: number): Promise<number | null> => {
      try {
        const res = await deleteReleaseProfile(id);
        await reload();
        onToast(
          res.artists_cleared > 0
            ? `Release profile deleted; ${res.artists_cleared} ${res.artists_cleared === 1 ? 'artist' : 'artists'} now have no profile`
            : 'Release profile deleted'
        );
        return res.artists_cleared;
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to delete release profile'), 'error');
        return null;
      }
    },
    [reload, onToast]
  );

  return { profiles, loading, save, remove, reload };
}

export interface UseReleaseProfilePreviewReturn {
  preview: ReleaseProfilePreview | null;
}

/** `{matching, total}` for an artist under a profile; null while loading or when no profile is chosen. */
export function useReleaseProfilePreview(
  artistId: number | string,
  profileId: number | null,
  refreshKey: number
): UseReleaseProfilePreviewReturn {
  const [preview, setPreview] = useState<ReleaseProfilePreview | null>(null);

  useEffect(() => {
    if (profileId === null) {
      setPreview(null);
      return;
    }
    const controller = new AbortController();
    previewReleaseProfile(artistId, profileId, controller.signal)
      .then((p) => {
        if (!controller.signal.aborted) setPreview(p);
      })
      .catch(() => {
        // The hint is advisory; a failed preview simply hides it.
        if (!controller.signal.aborted) setPreview(null);
      });
    return () => controller.abort();
  }, [artistId, profileId, refreshKey]);

  return { preview };
}

/** One-shot dry run for a chosen profile (null = none); resolves null when the preview cannot be fetched. */
export function useReleaseProfileDryRun(
  artistId: number | string,
  onToast: (msg: string, tone?: 'ok' | 'error') => void
): (profileId: number | null) => Promise<ReleaseProfilePreview | null> {
  return useCallback(
    async (profileId: number | null): Promise<ReleaseProfilePreview | null> => {
      try {
        return await previewReleaseProfile(artistId, profileId);
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to preview release profile'), 'error');
        return null;
      }
    },
    [artistId, onToast]
  );
}
