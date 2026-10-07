import { useCallback, useEffect, useRef, useState } from 'react';
import type { TaskRunItem } from '@/types/models';
import { getTaskRuns } from '@/services/systemService';
import { errorMessage } from '@/services/apiClient';

export const TASK_RUN_HISTORY_DAYS = 7;

export interface UseTaskRunsReturn {
  runs: TaskRunItem[];
  isLoading: boolean;
  error: string | null;
  reload: () => Promise<void>;
}

/** Last 7 days of runs for one task, newest first. Loads when `enabled` flips on (row expanded). */
export function useTaskRuns(taskId: string, enabled: boolean): UseTaskRunsReturn {
  const [runs, setRuns] = useState<TaskRunItem[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const latest = useRef<number>(0);

  const reload = useCallback(async (): Promise<void> => {
    latest.current += 1;
    const ticket = latest.current;
    setIsLoading(true);
    try {
      const rows = await getTaskRuns(taskId, TASK_RUN_HISTORY_DAYS);
      if (ticket !== latest.current) return;
      setRuns([...rows].sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at)));
      setError(null);
    } catch (err: unknown) {
      if (ticket === latest.current) setError(errorMessage(err, 'Failed to load run history'));
    } finally {
      if (ticket === latest.current) setIsLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    if (!enabled) return undefined;
    void reload();
    return () => {
      latest.current += 1;
    };
  }, [enabled, reload]);

  return { runs, isLoading, error, reload };
}
