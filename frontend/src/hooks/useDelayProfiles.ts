import { useCallback } from 'react';
import type { DelayProfile, DelayProfileInput, PendingRelease } from '@/types/delayProfiles';
import {
  createDelayProfile,
  deleteDelayProfile,
  dropPendingRelease,
  grabPendingRelease,
  listDelayProfiles,
  listPendingReleases,
  reorderDelayProfiles,
  updateDelayProfile,
} from '@/services/delayProfileService';
import { errorMessage } from '@/services/apiClient';
import { useLoadedList } from './useLoadedList';

type Toast = (msg: string, tone?: 'ok' | 'error') => void;

export interface UseDelayProfilesReturn {
  profiles: DelayProfile[];
  loading: boolean;
  /** Creates (null id) or updates; resolves an inline error message, or null on success. */
  save: (id: number | null, input: DelayProfileInput) => Promise<string | null>;
  remove: (id: number) => Promise<void>;
  /** Applies a new order of the non-default profiles (the default stays pinned last). */
  reorder: (orderedIds: number[]) => Promise<void>;
}

/** Delay profiles; the default (untagged) profile is always last and cannot be deleted. */
export function useDelayProfiles(enabled: boolean, onToast: Toast): UseDelayProfilesReturn {
  const { items, setItems, loading, reload } = useLoadedList<DelayProfile>(
    enabled,
    listDelayProfiles,
    'Failed to load delay profiles',
    onToast
  );

  const save = useCallback(
    async (id: number | null, input: DelayProfileInput): Promise<string | null> => {
      try {
        if (id === null) await createDelayProfile(input);
        else await updateDelayProfile(id, input);
        await reload();
        onToast(id === null ? 'Delay profile created' : 'Delay profile saved');
        return null;
      } catch (err: unknown) {
        return errorMessage(err, 'Failed to save delay profile');
      }
    },
    [reload, onToast]
  );

  const remove = useCallback(
    async (id: number): Promise<void> => {
      try {
        await deleteDelayProfile(id);
        await reload();
        onToast('Delay profile deleted');
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to delete delay profile'), 'error');
      }
    },
    [reload, onToast]
  );

  const reorder = useCallback(
    async (orderedIds: number[]): Promise<void> => {
      // Optimistic: show the new order immediately, then let the server's answer win.
      setItems((prev) => {
        const byId = new Map(prev.map((p) => [p.id, p]));
        const moved: DelayProfile[] = [];
        for (const id of orderedIds) {
          const p = byId.get(id);
          if (p) moved.push(p);
        }
        return [...moved, ...prev.filter((p) => !orderedIds.includes(p.id))];
      });
      try {
        await reorderDelayProfiles(orderedIds);
        await reload();
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to reorder delay profiles'), 'error');
        await reload();
      }
    },
    [setItems, reload, onToast]
  );

  return { profiles: items, loading, save, remove, reorder };
}

export interface UsePendingReleasesReturn {
  pending: PendingRelease[];
  loading: boolean;
  drop: (id: number) => Promise<void>;
  grabNow: (id: number) => Promise<void>;
  reload: () => Promise<void>;
}

/** Releases held back by a delay profile, with drop and grab-now. */
export function usePendingReleases(enabled: boolean, onToast: Toast): UsePendingReleasesReturn {
  const { items, loading, reload } = useLoadedList<PendingRelease>(
    enabled,
    listPendingReleases,
    'Failed to load pending releases',
    onToast
  );

  const act = useCallback(
    async (action: () => Promise<void>, done: string, failed: string): Promise<void> => {
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

  const drop = useCallback((id: number) => act(() => dropPendingRelease(id), 'Pending release dropped', 'Failed to drop release'), [act]);
  const grabNow = useCallback((id: number) => act(() => grabPendingRelease(id), 'Release grabbed', 'Failed to grab release'), [act]);

  return { pending: items, loading, drop, grabNow, reload };
}
