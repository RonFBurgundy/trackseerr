import React, { useId } from 'react';
import type { ScheduledTaskItem } from '@/types/models';
import { formatIntervalSeconds } from './taskFormat';

const DEFAULT_VALUE = 'default';

export interface TaskScheduleSelectProps {
  task: ScheduledTaskItem;
  disabled?: boolean;
  /** `null` resets to the task default. */
  onChange: (intervalSeconds: number | null) => void;
}

/** Labelled preset picker. "Default (x)" sends null; presets equal to the default are folded into that option. */
export const TaskScheduleSelect: React.FC<TaskScheduleSelectProps> = React.memo(({ task, disabled = false, onChange }) => {
  const id = useId();
  const defaultSeconds = task.default_interval_seconds ?? null;
  const presets = task.interval_presets.filter((p) => p !== defaultSeconds);
  const current = task.interval_seconds ?? null;
  const value = current === null || current === defaultSeconds ? DEFAULT_VALUE : String(current);
  // A current value outside the presets (e.g. changed elsewhere) must still be representable.
  const orphan = value !== DEFAULT_VALUE && !presets.includes(Number(value)) ? Number(value) : null;

  const handle = (e: React.ChangeEvent<HTMLSelectElement>): void => {
    onChange(e.target.value === DEFAULT_VALUE ? null : Number(e.target.value));
  };

  return (
    <div className="min-w-0">
      <label htmlFor={id} className="block text-[10px] font-mono uppercase tracking-wider text-neutral-500">
        Schedule
      </label>
      <select
        id={id}
        name={`schedule-${task.id}`}
        value={value}
        onChange={handle}
        disabled={disabled}
        className="mt-0.5 h-9 w-full max-w-[11rem] rounded-[3px] border border-[#2a2a2a] bg-[#0d0d0d] px-2 text-[13px] font-mono text-white focus:border-[var(--accent-amber)] focus:outline-none disabled:opacity-50"
      >
        {defaultSeconds !== null && (
          <option value={DEFAULT_VALUE}>{`Default (${formatIntervalSeconds(defaultSeconds)})`}</option>
        )}
        {defaultSeconds === null && current === null && <option value={DEFAULT_VALUE}>Default</option>}
        {orphan !== null && <option value={String(orphan)}>{formatIntervalSeconds(orphan)}</option>}
        {presets.map((p) => (
          <option key={p} value={String(p)}>
            {formatIntervalSeconds(p)}
          </option>
        ))}
      </select>
    </div>
  );
});
TaskScheduleSelect.displayName = 'TaskScheduleSelect';
