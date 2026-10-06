import { useCallback, useEffect, useRef, useState } from 'react';
import type { Schema } from '@/types';
import { createTag, deleteTag, getTagUsage, listTags, renameTag } from '@/services/tagService';
import { errorMessage } from '@/services/apiClient';

export type TagMutationResult = { ok: true; tag: Schema<'TagOut'> } | { ok: false; error: string };

export interface UseTagsReturn {
  tags: Schema<'TagOut'>[];
  loading: boolean;
  /** Set when the list could not be loaded; cleared by the next successful load. */
  loadError: string | null;
  reload: () => Promise<void>;
  /** Creates a tag; the server's 409/422 detail comes back as `error`. The list is refreshed on success. */
  create: (label: string) => Promise<TagMutationResult>;
  rename: (id: number, label: string) => Promise<TagMutationResult>;
  /** Resolves an error message, or null on success. */
  remove: (id: number) => Promise<string | null>;
  usage: (id: number) => Promise<Schema<'TagUsage'> | null>;
}

/** The tag catalogue plus its mutations. Idle until `enabled`. Errors are returned to the caller, not toasted. */
export function useTags(enabled: boolean): UseTagsReturn {
  const [tags, setTags] = useState<Schema<'TagOut'>[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Request counter: only the latest reload may write state, so a slow earlier response cannot overwrite a newer list.
  const requestId = useRef<number>(0);

  const reload = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    const mine = ++requestId.current;
    setLoading(true);
    try {
      const fresh = await listTags();
      if (mine !== requestId.current) return;
      setTags(fresh);
      setLoadError(null);
    } catch (err: unknown) {
      if (mine !== requestId.current) return;
      setLoadError(errorMessage(err, 'Failed to load tags'));
    } finally {
      if (mine === requestId.current) setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const create = useCallback(
    async (label: string): Promise<TagMutationResult> => {
      try {
        const tag = await createTag(label);
        await reload();
        return { ok: true, tag };
      } catch (err: unknown) {
        return { ok: false, error: errorMessage(err, 'Failed to create tag') };
      }
    },
    [reload]
  );

  const rename = useCallback(
    async (id: number, label: string): Promise<TagMutationResult> => {
      try {
        const tag = await renameTag(id, label);
        await reload();
        return { ok: true, tag };
      } catch (err: unknown) {
        return { ok: false, error: errorMessage(err, 'Failed to rename tag') };
      }
    },
    [reload]
  );

  const remove = useCallback(
    async (id: number): Promise<string | null> => {
      try {
        await deleteTag(id);
        await reload();
        return null;
      } catch (err: unknown) {
        return errorMessage(err, 'Failed to delete tag');
      }
    },
    [reload]
  );

  const usage = useCallback(async (id: number): Promise<Schema<'TagUsage'> | null> => {
    try {
      return await getTagUsage(id);
    } catch (err: unknown) {
      setLoadError(errorMessage(err, 'Failed to load tag usage'));
      return null;
    }
  }, []);

  return { tags, loading, loadError, reload, create, rename, remove, usage };
}
