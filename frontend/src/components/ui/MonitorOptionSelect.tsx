import React, { useId } from 'react';
import { MONITOR_OPTIONS, type MonitorOption } from '@/types/monitoring';
import { inputClass } from '@/components/settings/formClasses';

export type MonitorSelectValue = MonitorOption | 'default';

export interface MonitorOptionSelectProps<V extends MonitorSelectValue = MonitorOption> {
  value: V;
  onChange: (value: V) => void;
  id?: string;
  name?: string;
  /** Visible label rendered above the select. */
  label?: string;
  disabled?: boolean;
  'aria-label'?: string;
  className?: string;
  defaultOptionLabel?: string;
  defaultOptionValue?: V;
}

/** Select over the native library's monitor options (full width on mobile, 44px tall via the global input rule). */
export function MonitorOptionSelect<V extends MonitorSelectValue = MonitorOption>({
  value,
  onChange,
  id,
  name = 'monitor-option',
  label,
  disabled = false,
  className = '',
  defaultOptionLabel,
  defaultOptionValue,
  ...rest
}: MonitorOptionSelectProps<V>): React.ReactElement {
  const generatedId = useId();
  const selectId = id ?? generatedId;
  const defaultVal = defaultOptionValue ?? ('default' as V);
  const select = (
    <select
      id={selectId}
      name={name}
      value={value}
      disabled={disabled}
      aria-label={rest['aria-label'] ?? (label || id ? undefined : 'Monitor option')}
      onChange={(e) => onChange(e.target.value as V)}
      className={`${inputClass} ${className}`}
    >
      {defaultOptionLabel && (
        <option value={defaultVal}>
          {defaultOptionLabel}
        </option>
      )}
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
}
