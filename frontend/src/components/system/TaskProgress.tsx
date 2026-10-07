import React from 'react';
import type { TaskProgress as TaskProgressData } from '@/types/models';

export interface TaskProgressProps {
  progress: TaskProgressData | null | undefined;
  /** Accessible name, usually the task name. */
  label: string;
}

/** Determinate bar when `total` is known; otherwise the message over an indeterminate sweep (static when motion is reduced). */
export const TaskProgress: React.FC<TaskProgressProps> = React.memo(({ progress, label }) => {
  const total = progress?.total ?? null;
  const current = progress?.current ?? 0;
  const determinate = total !== null && total > 0;
  const pct = determinate ? Math.min(100, Math.max(0, Math.round((current / total) * 100))) : 0;
  const message = progress?.message ?? null;

  return (
    <div className="min-w-0">
      <div
        role="progressbar"
        aria-label={`${label} progress`}
        aria-valuemin={0}
        aria-valuemax={determinate ? total : undefined}
        aria-valuenow={determinate ? current : undefined}
        aria-valuetext={determinate ? `${current} of ${total}` : (message ?? 'In progress')}
        className="relative h-1.5 w-full overflow-hidden rounded-[2px] bg-[#0d0d0d] border border-[#1f1f1f]"
      >
        {determinate ? (
          <div className="h-full bg-[var(--accent-amber)] transition-[width] duration-300" style={{ width: `${pct}%` }} />
        ) : (
          <div className="task-progress-sweep absolute inset-y-0 w-1/3 bg-[var(--accent-amber)]" />
        )}
      </div>
      {(determinate || message) && (
        <p className="mt-1 text-[11px] font-mono text-neutral-400 truncate">
          {determinate && `${current}/${total}`}
          {determinate && message ? ' · ' : ''}
          {message}
        </p>
      )}
    </div>
  );
});
TaskProgress.displayName = 'TaskProgress';
