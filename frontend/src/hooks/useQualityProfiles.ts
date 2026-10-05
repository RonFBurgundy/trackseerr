import { useCallback } from 'react';
import type { QualityProfile, QualityProfileInput } from '@/types/qualityProfiles';
import {
  copyQualityProfile,
  deleteQualityProfile,
  listQualityProfiles,
  saveQualityProfile,
  setDefaultQualityProfile,
} from '@/services/qualityProfileService';
import { errorMessage } from '@/services/apiClient';
import { useLoadedList } from './useLoadedList';

export interface UseQualityProfilesReturn {
  profiles: QualityProfile[];
  loading: boolean;
  /** Creates (no id) or updates a profile; resolves an inline error message, or null on success. */
  save: (input: QualityProfileInput) => Promise<string | null>;
  copy: (id: string) => Promise<void>;
  remove: (id: string) => Promise<void>;
  makeDefault: (id: string) => Promise<void>;
  reload: () => Promise<void>;
}

type Toast = (msg: string, tone?: 'ok' | 'error') => void;

/** Native quality profiles (v2 shape) and their CRUD actions. */
export function useQualityProfiles(enabled: boolean, onToast: Toast): UseQualityProfilesReturn {
  const { items, loading, reload } = useLoadedList<QualityProfile>(
    enabled,
    listQualityProfiles,
    'Failed to load quality profiles',
    onToast
  );

  const save = useCallback(
    async (input: QualityProfileInput): Promise<string | null> => {
      try {
        await saveQualityProfile(input);
        await reload();
        onToast(input.id ? 'Quality profile saved' : 'Quality profile created');
        return null;
      } catch (err: unknown) {
        return errorMessage(err, 'Failed to save quality profile');
      }
    },
    [reload, onToast]
  );

  const run = useCallback(
    async (action: () => Promise<unknown>, done: string, failed: string): Promise<void> => {
      try {
        await action();
        await reload();
        onToast(done);
      } catch (err: unknown) {
        onToast(errorMessage(err, failed), 'error');
      }
    },
    [reload, onToast]
  );

  const copy = useCallback((id: string) => run(() => copyQualityProfile(id), 'Profile copied', 'Failed to copy profile'), [run]);
  const remove = useCallback((id: string) => run(() => deleteQualityProfile(id), 'Profile deleted', 'Failed to delete profile'), [run]);
  const makeDefault = useCallback(
    (id: string) => run(() => setDefaultQualityProfile(id), 'Default profile changed', 'Failed to set default profile'),
    [run]
  );

  return { profiles: items, loading, save, copy, remove, makeDefault, reload };
}
