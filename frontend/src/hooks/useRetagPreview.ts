import { useCallback, useEffect, useState } from 'react';
import {
  getRetagPreview,
  applyRetag,
  type RetagPreviewItem,
  type RetagApplyResponse,
} from '@/services/retagService';
import { errorMessage } from '@/services/apiClient';

export interface UseRetagPreviewOptions {
  artistId?: string | null;
  albumId?: string | null;
  enabled?: boolean;
}

export interface UseRetagPreviewReturn {
  items: RetagPreviewItem[];
  loading: boolean;
  applying: boolean;
  error: string | null;
  selectedIds: Set<string>;
  embedArt: boolean;
  setEmbedArt: React.Dispatch<React.SetStateAction<boolean>>;
  toggleSelect: (fileId: string) => void;
  selectAll: () => void;
  selectNone: () => void;
  setSelectedIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  apply: () => Promise<RetagApplyResponse | null>;
  result: RetagApplyResponse | null;
  reload: () => Promise<void>;
  resetResult: () => void;
}

export function useRetagPreview({
  artistId,
  albumId,
  enabled = true,
}: UseRetagPreviewOptions): UseRetagPreviewReturn {
  const [items, setItems] = useState<RetagPreviewItem[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [applying, setApplying] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [embedArt, setEmbedArt] = useState<boolean>(false);
  const [result, setResult] = useState<RetagApplyResponse | null>(null);

  const reload = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await getRetagPreview({
        artist_id: artistId ?? undefined,
        album_id: albumId ?? undefined,
        limit: 200,
        offset: 0,
      });
      setItems(data);
      // Select non-skipped files by default
      const selectable = data.filter((item) => !item.skipped_reason);
      setSelectedIds(new Set(selectable.map((item) => item.file_id)));
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to generate retag preview'));
      setItems([]);
      setSelectedIds(new Set());
    } finally {
      setLoading(false);
    }
  }, [artistId, albumId, enabled]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const toggleSelect = useCallback((fileId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(fileId)) {
        next.delete(fileId);
      } else {
        next.add(fileId);
      }
      return next;
    });
  }, []);

  const selectAll = useCallback(() => {
    const selectable = items.filter((item) => !item.skipped_reason);
    setSelectedIds(new Set(selectable.map((item) => item.file_id)));
  }, [items]);

  const selectNone = useCallback(() => {
    setSelectedIds(new Set());
  }, []);

  const apply = useCallback(async (): Promise<RetagApplyResponse | null> => {
    if (selectedIds.size === 0) return null;
    setApplying(true);
    setError(null);
    try {
      const res = await applyRetag({
        file_ids: Array.from(selectedIds),
        embed_art: embedArt,
      });
      setResult(res);
      return res;
    } catch (err: unknown) {
      const msg = errorMessage(err, 'Failed to apply bulk retag');
      setError(msg);
      return null;
    } finally {
      setApplying(false);
    }
  }, [selectedIds, embedArt]);

  const resetResult = useCallback(() => {
    setResult(null);
  }, []);

  return {
    items,
    loading,
    applying,
    error,
    selectedIds,
    embedArt,
    setEmbedArt,
    toggleSelect,
    selectAll,
    selectNone,
    setSelectedIds,
    apply,
    result,
    reload,
    resetResult,
  };
}
