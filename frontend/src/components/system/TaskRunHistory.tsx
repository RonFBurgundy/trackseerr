import React from 'react';
import { Loader2, RotateCw, AlertTriangle } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';
import { useTaskRuns, TASK_RUN_HISTORY_DAYS } from '@/hooks/useTaskRuns';
import { formatDuration, formatTimestamp } from './formatters';
import { TaskStatusBadge } from './TaskStatusBadge';

export interface TaskRunHistoryProps {
  taskId: string;
}

/** Last 7 days of runs, newest first; mounted only while its row is expanded, so it fetches on open. Rows stack on phones. */
export const TaskRunHistory: React.FC<TaskRunHistoryProps> = ({ taskId }) => {
  const { runs, isLoading, error, reload } = useTaskRuns(taskId, true);

  return (
    <div className="mt-3 border-t border-[#1c1c1c] pt-3">
      <div className="flex items-center justify-between gap-2">
        <h5 className="text-[11px] font-mono font-bold uppercase tracking-wider text-neutral-400">
          Run history · last {TASK_RUN_HISTORY_DAYS} days
        </h5>
        <TapeDeckButton
          size="sm"
          onClick={() => void reload()}
          disabled={isLoading}
          aria-label="Reload run history"
          title="Reload run history"
          icon={isLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCw className="h-3.5 w-3.5" />}
        />
      </div>

      {error && (
        <p className="mt-2 flex items-center gap-2 text-xs font-mono text-red-300" role="alert">
          <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
          {error}
        </p>
      )}

      {!error && !isLoading && runs.length === 0 && (
        <p className="mt-2 text-xs font-mono text-neutral-500">No runs recorded in the last {TASK_RUN_HISTORY_DAYS} days.</p>
      )}
      {isLoading && runs.length === 0 && (
        <p className="mt-2 flex items-center gap-2 text-xs font-mono text-neutral-400">
          <Loader2 className="h-3.5 w-3.5 animate-spin text-[#e5a00d]" aria-hidden="true" /> Loading history...
        </p>
      )}

      {runs.length > 0 && (
        <ul className="mt-2 divide-y divide-[#1c1c1c] text-xs font-mono">
          {runs.map((run) => (
            <li key={run.id} className="py-2 grid gap-1 sm:grid-cols-[7rem_6.5rem_12rem_5rem_minmax(0,1fr)] sm:items-center sm:gap-3">
              <span className="uppercase text-neutral-400">{run.trigger}</span>
              <span>
                <TaskStatusBadge status={run.status} />
              </span>
              <span className="text-neutral-300">{formatTimestamp(run.started_at)}</span>
              <span className="text-neutral-400">{formatDuration(run.duration_ms)}</span>
              <span className="min-w-0 break-words text-neutral-500">{run.message || ''}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};
