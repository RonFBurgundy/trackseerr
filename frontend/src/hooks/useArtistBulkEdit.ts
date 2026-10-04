import { useCallback, useState } from 'react';
import type { BulkArtistEditRequest, BulkArtistEditResult } from '@/types/monitoring';
import { bulkEditArtists } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';
import type { UseBulkSelectionReturn } from './useBulkSelection';

/** The editable part of a bulk request; the target (ids or all) is added from the selection. */
export type ArtistBulkPatch = Omit<BulkArtistEditRequest, 'artist_ids' | 'all'>;

export interface PendingArtistBulkEdit {
  patch: ArtistBulkPatch;
  /** Short description of the change for the confirmation step. */
  summary: string;
  targetCount: number;
}

export interface UseArtistBulkEditReturn {
  busy: boolean;
  pending: PendingArtistBulkEdit | null;
  /** Runs the patch, or stages it for inline confirmation when it is destructive-scale. */
  request: (patch: ArtistBulkPatch, summary: string) => void;
  confirm: () => void;
  cancel: () => void;
}

export function formatBulkArtistResult(r: BulkArtistEditResult): string {
  const parts = [`${r.artists_updated.toLocaleString()} ${r.artists_updated === 1 ? 'artist' : 'artists'} updated`];
  if (r.albums_monitored > 0) parts.push(`${r.albums_monitored.toLocaleString()} albums monitored`);
  if (r.albums_unmonitored > 0) parts.push(`${r.albums_unmonitored.toLocaleString()} albums unmonitored`);
  return parts.join(' · ');
}

/** Applies bulk artist edits for the current selection, with a confirmation gate for all-artists or album cascades. */
export function useArtistBulkEdit(
  selection: UseBulkSelectionReturn,
  total: number,
  onToast: (msg: string, tone?: 'ok' | 'error') => void,
  onDone: () => void
): UseArtistBulkEditReturn {
  const [busy, setBusy] = useState<boolean>(false);
  const [pending, setPending] = useState<PendingArtistBulkEdit | null>(null);
  const { allMatching, selected, exit } = selection;

  const execute = useCallback(
    async (patch: ArtistBulkPatch): Promise<void> => {
      const target: BulkArtistEditRequest = allMatching
        ? { ...patch, all: true }
        : { ...patch, artist_ids: Array.from(selected, String) };
      if (!allMatching && (target.artist_ids?.length ?? 0) === 0) return;
      setBusy(true);
      try {
        const result = await bulkEditArtists(target);
        onToast(formatBulkArtistResult(result));
        setPending(null);
        exit();
        onDone();
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Bulk edit failed'), 'error');
      } finally {
        setBusy(false);
      }
    },
    [allMatching, selected, exit, onToast, onDone]
  );

  const request = useCallback(
    (patch: ArtistBulkPatch, summary: string): void => {
      const targetCount = allMatching ? total : selected.size;
      if (targetCount === 0) return;
      if (allMatching || patch.apply_monitor_to_albums === true) {
        setPending({ patch, summary, targetCount });
        return;
      }
      void execute(patch);
    },
    [allMatching, total, selected, execute]
  );

  const confirm = useCallback((): void => {
    if (pending) void execute(pending.patch);
  }, [pending, execute]);
  const cancel = useCallback((): void => setPending(null), []);

  return { busy, pending, request, confirm, cancel };
}
