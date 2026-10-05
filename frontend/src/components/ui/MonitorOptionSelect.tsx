import React, { useId } from 'react';
import { MONITOR_OPTIONS, type MonitorOption } from '@/types/monitoring';
import { inputClass } from '@/components/settings/formClasses';

export interface MonitorOptionSelectProps {
  value: MonitorOption;
  onChange: (value: MonitorOption) => void;
  id?: string;
  name?: string;
  /** Visible label rendered above the select. */
  label?: string;
  disabled?: boolean;
  'aria-label'?: string;
  className?: string;
}

/** Select over the native library's monitor options (full width on mobile, 44px tall via the global input rule). */
export const MonitorOptionSelect: React.FC<MonitorOptionSelectProps> = ({
  value,
  onChange,
  id,
  name = 'monitor-option',
  label,
  disabled = false,
  className = '',
  ...rest
}) => {
  const generatedId = useId();
  const selectId = id ?? generatedId;
  const select = (
  <select
    id={selectId}
    name={name}
    value={value}
    disabled={disabled}
    aria-label={rest['aria-label'] ?? (label || id ? undefined : 'Monitor option')}
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
  return label ? (
    <div>
      <label htmlFor={selectId} className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5">
        {label}
      </label>
      {select}
    </div>
  ) : (
    select
  );
};
