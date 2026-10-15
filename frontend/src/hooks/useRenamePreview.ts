import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  getRenamePreview,
  applyRename,
  getRenameSafety,
  type RenamePreviewItem,
  type RenameApplyResponse,
} from '@/services/renameService';
import { errorMessage } from '@/services/apiClient';
import { RENAME_REASONS } from '@/components/library/renameReasons';

const isMismatch = (item: RenamePreviewItem): boolean => item.verification?.status === 'mismatch';

export interface UseRenamePreviewOptions {
  artistId?: string | null;
  albumId?: string | null;
  enabled?: boolean;
}

export interface UseRenamePreviewReturn {
  items: RenamePreviewItem[];
  allPreviewItems: RenamePreviewItem[];
  total: number;
  truncated: boolean;
  loading: boolean;
  applying: boolean;
  refreshing: boolean;
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
  /** file_ids of proposed moves with at least one risky reason, i.e. worth verifying by audio. */
  riskyIds: string[];
  safetyWarnings: string[];
  setSelectedIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  apply: () => Promise<RenameApplyResponse | null>;
  result: RenameApplyResponse | null;
  reload: () => Promise<void>;
  refreshFiles: (fileIds: string[]) => Promise<void>;
  resetResult: () => void;
}

export function useRenamePreview({
  artistId,
  albumId,
  enabled = true,
}: UseRenamePreviewOptions): UseRenamePreviewReturn {
  const [allPreviewItems, setAllPreviewItems] = useState<RenamePreviewItem[]>([]);
  const [items, setItems] = useState<RenamePreviewItem[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [truncated, setTruncated] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(false);
  const [applying, setApplying] = useState<boolean>(false);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [activeReasons, setActiveReasons] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<RenameApplyResponse | null>(null);
  const [safetyWarnings, setSafetyWarnings] = useState<string[]>([]);

  const reload = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    setLoading(true);
    setError(null);
    setResult(null);

    const safetyPromise = getRenameSafety()
      .then((res) => setSafetyWarnings(res.warnings ?? []))
      .catch(() => setSafetyWarnings([]));

    try {
      const data = await getRenamePreview({
        artist_id: artistId ?? undefined,
        album_id: albumId ?? undefined,
        limit: 5000,
      });
      setAllPreviewItems(data.items);
      setTotal(data.total);
      setTruncated(data.truncated);
      // Filter only files that would change
      const changing = data.items.filter((item) => item.needs_rename);
      setItems(changing);
      setSelectedIds(
        new Set(changing.filter((item) => item.default_selected).map((item) => item.file_id))
      );
      setActiveReasons(new Set());
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to generate rename preview'));
      setAllPreviewItems([]);
      setItems([]);
      setTotal(0);
      setTruncated(false);
      setSelectedIds(new Set());
    } finally {
      await safetyPromise;
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
        if (items.some((item) => item.file_id === fileId && isMismatch(item))) return prev;
        next.add(fileId);
      }
      return next;
    });
  }, [items]);

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

  const riskyIds = useMemo(
    () =>
      items
        .filter((item) => item.reasons.some((reason) => RENAME_REASONS[reason]?.risky === true))
        .map((item) => item.file_id),
    [items]
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
      for (const item of filteredItems) if (!isMismatch(item)) next.add(item.file_id);
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

  const refreshFiles = useCallback(async (fileIds: string[]): Promise<void> => {
    const uniqueIds = Array.from(new Set(fileIds.filter((id) => typeof id === 'string' && id.length > 0)));
    if (uniqueIds.length === 0) return;

    setRefreshing(true);
    try {
      const CHUNK_SIZE = 1000;
      const returnedItems: RenamePreviewItem[] = [];

      for (let i = 0; i < uniqueIds.length; i += CHUNK_SIZE) {
        const chunk = uniqueIds.slice(i, i + CHUNK_SIZE);
        const data = await getRenamePreview({
          file_ids: chunk,
          limit: chunk.length,
        });
        if (data?.items) {
          returnedItems.push(...data.items);
        }
      }

      const returnedMap = new Map<string, RenamePreviewItem>();
      for (const item of returnedItems) {
        returnedMap.set(item.file_id, item);
      }
      const queriedSet = new Set(uniqueIds);

      setAllPreviewItems((prev) => {
        const next: RenamePreviewItem[] = [];
        for (const item of prev) {
          if (queriedSet.has(item.file_id)) {
            const updated = returnedMap.get(item.file_id);
            if (updated) {
              next.push(updated);
            }
          } else {
            next.push(item);
          }
        }
        return next;
      });

      setItems((prev) => {
        const next: RenamePreviewItem[] = [];
        for (const item of prev) {
          if (queriedSet.has(item.file_id)) {
            const updated = returnedMap.get(item.file_id);
            if (updated && updated.needs_rename) {
              next.push(updated);
            }
          } else {
            next.push(item);
          }
        }
        const removedCount = prev.length - next.length;
        if (removedCount > 0) {
          setTotal((prevTotal) => Math.max(0, prevTotal - removedCount));
        }
        const survivingIds = new Set(next.map((item) => item.file_id));
        setSelectedIds((prevSelected) => {
          const nextSelected = new Set<string>();
          for (const id of prevSelected) {
            if (survivingIds.has(id)) {
              nextSelected.add(id);
            }
          }
          return nextSelected;
        });
        return next;
      });
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to refresh updated files'));
    } finally {
      setRefreshing(false);
    }
  }, []);

  const apply = useCallback(async (): Promise<RenameApplyResponse | null> => {
    if (selectedIds.size === 0) return null;
    setApplying(true);
    setError(null);
    try {
      const appliedIds = Array.from(selectedIds);
      const res = await applyRename({
        file_ids: appliedIds,
      });
      setResult(res);
      await refreshFiles(appliedIds);
      return res;
    } catch (err: unknown) {
      const msg = errorMessage(err, 'Failed to apply batch rename');
      setError(msg);
      return null;
    } finally {
      setApplying(false);
    }
  }, [selectedIds, refreshFiles]);

  const resetResult = useCallback(() => {
    setResult(null);
  }, []);

  return {
    items,
    allPreviewItems,
    total,
    truncated,
    loading,
    applying,
    refreshing,
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
    riskyIds,
    safetyWarnings,
    setSelectedIds,
    apply,
    result,
    reload,
    refreshFiles,
    resetResult,
  };
}
