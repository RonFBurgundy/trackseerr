import { useCallback, useEffect, useRef, useState } from 'react';
import type { Issue, IssueComment, IssueStatus } from '@/types/models';
import {
  addIssueComment,
  getIssue,
  getIssueComments,
  markIssueSeen,
  setIssueStatus,
  updateIssue,
} from '@/services/issueService';
import { errorMessage } from '@/services/apiClient';

export interface UseIssueDetailReturn {
  issue: Issue | null;
  comments: IssueComment[];
  isLoading: boolean;
  error: string | null;
  /** True while a comment or status change is in flight. */
  isBusy: boolean;
  /** Posts a comment; returns an error message, or null on success. */
  postComment: (body: string) => Promise<string | null>;
  /** Moves the issue to `status` (admins via PUT, reporters via the status endpoint); returns an error message or null. */
  changeStatus: (status: IssueStatus) => Promise<string | null>;
  /** Replace the loaded issue (an admin action returned a fresh one) and re-read the thread. */
  adopt: (issue: Issue) => Promise<void>;
}

/**
 * One issue with its thread. Opening marks it seen for the reporter. `onChanged` fires after anything that mutates it
 * so lists and nav badges can refresh.
 */
export function useIssueDetail(issueId: string | null, isAdmin: boolean, onChanged: (issue: Issue | null) => void): UseIssueDetailReturn {
  const [issue, setIssue] = useState<Issue | null>(null);
  const [comments, setComments] = useState<IssueComment[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [isBusy, setIsBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;
  const requestRef = useRef<number>(0);

  const load = useCallback(async (id: string, markSeen: boolean): Promise<void> => {
    const ticket = requestRef.current + 1;
    requestRef.current = ticket;
    setIsLoading(true);
    setError(null);
    try {
      const [loaded, thread] = await Promise.all([getIssue(id), getIssueComments(id)]);
      if (requestRef.current !== ticket) return;
      setIssue(loaded);
      setComments(thread);
      if (markSeen && loaded.unread) {
        try {
          await markIssueSeen(id);
          if (requestRef.current === ticket) {
            setIssue({ ...loaded, unread: false });
            onChangedRef.current({ ...loaded, unread: false });
          }
        } catch (err: unknown) {
          console.warn('Marking issue seen failed', err);
        }
      }
    } catch (err: unknown) {
      if (requestRef.current === ticket) setError(errorMessage(err, 'Failed to load issue'));
    } finally {
      if (requestRef.current === ticket) setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (issueId === null) {
      requestRef.current += 1;
      setIssue(null);
      setComments([]);
      setError(null);
      setIsLoading(false);
      return;
    }
    void load(issueId, !isAdmin);
  }, [issueId, isAdmin, load]);

  const postComment = useCallback(
    async (body: string): Promise<string | null> => {
      if (issueId === null) return 'No issue selected';
      setIsBusy(true);
      try {
        const created = await addIssueComment(issueId, body);
        setComments((prev) => [...prev, created]);
        onChangedRef.current(null);
        return null;
      } catch (err: unknown) {
        return errorMessage(err, 'Failed to post comment');
      } finally {
        setIsBusy(false);
      }
    },
    [issueId]
  );

  const changeStatus = useCallback(
    async (status: IssueStatus): Promise<string | null> => {
      if (issueId === null) return 'No issue selected';
      setIsBusy(true);
      try {
        const updated = isAdmin ? await updateIssue(issueId, { status }) : await setIssueStatus(issueId, status);
        setIssue(updated);
        onChangedRef.current(updated);
        // The server may append a system comment on status changes; re-read the thread.
        setComments(await getIssueComments(issueId));
        return null;
      } catch (err: unknown) {
        return errorMessage(err, 'Failed to change status');
      } finally {
        setIsBusy(false);
      }
    },
    [issueId, isAdmin]
  );

  const adopt = useCallback(async (next: Issue): Promise<void> => {
    setIssue(next);
    onChangedRef.current(next);
    try {
      setComments(await getIssueComments(next.id));
    } catch (err: unknown) {
      console.warn('Reloading issue thread failed', err);
    }
  }, []);

  return { issue, comments, isLoading, error, isBusy, postComment, changeStatus, adopt };
}
