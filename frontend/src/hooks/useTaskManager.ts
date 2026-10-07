import { useCallback, useEffect, useRef, useState } from 'react';
import type { ScheduledTaskItem } from '@/types/models';
import {
  getScheduledTasks,
  triggerScheduledTask,
  cancelScheduledTask,
  updateTaskSchedule,
} from '@/services/systemService';
import { errorMessage } from '@/services/apiClient';
import { usePolling } from './usePolling';

const POLL_IDLE_MS = 5000;
const POLL_BUSY_MS = 2000;

export type ToastFn = (msg: string, tone?: 'ok' | 'error') => void;

export interface UseTaskManagerReturn {
  tasks: ScheduledTaskItem[];
  isLoading: boolean;
  isRefreshing: boolean;
  error: string | null;
  runningIds: ReadonlySet<string>;
  cancellingIds: ReadonlySet<string>;
  savingScheduleIds: ReadonlySet<string>;
  refresh: () => Promise<void>;
  run: (taskId: string) => Promise<void>;
  cancel: (taskId: string) => Promise<void>;
  /** `null` resets to the task's default interval. */
  setSchedule: (taskId: string, intervalSeconds: number | null) => Promise<void>;
}

function withId(prev: ReadonlySet<string>, id: string): Set<string> {
  return new Set([...prev, id]);
}

function withoutId(prev: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(prev);
  next.delete(id);
  return next;
}

function replaceTask(list: ScheduledTaskItem[], next: ScheduledTaskItem): ScheduledTaskItem[] {
  return list.map((t) => (t.id === next.id ? next : t));
}

/** Task list state for the Tasks page: adaptive polling, run/cancel, and optimistic schedule edits. */
export function useTaskManager(onToast: ToastFn): UseTaskManagerReturn {
  const [tasks, setTasks] = useState<ScheduledTaskItem[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [runningIds, setRunningIds] = useState<ReadonlySet<string>>(new Set());
  const [cancellingIds, setCancellingIds] = useState<ReadonlySet<string>>(new Set());
  const [savingScheduleIds, setSavingScheduleIds] = useState<ReadonlySet<string>>(new Set());
  const mounted = useRef<boolean>(true);
  // A poll in flight when an optimistic edit lands must not clobber it with stale data.
  const savingRef = useRef<ReadonlySet<string>>(new Set());
  const tasksRef = useRef<ScheduledTaskItem[]>(tasks);
  tasksRef.current = tasks;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async (): Promise<void> => {
    try {
      const fresh = await getScheduledTasks();
      if (!mounted.current) return;
      setTasks(
        fresh.map((t) => (savingRef.current.has(t.id) ? (tasksRef.current.find((c) => c.id === t.id) ?? t) : t))
      );
      setError(null);
    } catch (err: unknown) {
      if (mounted.current) setError(errorMessage(err, 'Failed to load scheduled tasks'));
    } finally {
      if (mounted.current) setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const anyRunning = tasks.some((t) => t.status === 'running') || runningIds.size > 0;
  usePolling(() => void load(), anyRunning ? POLL_BUSY_MS : POLL_IDLE_MS);

  const refresh = useCallback(async (): Promise<void> => {
    setIsRefreshing(true);
    try {
      await load();
    } finally {
      if (mounted.current) setIsRefreshing(false);
    }
  }, [load]);

  const run = useCallback(
    async (taskId: string): Promise<void> => {
      setRunningIds((p) => withId(p, taskId));
      try {
        const res = await triggerScheduledTask(taskId);
        onToast(res.message || `Task '${taskId}' dispatched`, 'ok');
        await load();
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to trigger task'), 'error');
      } finally {
        if (mounted.current) setRunningIds((p) => withoutId(p, taskId));
      }
    },
    [onToast, load]
  );

  const cancel = useCallback(
    async (taskId: string): Promise<void> => {
      setCancellingIds((p) => withId(p, taskId));
      try {
        const res = await cancelScheduledTask(taskId);
        onToast(res.message || `Task '${taskId}' cancelled`, 'ok');
        await load();
      } catch (err: unknown) {
        onToast(errorMessage(err, 'Failed to cancel task'), 'error');
      } finally {
        if (mounted.current) setCancellingIds((p) => withoutId(p, taskId));
      }
    },
    [onToast, load]
  );

  const setSchedule = useCallback(
    async (taskId: string, intervalSeconds: number | null): Promise<void> => {
      const before = tasksRef.current.find((t) => t.id === taskId);
      if (!before) return;
      const optimisticSeconds = intervalSeconds ?? before.default_interval_seconds ?? null;
      savingRef.current = withId(savingRef.current, taskId);
      setSavingScheduleIds((p) => withId(p, taskId));
      setTasks((list) => replaceTask(list, { ...before, interval_seconds: optimisticSeconds }));
      try {
        const saved = await updateTaskSchedule(taskId, intervalSeconds);
        if (mounted.current) setTasks((list) => replaceTask(list, saved));
        onToast('Schedule updated', 'ok');
      } catch (err: unknown) {
        if (mounted.current) setTasks((list) => replaceTask(list, before));
        onToast(errorMessage(err, 'Failed to update schedule'), 'error');
      } finally {
        savingRef.current = withoutId(savingRef.current, taskId);
        if (mounted.current) setSavingScheduleIds((p) => withoutId(p, taskId));
      }
    },
    [onToast]
  );

  return {
    tasks,
    isLoading,
    isRefreshing,
    error,
    runningIds,
    cancellingIds,
    savingScheduleIds,
    refresh,
    run,
    cancel,
    setSchedule,
  };
}
