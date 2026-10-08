import { useState, useEffect, useCallback } from 'react';
import {
  getChangelog,
  type ChangelogResponse,
  type ChangelogRelease,
} from '@/services/changelogService';

export interface UseChangelogResult {
  changelog: ChangelogResponse | null;
  releases: ChangelogRelease[];
  latestRelease: ChangelogRelease | null;
  version: string | null;
  commit: string | null;
  isLoading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export function useChangelog(limit?: number): UseChangelogResult {
  const [changelog, setChangelog] = useState<ChangelogResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await getChangelog(limit);
      setChangelog(data);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Failed to fetch changelog';
      setError(msg);
    } finally {
      setIsLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return {
    changelog,
    releases: changelog?.releases ?? [],
    latestRelease: changelog?.latest ?? null,
    version: changelog?.version ?? null,
    commit: changelog?.commit ?? null,
    isLoading,
    error,
    refresh,
  };
}
