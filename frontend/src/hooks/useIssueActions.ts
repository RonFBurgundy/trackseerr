import { useCallback, useState } from 'react';
import type { Issue, IssueRematchResult } from '@/types/models';
import { runIssueAction } from '@/services/issueService';
import { errorMessage } from '@/services/apiClient';

export type IssueActionOutcome =
  | { kind: 'done'; issue: Issue; message: string }
  | { kind: 'rematch'; issue: Issue; rematch: IssueRematchResult }
  | { kind: 'error'; message: string };

export interface UseIssueActionsReturn {
  /** The action currently running, if any. */
  running: string | null;
  run: (issueId: string, action: string) => Promise<IssueActionOutcome>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Narrows the untyped `rematch` result at the API boundary. */
function toRematch(result: Record<string, unknown>): IssueRematchResult | null {
  const { scope, album } = result;
  if (!isRecord(scope) || typeof scope.album_id !== 'string' || !isRecord(album) || typeof album.id !== 'string') return null;
  return {
    scope: { album_id: scope.album_id },
    album: {
      id: album.id,
      title: typeof album.title === 'string' ? album.title : null,
      artist_name: typeof album.artist_name === 'string' ? album.artist_name : null,
    },
  };
}

/** Runs admin fix actions on an issue and classifies the outcome for the caller (toast / open import). */
export function useIssueActions(): UseIssueActionsReturn {
  const [running, setRunning] = useState<string | null>(null);

  const run = useCallback(async (issueId: string, action: string): Promise<IssueActionOutcome> => {
    setRunning(action);
    try {
      const res = await runIssueAction(issueId, action);
      if (action === 'rematch') {
        const rematch = toRematch(res.result);
        if (!rematch) return { kind: 'error', message: 'The server returned no album to rematch.' };
        return { kind: 'rematch', issue: res.issue, rematch };
      }
      const message = typeof res.result.message === 'string' ? res.result.message : 'Action started';
      return { kind: 'done', issue: res.issue, message };
    } catch (err: unknown) {
      return { kind: 'error', message: errorMessage(err, 'Action failed') };
    } finally {
      setRunning(null);
    }
  }, []);

  return { running, run };
}
