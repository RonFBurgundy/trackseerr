import React, { useId } from 'react';
import {
  LIST_MONITOR_MODES,
  LIST_MONITOR_MODE_HELP,
  LIST_MONITOR_MODE_LABELS,
  type ListMonitorMode,
} from '@/types/importLists';
import { inputClass } from '@/components/settings/formClasses';

export interface MonitorModeSelectProps {
  value: ListMonitorMode;
  onChange: (value: ListMonitorMode) => void;
  id?: string;
  name?: string;
  /** Visible label rendered above the select. */
  label?: string;
  disabled?: boolean;
  /** Smaller control for dense cards. */
  compact?: boolean;
  /** Show the help line for the selected mode under the select. */
  showHelp?: boolean;
  /** Modes the user may pick (default all). The current value is always listed so it can be displayed. */
  allowedModes?: ReadonlyArray<ListMonitorMode>;
  'aria-label'?: string;
  className?: string;
}

/** Select over list monitor modes (track / album / artist / none) with optional help text. */
export const MonitorModeSelect: React.FC<MonitorModeSelectProps> = ({
  value,
  onChange,
  id,
  name = 'monitor-mode',
  label,
  disabled = false,
  compact = false,
  showHelp = true,
  allowedModes = LIST_MONITOR_MODES,
  className = '',
  ...rest
}) => {
  const generatedId = useId();
  const selectId = id ?? generatedId;
  return (
  <div className={className}>
    {label && (
      <label htmlFor={selectId} className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5">
        {label}
      </label>
    )}
    <select
      id={selectId}
      name={name}
      value={value}
      disabled={disabled}
      aria-label={rest['aria-label'] ?? (label || id ? undefined : 'Monitor mode')}
      onChange={(e) => onChange(e.target.value as ListMonitorMode)}
      className={`${inputClass} ${compact ? 'text-xs' : ''}`}
    >
      {LIST_MONITOR_MODES.filter((m) => m === value || allowedModes.includes(m)).map((m) => (
        <option key={m} value={m} disabled={!allowedModes.includes(m)}>
          {LIST_MONITOR_MODE_LABELS[m]}
        </option>
      ))}
    </select>
    {showHelp && <p className="mt-1 text-[11px] font-mono text-neutral-500">{LIST_MONITOR_MODE_HELP[value]}</p>}
  </div>
  );
};
