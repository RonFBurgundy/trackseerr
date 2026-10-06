import { useCallback, useState } from 'react';
import type { BulkArtistEditRequest, BulkArtistEditResult } from '@/types/monitoring';
import { bulkEditArtists } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';
import type { UseBulkSelectionReturn } from './useBulkSelection';

/** The editable part of a bulk request; the target (ids or all) is added from the selection. */
export type ArtistBulkPatch = Omit<BulkArtistEditRequest, 'artist_ids' | 'all'>;

export interface PendingArtistBulkEdit {
  patch: ArtistBulkPatch;
  /** Plain sentence for the confirmation step ("Set 3 artists to Monitored ..."). */
  summary: string;
  /** Short list of what changed, used in the success toast. */
  changes: string;
  /** Extra warning lines shown in the confirmation. */
  warnings: string[];
  targetCount: number;
  /** Filter/search signature at staging time; an "all" target is only valid while it still matches. */
  scopeKey: string;
}

export interface UseArtistBulkEditReturn {
  busy: boolean;
  pending: PendingArtistBulkEdit | null;
  /** Stages the patch for confirmation; nothing is sent until `confirm`. */
  request: (patch: ArtistBulkPatch, summary: string, changes: string, warnings?: string[]) => void;
  /** Sends the staged patch. Resolves true on success. The selection is kept so edits can be chained. */
  confirm: () => Promise<boolean>;
  cancel: () => void;
}

export function formatBulkArtistResult(r: BulkArtistEditResult): string {
  const parts = [`${r.artists_updated.toLocaleString()} ${r.artists_updated === 1 ? 'artist' : 'artists'} updated`];
  if (r.albums_monitored > 0) parts.push(`${r.albums_monitored.toLocaleString()} albums monitored`);
  if (r.albums_unmonitored > 0) parts.push(`${r.albums_unmonitored.toLocaleString()} albums unmonitored`);
  if ((r.tags_added ?? 0) > 0) parts.push(`${(r.tags_added ?? 0).toLocaleString()} tags added`);
  if ((r.tags_removed ?? 0) > 0) parts.push(`${(r.tags_removed ?? 0).toLocaleString()} tags removed`);
  return parts.join(' · ');
}

/** Applies bulk artist edits for the current selection after an explicit confirmation. */
export function useArtistBulkEdit(
  selection: UseBulkSelectionReturn,
  total: number,
  onToast: (msg: string, tone?: 'ok' | 'error') => void,
  onDone: () => void,
  /** Signature of the current list filter/search; changes invalidate a staged "all" target. */
  scopeKey = ''
): UseArtistBulkEditReturn {
  const [busy, setBusy] = useState<boolean>(false);
  const [pending, setPending] = useState<PendingArtistBulkEdit | null>(null);
  const { allMatching, selected } = selection;

  const execute = useCallback(
    async (staged: PendingArtistBulkEdit): Promise<boolean> => {
      if (allMatching && staged.scopeKey !== scopeKey) {
        setPending(null);
        onToast('The list filter changed since this edit was staged. Review the selection and apply again.', 'error');
        return false;
      }
      const target: BulkArtistEditRequest = allMatching
        ? { ...staged.patch, all: true }
        : { ...staged.patch, artist_ids: Array.from(selected, String) };
      if (!allMatching && (target.artist_ids?.length ?? 0) === 0) return false;
      setBusy(true);
      try {
        const result = await bulkEditArtists(target);
        onToast(`${staged.changes}: ${formatBulkArtistResult(result)}`);
        setPending(null);
        onDone();
        return true;
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Bulk edit failed'), 'error');
        return false;
      } finally {
        setBusy(false);
      }
    },
    [allMatching, selected, scopeKey, onToast, onDone]
  );

  const request = useCallback(
    (patch: ArtistBulkPatch, summary: string, changes: string, warnings: string[] = []): void => {
      const targetCount = allMatching ? total : selected.size;
      if (targetCount === 0) return;
      setPending({ patch, summary, changes, warnings, targetCount, scopeKey });
    },
    [allMatching, total, selected, scopeKey]
  );

  const confirm = useCallback(async (): Promise<boolean> => (pending ? execute(pending) : false), [pending, execute]);
  const cancel = useCallback((): void => setPending(null), []);

  return { busy, pending, request, confirm, cancel };
}
