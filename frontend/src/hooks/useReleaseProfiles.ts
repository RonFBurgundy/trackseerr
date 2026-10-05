import { useCallback } from 'react';
import type { ReleaseProfile, ReleaseProfileInput } from '@/types/releaseProfiles';
import {
  createReleaseProfile,
  deleteReleaseProfile,
  listReleaseProfiles,
  updateReleaseProfile,
} from '@/services/releaseProfileService';
import { errorMessage } from '@/services/apiClient';
import { useLoadedList } from './useLoadedList';

export interface UseReleaseProfilesReturn {
  profiles: ReleaseProfile[];
  loading: boolean;
  /** Creates (null id) or updates; resolves the backend's validation message for inline display, or null on success. */
  save: (id: number | null, input: ReleaseProfileInput) => Promise<string | null>;
  remove: (id: number) => Promise<void>;
  toggleEnabled: (profile: ReleaseProfile, enabled: boolean) => Promise<void>;
}

type Toast = (msg: string, tone?: 'ok' | 'error') => void;

/** Term-based release profiles (required / ignored terms, indexer and quality-profile scope). */
export function useReleaseProfiles(enabled: boolean, onToast: Toast): UseReleaseProfilesReturn {
  const { items, loading, reload } = useLoadedList<ReleaseProfile>(
    enabled,
    listReleaseProfiles,
    'Failed to load release profiles',
    onToast
  );

  const save = useCallback(
    async (id: number | null, input: ReleaseProfileInput): Promise<string | null> => {
      try {
        if (id === null) await createReleaseProfile(input);
        else await updateReleaseProfile(id, input);
        await reload();
        onToast(id === null ? 'Release profile created' : 'Release profile saved');
        return null;
      } catch (err: unknown) {
        return errorMessage(err, 'Failed to save release profile');
      }
    },
    [reload, onToast]
  );

  const remove = useCallback(
    async (id: number): Promise<void> => {
      try {
        await deleteReleaseProfile(id);
        await reload();
        onToast('Release profile deleted');
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to delete release profile'), 'error');
      }
    },
    [reload, onToast]
  );

  const toggleEnabled = useCallback(
    async (profile: ReleaseProfile, next: boolean): Promise<void> => {
      const { id, ...rest } = profile;
      const err = await save(id, { ...rest, enabled: next });
      if (err) onToast(err, 'error');
    },
    [save, onToast]
  );

  return { profiles: items, loading, save, remove, toggleEnabled };
}
