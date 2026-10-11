import { useCallback, useEffect, useState } from 'react';
import { approveTrackAlias, listTrackAliases, rejectTrackAlias } from '@/services/trackFixService';
import type { TrackAlias } from '@/services/trackFixService';
import { errorMessage } from '@/services/apiClient';

export interface UseTrackAliasForIssueReturn {
  /** The pending alias suggested through this issue, or null (report-only issue or already decided). */
  alias: TrackAlias | null;
  busy: boolean;
  error: string | null;
  /** Approves or rejects the alias; resolves true on success. */
  decide: (decision: 'approve' | 'reject') => Promise<boolean>;
}

/** Loads the pending track alias linked to an issue and lets an admin approve or reject it. */
export function useTrackAliasForIssue(issueId: string, enabled: boolean): UseTrackAliasForIssueReturn {
  const [alias, setAlias] = useState<TrackAlias | null>(null);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) {
      setAlias(null);
      return;
    }
    let cancelled = false;
    setError(null);
    listTrackAliases('pending')
      .then((rows) => {
        if (!cancelled) setAlias(rows.find((r) => r.issue_id === issueId) ?? null);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setAlias(null);
          setError(errorMessage(err, 'Failed to load the track suggestion'));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [issueId, enabled]);

  const decide = useCallback(
    async (decision: 'approve' | 'reject'): Promise<boolean> => {
      if (!alias) return false;
      setBusy(true);
      setError(null);
      try {
        if (decision === 'approve') await approveTrackAlias(alias.id);
        else await rejectTrackAlias(alias.id);
        setAlias(null);
        return true;
      } catch (err: unknown) {
        setError(errorMessage(err, `Failed to ${decision} the match`));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [alias]
  );

  return { alias, busy, error, decide };
}
