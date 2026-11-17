import { useCallback, useRef, useState } from 'react';
import type { StoredMatchSuggestion } from '@/types/models';
import { applyStoredSuggestions, getStoredSuggestions } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';

/** Matches at or above this confidence start out ticked. */
export const SURE_CONFIDENCE = 80;
const APPLY_CHUNK = 500;
const FETCH_PAGE_SIZE = 200;

export interface ApplySummary {
  applied: number;
  conflicts: number;
  failed: number;
  /** Album ids that were linked, so callers can drop them from the selection. */
  appliedIds: string[];
}

export interface UseStoredSuggestionsReturn {
  selected: ReadonlySet<string>;
  setSelected: (next: ReadonlySet<string>) => void;
  /** Ticks the sure matches among newly loaded rows; rows seen before keep the admin's choice. */
  seedDefaults: (items: readonly StoredMatchSuggestion[]) => void;
  selectAll: () => Promise<void>;
  selectNone: () => void;
  isApplying: boolean;
  isSelectingAll: boolean;
  apply: () => Promise<ApplySummary | null>;
  error: string | null;
}

export function summarizeApply(s: ApplySummary): string {
  const parts = [`Linked ${s.applied} ${s.applied === 1 ? 'album' : 'albums'}`];
  if (s.conflicts > 0) parts.push(`${s.conflicts} need a duplicate choice`);
  if (s.failed > 0) parts.push(`${s.failed} failed`);
  return parts.join(' · ');
}

export function useStoredSuggestions(): UseStoredSuggestionsReturn {
  const [selected, setSelectedState] = useState<ReadonlySet<string>>(new Set());
  const [isApplying, setIsApplying] = useState<boolean>(false);
  const [isSelectingAll, setIsSelectingAll] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const seen = useRef<Set<string>>(new Set());

  const setSelected = useCallback((next: ReadonlySet<string>) => setSelectedState(new Set(next)), []);

  const seedDefaults = useCallback((items: readonly StoredMatchSuggestion[]) => {
    const fresh = items.filter((i) => !seen.current.has(i.album_id));
    if (fresh.length === 0) return;
    for (const i of fresh) seen.current.add(i.album_id);
    const sure = fresh.filter((i) => i.confidence >= SURE_CONFIDENCE).map((i) => i.album_id);
    if (sure.length === 0) return;
    setSelectedState((prev) => new Set([...prev, ...sure]));
  }, []);

  const selectAll = useCallback(async () => {
    setIsSelectingAll(true);
    setError(null);
    try {
      const ids = new Set<string>();
      let page = 1;
      let total = Infinity;
      while (ids.size < total) {
        const res = await getStoredSuggestions({ page, pageSize: FETCH_PAGE_SIZE });
        total = res.total;
        if (res.items.length === 0) break;
        for (const i of res.items) {
          ids.add(i.album_id);
          seen.current.add(i.album_id);
        }
        page += 1;
      }
      setSelectedState(ids);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not select every suggestion'));
    } finally {
      setIsSelectingAll(false);
    }
  }, []);

  const selectNone = useCallback(() => setSelectedState(new Set()), []);

  const apply = useCallback(async (): Promise<ApplySummary | null> => {
    const ids = [...selected];
    if (ids.length === 0) return null;
    setIsApplying(true);
    setError(null);
    const summary: ApplySummary = { applied: 0, conflicts: 0, failed: 0, appliedIds: [] };
    try {
      for (let i = 0; i < ids.length; i += APPLY_CHUNK) {
        const res = await applyStoredSuggestions(ids.slice(i, i + APPLY_CHUNK));
        for (const r of res.results) {
          if (r.status === 'applied') {
            summary.applied += 1;
            summary.appliedIds.push(r.album_id);
          } else if (r.status === 'conflict') {
            summary.conflicts += 1;
          } else {
            summary.failed += 1;
          }
        }
      }
    } catch (err: unknown) {
      setError(errorMessage(err, 'Could not apply the selected matches'));
      // A later chunk failing leaves earlier ones applied; count the rest as failed.
      summary.failed = ids.length - summary.applied - summary.conflicts;
    } finally {
      setIsApplying(false);
    }
    setSelectedState((prev) => {
      const next = new Set(prev);
      for (const id of summary.appliedIds) next.delete(id);
      return next;
    });
    return summary;
  }, [selected]);

  return { selected, setSelected, seedDefaults, selectAll, selectNone, isApplying, isSelectingAll, apply, error };
}
