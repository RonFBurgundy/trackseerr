import React from 'react';
import { Loader2, Play, Square, ChevronDown, Pause, PlayCircle } from 'lucide-react';
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
  isPausing: boolean;
  isSavingSchedule: boolean;
  expanded: boolean;
  onToggleExpanded: (taskId: string) => void;
  onRun: (taskId: string) => void;
  onCancel: (taskId: string) => void;
  onPause: (taskId: string, paused: boolean) => void;
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
  ({
    task,
    now,
    isTriggering,
    isCancelling,
    isPausing,
    isSavingSchedule,
    expanded,
    onToggleExpanded,
    onRun,
    onCancel,
    onPause,
    onSchedule,
  }) => {
    const isRunning = task.status === 'running';
    const startedAt = parseServerTimestamp(task.current_run_started_at);
    const countdown = isRunning ? null : nextRunText(task, now);
    const detailsId = `task-details-${task.id}`;
    const showEditable = task.editable && task.interval_presets.length > 0;

    return (
      <li className="p-3 sm:p-4 min-w-0">
        <div className="flex items-center justify-between gap-3 min-w-0">
          <button
            type="button"
            onClick={() => onToggleExpanded(task.id)}
            aria-expanded={expanded}
            aria-controls={detailsId}
            className="flex-1 min-w-0 text-left min-h-[44px] py-1 px-2 -ml-2 rounded-[3px] hover:bg-[#181818]/60 focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--accent-amber)] transition-colors duration-75 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-x-4 gap-y-1 group"
          >
            <div className="flex items-center justify-between gap-2 w-full sm:w-auto min-w-0">
              <div className="flex items-center gap-2 min-w-0">
                <span className="font-mono font-bold text-sm text-white truncate group-hover:text-[#e5a00d] transition-colors duration-75">
                  {task.name}
                </span>
                <TaskStatusBadge status={isRunning ? 'running' : task.status === 'idle' ? 'idle' : task.status} />
              </div>
              <ChevronDown
                className={`h-4 w-4 text-neutral-400 sm:hidden transition-transform duration-150 shrink-0 ${
                  expanded ? 'rotate-180' : ''
                }`}
                aria-hidden="true"
              />
            </div>

            <div className="flex items-center gap-x-3 gap-y-1 text-xs font-mono text-neutral-400 shrink-0">
              <div className="flex items-center gap-1.5">
                <span className="text-neutral-500 text-[10px] uppercase tracking-wider hidden md:inline">Last:</span>
                {task.last_run_at ? (
                  <span className="flex items-center gap-1">
                    <span>{relativeTime(task.last_run_at, now)}</span>
                    {task.last_run_status && <TaskStatusBadge status={task.last_run_status} />}
                  </span>
                ) : (
                  <span className="text-neutral-500">Never</span>
                )}
              </div>

              {isRunning ? (
                startedAt !== null && (
                  <div className="flex items-center gap-1 text-[#e5a00d]">
                    <span className="text-neutral-500 text-[10px] uppercase tracking-wider hidden md:inline">Elapsed:</span>
                    <span className="tabular-nums">{formatElapsed(now - startedAt)}</span>
                  </div>
                )
              ) : (
                countdown !== null && (
                  <div className="flex items-center gap-1">
                    <span className="text-neutral-500 text-[10px] uppercase tracking-wider hidden md:inline">Next:</span>
                    <span className={countdown === 'Due' ? 'text-[#e5a00d]' : 'tabular-nums text-neutral-300'}>
                      {countdown}
                    </span>
                  </div>
                )
              )}

              <ChevronDown
                className={`hidden sm:block h-4 w-4 text-neutral-400 transition-transform duration-150 shrink-0 ml-1 ${
                  expanded ? 'rotate-180' : ''
                }`}
                aria-hidden="true"
              />
            </div>
          </button>

          <div className="flex items-center gap-2 shrink-0">
            {task.can_pause && (
              <TapeDeckButton
                size="sm"
                disabled={isPausing}
                onClick={() => onPause(task.id, !task.paused)}
                aria-label={task.paused ? `Resume ${task.name}` : `Pause ${task.name}`}
                collapseLabel
                icon={
                  isPausing ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : task.paused ? (
                    <PlayCircle className="h-3.5 w-3.5" />
                  ) : (
                    <Pause className="h-3.5 w-3.5" />
                  )
                }
              >
                {task.paused ? 'Resume' : 'Pause'}
              </TapeDeckButton>
            )}
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

        {isRunning && (
          <div className="mt-2.5">
            <TaskProgress progress={task.progress} label={task.name} />
          </div>
        )}

        {expanded && (
          <div id={detailsId} className="mt-3 pt-3 border-t border-[#1c1c1c] space-y-3">
            {task.description && (
              <p className="text-xs font-mono text-neutral-400 break-words">{task.description}</p>
            )}

            <dl className="grid grid-cols-2 gap-x-3 gap-y-3 md:grid-cols-4">
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

            <TaskRunHistory taskId={task.id} />
          </div>
        )}
      </li>
    );
  }
);
TaskRow.displayName = 'TaskRow';
