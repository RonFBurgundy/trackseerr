import { useState, useEffect, useCallback } from 'react';
import type { ScheduledTaskItem } from '@/types/models';
import {
  getScheduledTasks,
  triggerScheduledTask,
  cancelScheduledTask,
} from '@/services/systemService';
import { errorMessage } from '@/services/apiClient';

export interface UseScheduledTasksReturn {
  tasks: ScheduledTaskItem[];
  isLoading: boolean;
  error: string | null;
  runningIds: Set<string>;
  cancellingIds: Set<string>;
  refresh: () => Promise<void>;
  run: (taskId: string) => Promise<void>;
  cancel: (taskId: string) => Promise<void>;
}

function withId(prev: Set<string>, id: string): Set<string> {
  return new Set([...prev, id]);
}

function withoutId(prev: Set<string>, id: string): Set<string> {
  const next = new Set(prev);
  next.delete(id);
  return next;
}

export function useScheduledTasks(onToast: (msg: string) => void): UseScheduledTasksReturn {
  const [tasks, setTasks] = useState<ScheduledTaskItem[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [runningIds, setRunningIds] = useState<Set<string>>(new Set());
  const [cancellingIds, setCancellingIds] = useState<Set<string>>(new Set());

  const refresh = useCallback(async () => {
    setIsLoading(true);
    try {
      setTasks(await getScheduledTasks());
      setError(null);
    } catch (err: unknown) {
      setError(errorMessage(err, 'Failed to load scheduled tasks'));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const run = useCallback(
    async (taskId: string) => {
      setRunningIds((p) => withId(p, taskId));
      try {
        const res = await triggerScheduledTask(taskId);
        onToast(res.message || `Task '${taskId}' dispatched`);
        await refresh();
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to trigger task'));
      } finally {
        setRunningIds((p) => withoutId(p, taskId));
      }
    },
    [onToast, refresh]
  );

  const cancel = useCallback(
    async (taskId: string) => {
      setCancellingIds((p) => withId(p, taskId));
      try {
        const res = await cancelScheduledTask(taskId);
        onToast(res.message || `Task '${taskId}' cancelled`);
        await refresh();
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to cancel task'));
      } finally {
        setCancellingIds((p) => withoutId(p, taskId));
      }
    },
    [onToast, refresh]
  );

  return { tasks, isLoading, error, runningIds, cancellingIds, refresh, run, cancel };
}
