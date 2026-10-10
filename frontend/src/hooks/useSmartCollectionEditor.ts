import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Schema } from '@/types/apiSchema';
import type { LibraryFilters } from '@/types/libraryFilters';
import { EMPTY_LIBRARY_FILTERS, libraryFiltersToRules, rulesToLibraryFilters } from '@/lib/libraryFilters';
import { useDebouncedValue } from '@/hooks/useDebouncedValue';
import {
  createSmartCollection,
  getSmartCollection,
  previewSmartCollection,
  updateSmartCollection,
} from '@/services/smartCollectionService';
import { togglePlaylistActive, toggleUserTarget } from '@/services/playlistService';
import { ApiError, errorMessage } from '@/services/apiClient';

export interface UseSmartCollectionEditorOptions {
  collectionId?: string;
  initialFilters?: LibraryFilters;
  defaultTargets?: string[];
  enabled?: boolean;
}

export interface SmartCollectionPreviewState {
  count: number;
  tracks: Schema<'SmartPreviewTrack'>[];
  loading: boolean;
}

export interface UseSmartCollectionEditorReturn {
  name: string;
  setName: (name: string) => void;
  description: string;
  setDescription: (description: string) => void;
  filters: LibraryFilters;
  setFilters: (filters: LibraryFilters) => void;
  sort: Schema<'SmartRulesBody'>['sort'];
  setSort: (sort: Schema<'SmartRulesBody'>['sort']) => void;
  limit: number;
  setLimit: (limit: number) => void;
  targets: string[];
  setTargets: (targets: string[]) => void;
  autoUpdate: boolean;
  setAutoUpdate: (autoUpdate: boolean) => void;
  load: (id: string) => Promise<void>;
  save: () => Promise<Schema<'PlaylistImportResponse'> | Schema<'SmartCollectionRecord'>>;
  error: string | null;
  setError: (error: string | null) => void;
  saving: boolean;
  preview: SmartCollectionPreviewState;
  reset: () => void;
}

interface LoadedState {
  id: string;
  enabled: boolean;
  targets: string[];
}

export function useSmartCollectionEditor({
  collectionId,
  initialFilters,
  defaultTargets,
  enabled = true,
}: UseSmartCollectionEditorOptions = {}): UseSmartCollectionEditorReturn {
  const [name, setName] = useState<string>('');
  const [description, setDescription] = useState<string>('');
  const [filters, setFilters] = useState<LibraryFilters>(initialFilters ?? EMPTY_LIBRARY_FILTERS);
  const [sort, setSort] = useState<Schema<'SmartRulesBody'>['sort']>('random');
  const [limit, setLimit] = useState<number>(100);
  const [targets, setTargets] = useState<string[]>(defaultTargets ?? []);
  const [autoUpdate, setAutoUpdate] = useState<boolean>(false);

  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const loadedStateRef = useRef<LoadedState | null>(null);

  const [previewData, setPreviewData] = useState<{
    count: number;
    tracks: Schema<'SmartPreviewTrack'>[];
  }>({ count: 0, tracks: [] });
  const [previewLoading, setPreviewLoading] = useState<boolean>(false);

  const reset = useCallback(() => {
    setName('');
    setDescription('');
    setFilters(initialFilters ?? EMPTY_LIBRARY_FILTERS);
    setSort('random');
    setLimit(100);
    setTargets(defaultTargets ?? []);
    setAutoUpdate(false);
    setError(null);
    loadedStateRef.current = null;
    setPreviewData({ count: 0, tracks: [] });
    setPreviewLoading(false);
  }, [initialFilters, defaultTargets]);

  const load = useCallback(async (id: string) => {
    setError(null);
    try {
      const record = await getSmartCollection(id);
      setName(record.name);
      setDescription(record.description ?? '');
      const parsed = rulesToLibraryFilters(record.rules);
      setFilters(parsed.filters);
      setSort(parsed.sort);
      setLimit(parsed.limit);
      setTargets(record.targets ? [...record.targets] : []);
      setAutoUpdate(record.enabled);
      loadedStateRef.current = {
        id: record.id,
        enabled: record.enabled,
        targets: record.targets ? [...record.targets] : [],
      };
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to load smart collection'));
    }
  }, []);

  useEffect(() => {
    if (enabled && collectionId) {
      void load(collectionId);
    } else if (enabled && !collectionId) {
      if (initialFilters) {
        setFilters(initialFilters);
      }
      if (defaultTargets) {
        setTargets(defaultTargets);
      }
    }
  }, [enabled, collectionId, load, initialFilters, defaultTargets]);

  // Debounced preview
  const previewPayload = useMemo(
    () => ({ filters, sort, limit }),
    [filters, sort, limit]
  );
  const debouncedPayload = useDebouncedValue(previewPayload, 400);

  const isDebouncing = previewPayload !== debouncedPayload;

  useEffect(() => {
    if (!enabled) return;

    const controller = new AbortController();
    setPreviewLoading(true);

    const rules = libraryFiltersToRules(
      debouncedPayload.filters,
      debouncedPayload.sort,
      debouncedPayload.limit
    );

    previewSmartCollection(rules, controller.signal)
      .then((res) => {
        setPreviewData({
          count: res.track_count,
          tracks: res.tracks,
        });
        setPreviewLoading(false);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setPreviewLoading(false);
        if (err instanceof ApiError) {
          // If 400 (e.g. no tracks matched), count is 0
          setPreviewData({ count: 0, tracks: [] });
        }
      });

    return () => {
      controller.abort();
    };
  }, [debouncedPayload, enabled]);

  const save = useCallback(async (): Promise<
    Schema<'PlaylistImportResponse'> | Schema<'SmartCollectionRecord'>
  > => {
    setSaving(true);
    setError(null);
    try {
      const rules = libraryFiltersToRules(filters, sort, limit);
      const activeId = collectionId ?? loadedStateRef.current?.id;

      if (activeId) {
        const updated = await updateSmartCollection(activeId, {
          name: name.trim(),
          description: description.trim(),
          rules,
        });

        const initial = loadedStateRef.current;
        if (initial) {
          if (autoUpdate !== initial.enabled) {
            await togglePlaylistActive(activeId, autoUpdate);
          }
          const prevTargets = initial.targets;
          const targetsChanged =
            targets.length !== prevTargets.length ||
            targets.some((t) => !prevTargets.includes(t)) ||
            prevTargets.some((t) => !targets.includes(t));
          if (targetsChanged) {
            await toggleUserTarget(activeId, targets);
          }
        }

        return updated;
      } else {
        const created = await createSmartCollection({
          name: name.trim(),
          description: description.trim(),
          rules,
          targets,
          keep_in_sync: autoUpdate,
        });
        return created;
      }
    } catch (err: unknown) {
      const msg = errorMessage(err, 'Failed to save smart collection');
      setError(msg);
      throw err;
    } finally {
      setSaving(false);
    }
  }, [collectionId, name, description, filters, sort, limit, targets, autoUpdate]);

  return {
    name,
    setName,
    description,
    setDescription,
    filters,
    setFilters,
    sort,
    setSort,
    limit,
    setLimit,
    targets,
    setTargets,
    autoUpdate,
    setAutoUpdate,
    load,
    save,
    error,
    setError,
    saving,
    preview: {
      count: previewData.count,
      tracks: previewData.tracks,
      loading: isDebouncing || previewLoading,
    },
    reset,
  };
}
