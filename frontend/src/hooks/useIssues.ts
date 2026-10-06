import { useState, useEffect, useCallback } from 'react';
import type { CreateIssuePayload, Issue } from '@/types/models';
import { ACTIVE_ISSUE_STATUSES } from '@/types/models';
import { getIssues, createIssue, notifyIssuesChanged } from '@/services/issueService';

export interface UseIssuesReturn {
  issues: Issue[];
  isLoading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  /** Creates an issue; rethrows so the caller can map the HTTP status. */
  submit: (payload: CreateIssuePayload) => Promise<Issue>;
  /** Replaces one issue in the list (after a detail action changed it). */
  patch: (issue: Issue) => void;
  /** Returns the current user's open or in-progress issue for this media, if any. */
  findActive: (mediaTitle: string, artist: string) => Issue | undefined;
}

const norm = (v: string): string => v.trim().toLowerCase();

/** The signed-in user's own issues (admins get everyone's from the API; consumers filter by user). */
export function useIssues(currentUserId?: string | number): UseIssuesReturn {
  const [issues, setIssues] = useState<Issue[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      setIssues(await getIssues());
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to load issues');
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const submit = useCallback(async (payload: CreateIssuePayload): Promise<Issue> => {
    const created = await createIssue(payload);
    notifyIssuesChanged();
    setIssues((prev) => [created, ...prev.filter((i) => i.id !== created.id)]);
    return created;
  }, []);

  const patch = useCallback((issue: Issue): void => {
    setIssues((prev) => prev.map((i) => (i.id === issue.id ? issue : i)));
  }, []);

  const findActive = useCallback(
    (mediaTitle: string, artist: string): Issue | undefined =>
      issues.find(
        (i) =>
          currentUserId !== undefined &&
          String(i.user_id) === String(currentUserId) &&
          ACTIVE_ISSUE_STATUSES.includes(i.status) &&
          norm(i.media_title) === norm(mediaTitle) &&
          norm(i.artist) === norm(artist)
      ),
    [issues, currentUserId]
  );

  return { issues, isLoading, error, refresh, submit, patch, findActive };
}
