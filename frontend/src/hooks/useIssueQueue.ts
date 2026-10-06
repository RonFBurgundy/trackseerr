import { useCallback, useEffect, useMemo, useState } from 'react';
import type { Issue, IssueStatus, IssueType } from '@/types/models';
import { ACTIVE_ISSUE_STATUSES } from '@/types/models';
import { deleteIssue, getIssues } from '@/services/issueService';
import { errorMessage } from '@/services/apiClient';

export type IssueStatusFilter = 'active' | 'all' | IssueStatus;
export type IssueTypeFilter = 'all' | IssueType;

export interface UseIssueQueueReturn {
  /** Issues matching the filters, newest activity first. */
  issues: Issue[];
  total: number;
  isLoading: boolean;
  error: string | null;
  statusFilter: IssueStatusFilter;
  typeFilter: IssueTypeFilter;
  search: string;
  setStatusFilter: (value: IssueStatusFilter) => void;
  setTypeFilter: (value: IssueTypeFilter) => void;
  setSearch: (value: string) => void;
  refresh: () => Promise<void>;
  /** Swap an updated issue into the list. */
  patch: (issue: Issue) => void;
  /** Deletes an issue; returns an error message or null. */
  remove: (id: string) => Promise<string | null>;
}

const activityOf = (i: Issue): string => i.last_activity_at ?? i.updated_at ?? i.created_at ?? '';

/** Admin issue queue: loads everything once and filters locally (status, type, free-text search). */
export function useIssueQueue(enabled: boolean): UseIssueQueueReturn {
  const [all, setAll] = useState<Issue[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<IssueStatusFilter>('active');
  const [typeFilter, setTypeFilter] = useState<IssueTypeFilter>('all');
  const [search, setSearch] = useState<string>('');

  const refresh = useCallback(async (): Promise<void> => {
    if (!enabled) return;
    setIsLoading(true);
    setError(null);
    try {
      setAll(await getIssues());
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to load issues'));
    } finally {
      setIsLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const patch = useCallback((issue: Issue): void => {
    setAll((prev) => prev.map((i) => (i.id === issue.id ? { ...i, ...issue } : i)));
  }, []);

  const remove = useCallback(async (id: string): Promise<string | null> => {
    try {
      await deleteIssue(id);
      setAll((prev) => prev.filter((i) => i.id !== id));
      return null;
    } catch (err: unknown) {
      return errorMessage(err, 'Failed to delete issue');
    }
  }, []);

  const issues = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return all
      .filter((i) => {
        if (statusFilter === 'active' ? !ACTIVE_ISSUE_STATUSES.includes(i.status) : statusFilter !== 'all' && i.status !== statusFilter) {
          return false;
        }
        if (typeFilter !== 'all' && i.issue_type !== typeFilter) return false;
        if (!needle) return true;
        return [i.media_title, i.artist, i.username ?? '', i.problem_details].some((f) => f.toLowerCase().includes(needle));
      })
      .sort((a, b) => activityOf(b).localeCompare(activityOf(a)));
  }, [all, statusFilter, typeFilter, search]);

  return { issues, total: all.length, isLoading, error, statusFilter, typeFilter, search, setStatusFilter, setTypeFilter, setSearch, refresh, patch, remove };
}
