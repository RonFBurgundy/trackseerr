import React from 'react';
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
  disabled = false,
  compact = false,
  showHelp = true,
  allowedModes = LIST_MONITOR_MODES,
  className = '',
  ...rest
}) => (
  <div className={className}>
    <select
      id={id}
      value={value}
      disabled={disabled}
      aria-label={rest['aria-label']}
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
