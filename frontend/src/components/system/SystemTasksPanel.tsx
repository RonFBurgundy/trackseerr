import React, { useCallback, useState } from 'react';
import { Loader2, RotateCw, AlertTriangle } from 'lucide-react';
import { TapeDeckButton, MachinedCard } from '@/components/ui';
import { PageActionsPortal } from '@/components/layout';
import { useTaskManager } from '@/hooks/useTaskManager';
import { useNow } from '@/hooks/useNow';
import { TaskResourceStrip } from './TaskResourceStrip';
import { TaskRow } from './TaskRow';

export interface SystemTasksPanelProps {
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Settings > System > Tasks: process gauge plus one live row per scheduled task. */
export const SystemTasksPanel: React.FC<SystemTasksPanelProps> = ({ onToast }) => {
  const { tasks, isLoading, isRefreshing, error, runningIds, cancellingIds, savingScheduleIds, refresh, run, cancel, setSchedule } =
    useTaskManager(onToast);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const needsClock = tasks.some((t) => t.status === 'running' || t.schedule_kind === 'interval');
  const now = useNow(1000, needsClock);

  const toggleExpanded = useCallback((taskId: string) => setExpandedId((cur) => (cur === taskId ? null : taskId)), []);
  const handleRun = useCallback((taskId: string) => void run(taskId), [run]);
  const handleCancel = useCallback((taskId: string) => void cancel(taskId), [cancel]);
  const handleSchedule = useCallback((taskId: string, seconds: number | null) => void setSchedule(taskId, seconds), [setSchedule]);

  return (
    <div className="space-y-4 min-w-0">
      <PageActionsPortal>
        <div className="flex items-center justify-end gap-2">
          <TapeDeckButton
            size="sm"
            onClick={() => void refresh()}
            disabled={isRefreshing}
            collapseLabel
            aria-label="Refresh tasks"
            title="Refresh tasks"
            icon={isRefreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCw className="h-3.5 w-3.5" />}
          >
            Refresh
          </TapeDeckButton>
        </div>
      </PageActionsPortal>

      <TaskResourceStrip />

      <div className="min-w-0">
        <h4 className="text-sm font-bold uppercase font-mono text-white">Scheduled Tasks &amp; Background Workers</h4>
        <p className="text-xs text-neutral-400 font-mono mt-0.5">
          Live status, next run, schedules and 7-day run history. Trigger on-demand sweeps.
        </p>
      </div>

      {error && (
        <div role="alert" className="p-3 bg-red-950/40 border border-red-800/50 rounded-[4px] text-xs text-red-300 font-mono flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="min-w-0 break-words">{error}</span>
        </div>
      )}

      <MachinedCard className="overflow-hidden p-0 border-[#222222]">
        {tasks.length > 0 && (
          <ul className="divide-y divide-[#1c1c1c]">
            {tasks.map((task) => (
              <TaskRow
                key={task.id}
                task={task}
                now={now}
                isTriggering={runningIds.has(task.id)}
                isCancelling={cancellingIds.has(task.id)}
                isSavingSchedule={savingScheduleIds.has(task.id)}
                expanded={expandedId === task.id}
                onToggleExpanded={toggleExpanded}
                onRun={handleRun}
                onCancel={handleCancel}
                onSchedule={handleSchedule}
              />
            ))}
          </ul>
        )}
        {isLoading && tasks.length === 0 && (
          <div className="flex items-center justify-center gap-2 py-12 text-neutral-400 text-xs font-mono">
            <Loader2 className="h-4 w-4 animate-spin text-[#e5a00d]" aria-hidden="true" /> Loading scheduled tasks...
          </div>
        )}
        {!isLoading && tasks.length === 0 && (
          <div className="text-center py-12 text-neutral-500 font-mono text-sm">
            {error ? 'Scheduled tasks could not be loaded.' : 'No scheduled background tasks registered.'}
          </div>
        )}
      </MachinedCard>
    </div>
  );
};
