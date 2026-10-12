import { useCallback, useEffect, useMemo, useState } from 'react';
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
  /** Rows after the reason filter (all rows when no reason is active). */
  filteredItems: RenamePreviewItem[];
  /** Number of proposed moves per reason code. */
  reasonCounts: Record<string, number>;
  activeReasons: Set<string>;
  toggleReason: (reason: string) => void;
  clearReasons: () => void;
  /** Selects every currently filtered row. */
  selectAll: () => void;
  /** Selects exactly the rows the backend marks as safe (default_selected). */
  selectSafe: () => void;
  /** Deselects every currently filtered row. */
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
  const [activeReasons, setActiveReasons] = useState<Set<string>>(new Set());
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
        limit: 5000,
      });
      setAllPreviewItems(data);
      // Filter only files that would change
      const changing = data.filter((item) => item.needs_rename);
      setItems(changing);
      setSelectedIds(
        new Set(changing.filter((item) => item.default_selected).map((item) => item.file_id))
      );
      setActiveReasons(new Set());
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

  const reasonCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of items) {
      for (const reason of item.reasons) counts[reason] = (counts[reason] ?? 0) + 1;
    }
    return counts;
  }, [items]);

  const filteredItems = useMemo(
    () =>
      activeReasons.size === 0
        ? items
        : items.filter((item) => item.reasons.some((reason) => activeReasons.has(reason))),
    [items, activeReasons]
  );

  const toggleReason = useCallback((reason: string) => {
    setActiveReasons((prev) => {
      const next = new Set(prev);
      if (next.has(reason)) next.delete(reason);
      else next.add(reason);
      return next;
    });
  }, []);

  const clearReasons = useCallback(() => {
    setActiveReasons(new Set());
  }, []);

  const selectAll = useCallback(() => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const item of filteredItems) next.add(item.file_id);
      return next;
    });
  }, [filteredItems]);

  const selectSafe = useCallback(() => {
    setSelectedIds(new Set(items.filter((item) => item.default_selected).map((item) => item.file_id)));
  }, [items]);

  const selectNone = useCallback(() => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const item of filteredItems) next.delete(item.file_id);
      return next;
    });
  }, [filteredItems]);

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
    filteredItems,
    reasonCounts,
    activeReasons,
    toggleReason,
    clearReasons,
    selectAll,
    selectSafe,
    selectNone,
    setSelectedIds,
    apply,
    result,
    reload,
    resetResult,
  };
}
