import React from 'react';
import { Loader2, Play, Square, ChevronDown, ChevronRight } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';
import { parseServerTimestamp, relativeTime } from '@/components/issues/issueFormat';
import type { ScheduledTaskItem } from '@/types/models';
import { formatDuration } from './formatters';
import { formatCountdown, formatElapsed, scheduleLabel } from './taskFormat';
import { TaskStatusBadge } from './TaskStatusBadge';
import { TaskProgress } from './TaskProgress';
import { TaskScheduleSelect } from './TaskScheduleSelect';
import { TaskRunHistory } from './TaskRunHistory';

export interface TaskRowProps {
  task: ScheduledTaskItem;
  /** Shared ticking clock (ms) so every row counts down off one timer. */
  now: number;
  isTriggering: boolean;
  isCancelling: boolean;
  isSavingSchedule: boolean;
  expanded: boolean;
  onToggleExpanded: (taskId: string) => void;
  onRun: (taskId: string) => void;
  onCancel: (taskId: string) => void;
  onSchedule: (taskId: string, intervalSeconds: number | null) => void;
}

interface FieldProps {
  label: string;
  children: React.ReactNode;
}

const Field: React.FC<FieldProps> = ({ label, children }) => (
  <div className="min-w-0">
    <dt className="text-[10px] font-mono uppercase tracking-wider text-neutral-500">{label}</dt>
    <dd className="mt-0.5 text-[13px] font-mono text-neutral-200 min-w-0">{children}</dd>
  </div>
);

/** Countdown cell: hidden for continuous/manual tasks, "Due" once the instant has passed. */
function nextRunText(task: ScheduledTaskItem, now: number): string | null {
  if (task.schedule_kind !== 'interval') return null;
  const at = parseServerTimestamp(task.next_run_at);
  if (at === null) return null;
  return formatCountdown(at - now);
}

export const TaskRow: React.FC<TaskRowProps> = React.memo(
  ({ task, now, isTriggering, isCancelling, isSavingSchedule, expanded, onToggleExpanded, onRun, onCancel, onSchedule }) => {
    const isRunning = task.status === 'running';
    const startedAt = parseServerTimestamp(task.current_run_started_at);
    const countdown = isRunning ? null : nextRunText(task, now);
    const historyId = `task-history-${task.id}`;
    const showEditable = task.editable && task.interval_presets.length > 0;

    return (
      <li className="p-3 sm:p-4 min-w-0">
        <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
          <div className="min-w-0 flex-1 basis-56">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono font-bold text-sm text-white break-words">{task.name}</span>
              <TaskStatusBadge status={isRunning ? 'running' : task.status === 'idle' ? 'idle' : task.status} />
            </div>
            <p className="mt-0.5 text-[11px] font-mono text-neutral-400 break-words">{task.description}</p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {task.can_trigger && (
              <TapeDeckButton
                size="sm"
                variant="amber"
                disabled={isRunning || isTriggering || isCancelling}
                onClick={() => onRun(task.id)}
                aria-label={`Run ${task.name} now`}
                icon={isTriggering ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
              >
                Run now
              </TapeDeckButton>
            )}
            {task.can_cancel && isRunning && (
              <TapeDeckButton
                size="sm"
                variant="danger"
                disabled={isCancelling}
                onClick={() => onCancel(task.id)}
                aria-label={`Cancel ${task.name}`}
                icon={isCancelling ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Square className="h-3.5 w-3.5" />}
              >
                Cancel
              </TapeDeckButton>
            )}
          </div>
        </div>

        <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-3 md:grid-cols-4">
          {showEditable ? (
            <TaskScheduleSelect task={task} disabled={isSavingSchedule} onChange={(s) => onSchedule(task.id, s)} />
          ) : (
            <Field label="Schedule">{scheduleLabel(task)}</Field>
          )}
          <Field label="Last run">
            {task.last_run_at ? (
              <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span>{relativeTime(task.last_run_at, now)}</span>
                {task.last_run_status && <TaskStatusBadge status={task.last_run_status} />}
                {task.last_duration_ms != null && (
                  <span className="text-neutral-500">{formatDuration(task.last_duration_ms)}</span>
                )}
              </span>
            ) : (
              <span className="text-neutral-500">Never</span>
            )}
          </Field>
          {isRunning ? (
            <Field label="Elapsed">
              {startedAt !== null ? <span className="text-[#e5a00d] tabular-nums">{formatElapsed(now - startedAt)}</span> : '—'}
            </Field>
          ) : (
            countdown !== null && (
              <Field label="Next run">
                <span className={countdown === 'Due' ? 'text-[#e5a00d]' : 'tabular-nums'}>{countdown}</span>
              </Field>
            )
          )}
        </dl>

        {isRunning && (
          <div className="mt-3">
            <TaskProgress progress={task.progress} label={task.name} />
          </div>
        )}

        <button
          type="button"
          onClick={() => onToggleExpanded(task.id)}
          aria-expanded={expanded}
          aria-controls={historyId}
          className="mt-3 inline-flex items-center gap-1 min-h-[28px] text-[11px] font-mono font-bold uppercase tracking-wider text-neutral-400 hover:text-white focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--accent-amber)]"
        >
          {expanded ? <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />}
          Run history
        </button>
        <div id={historyId}>{expanded && <TaskRunHistory taskId={task.id} />}</div>
      </li>
    );
  }
);
TaskRow.displayName = 'TaskRow';
