import React from 'react';
import { MONITOR_OPTIONS, type MonitorOption } from '@/types/monitoring';
import { inputClass } from '@/components/settings/formClasses';

export interface MonitorOptionSelectProps {
  value: MonitorOption;
  onChange: (value: MonitorOption) => void;
  id?: string;
  disabled?: boolean;
  'aria-label'?: string;
  className?: string;
}

/** Select over the native library's monitor options (full width on mobile, 44px tall via the global input rule). */
export const MonitorOptionSelect: React.FC<MonitorOptionSelectProps> = ({
  value,
  onChange,
  id,
  disabled = false,
  className = '',
  ...rest
}) => (
  <select
    id={id}
    value={value}
    disabled={disabled}
    aria-label={rest['aria-label']}
    onChange={(e) => onChange(e.target.value as MonitorOption)}
    className={`${inputClass} ${className}`}
  >
    {MONITOR_OPTIONS.map((o) => (
      <option key={o.value} value={o.value}>
        {o.label}
      </option>
    ))}
  </select>
);
