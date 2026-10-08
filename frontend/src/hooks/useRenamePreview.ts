import { useCallback, useEffect, useState } from 'react';
import {
  getRenamePreview,
  applyRename,
  type RenamePreviewItem,
  type RenameApplyResponse,
} from '@/services/renameService';
import { errorMessage } from '@/services/apiClient';

export interface UseRenamePreviewOptions {
  artistId?: string | null;
  albumId?: string | null;
  enabled?: boolean;
}

export interface UseRenamePreviewReturn {
  items: RenamePreviewItem[];
  allPreviewItems: RenamePreviewItem[];
  loading: boolean;
  applying: boolean;
  error: string | null;
  selectedIds: Set<string>;
  toggleSelect: (fileId: string) => void;
  selectAll: () => void;
  selectNone: () => void;
  setSelectedIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  apply: () => Promise<RenameApplyResponse | null>;
  result: RenameApplyResponse | null;
  reload: () => Promise<void>;
  resetResult: () => void;
}

export function useRenamePreview({
  artistId,
  albumId,
  enabled = true,
}: UseRenamePreviewOptions): UseRenamePreviewReturn {
  const [allPreviewItems, setAllPreviewItems] = useState<RenamePreviewItem[]>([]);
  const [items, setItems] = useState<RenamePreviewItem[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [applying, setApplying] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<RenameApplyResponse | null>(null);

  const reload = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const data = await getRenamePreview({
        artist_id: artistId ?? undefined,
        album_id: albumId ?? undefined,
        limit: 500,
      });
      setAllPreviewItems(data);
      // Filter only files that would change
      const changing = data.filter((item) => item.needs_rename);
      setItems(changing);
      setSelectedIds(new Set(changing.map((item) => item.file_id)));
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to generate rename preview'));
      setAllPreviewItems([]);
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
    setSelectedIds(new Set(items.map((item) => item.file_id)));
  }, [items]);

  const selectNone = useCallback(() => {
    setSelectedIds(new Set());
  }, []);

  const apply = useCallback(async (): Promise<RenameApplyResponse | null> => {
    if (selectedIds.size === 0) return null;
    setApplying(true);
    setError(null);
    try {
      const res = await applyRename({
        file_ids: Array.from(selectedIds),
      });
      setResult(res);
      return res;
    } catch (err: unknown) {
      const msg = errorMessage(err, 'Failed to apply batch rename');
      setError(msg);
      return null;
    } finally {
      setApplying(false);
    }
  }, [selectedIds]);

  const resetResult = useCallback(() => {
    setResult(null);
  }, []);

  return {
    items,
    allPreviewItems,
    loading,
    applying,
    error,
    selectedIds,
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
