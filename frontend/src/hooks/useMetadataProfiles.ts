import { useCallback, useEffect, useState } from 'react';
import type { MetadataProfile, MetadataProfileInput, MetadataProfilePreview } from '@/types/metadataProfiles';
import {
  createMetadataProfile,
  deleteMetadataProfile,
  listMetadataProfiles,
  previewMetadataProfile,
  updateMetadataProfile,
} from '@/services/metadataProfileService';
import { errorMessage } from '@/services/apiClient';

export interface UseMetadataProfilesReturn {
  profiles: MetadataProfile[];
  loading: boolean;
  /** Creates (no id) or updates a profile; resolves true on success. */
  save: (id: number | null, input: MetadataProfileInput) => Promise<boolean>;
  /** Deletes a profile; resolves the number of artists that fell back to no profile, or null on failure. */
  remove: (id: number) => Promise<number | null>;
  reload: () => Promise<void>;
}

/** Metadata profile list and CRUD (native mode). Fetches while `enabled`; errors surface through `onToast`. */
export function useMetadataProfiles(
  enabled: boolean,
  onToast: (msg: string, tone?: 'ok' | 'error') => void
): UseMetadataProfilesReturn {
  const [profiles, setProfiles] = useState<MetadataProfile[]>([]);
  const [loading, setLoading] = useState<boolean>(false);

  const reload = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    setLoading(true);
    try {
      setProfiles((await listMetadataProfiles()).profiles);
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to load metadata profiles'), 'error');
    } finally {
      setLoading(false);
    }
  }, [enabled, onToast]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const save = useCallback(
    async (id: number | null, input: MetadataProfileInput): Promise<boolean> => {
      try {
        if (id === null) await createMetadataProfile(input);
        else await updateMetadataProfile(id, input);
        await reload();
        onToast(id === null ? 'Metadata profile created' : 'Metadata profile saved');
        return true;
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to save metadata profile'), 'error');
        return false;
      }
    },
    [reload, onToast]
  );

  const remove = useCallback(
    async (id: number): Promise<number | null> => {
      try {
        const res = await deleteMetadataProfile(id);
        await reload();
        onToast(
          res.artists_cleared > 0
            ? `Metadata profile deleted; ${res.artists_cleared} ${res.artists_cleared === 1 ? 'artist' : 'artists'} now have no profile`
            : 'Metadata profile deleted'
        );
        return res.artists_cleared;
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to delete metadata profile'), 'error');
        return null;
      }
    },
    [reload, onToast]
  );

  return { profiles, loading, save, remove, reload };
}

export interface UseMetadataProfilePreviewReturn {
  preview: MetadataProfilePreview | null;
}

/** `{matching, total}` for an artist under a profile; null while loading or when no profile is chosen. */
export function useMetadataProfilePreview(
  artistId: number | string,
  profileId: number | null,
  refreshKey: number
): UseMetadataProfilePreviewReturn {
  const [preview, setPreview] = useState<MetadataProfilePreview | null>(null);

  useEffect(() => {
    if (profileId === null) {
      setPreview(null);
      return;
    }
    const controller = new AbortController();
    previewMetadataProfile(artistId, profileId, controller.signal)
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
export function useMetadataProfileDryRun(
  artistId: number | string,
  onToast: (msg: string, tone?: 'ok' | 'error') => void
): (profileId: number | null) => Promise<MetadataProfilePreview | null> {
  return useCallback(
    async (profileId: number | null): Promise<MetadataProfilePreview | null> => {
      try {
        return await previewMetadataProfile(artistId, profileId);
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to preview metadata profile'), 'error');
        return null;
      }
    },
    [artistId, onToast]
  );
}
