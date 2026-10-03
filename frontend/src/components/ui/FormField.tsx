import React from 'react';

export const inputClass =
  'w-full bg-[var(--bg-canvas)] border border-[var(--border-default)] rounded-[3px] px-3 py-2 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-[var(--accent-amber)] min-h-[44px] sm:min-h-[38px] disabled:opacity-50';

export const labelClass =
  'block text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] mb-1.5';

export interface FormFieldProps {
  label: string;
  htmlFor?: string;
  hint?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}

export const FormField: React.FC<FormFieldProps> = ({
  label,
  htmlFor,
  hint,
  children,
  className = '',
}) => (
  <div className={className}>
    <label htmlFor={htmlFor} className={labelClass}>
      {label}
    </label>
    {children}
    {hint && <div className="mt-1 text-[11px] font-mono text-[var(--text-muted)]">{hint}</div>}
  </div>
);

export interface StatusMessageProps {
  variant: 'error' | 'success' | 'info';
  children: React.ReactNode;
  className?: string;
}

export const StatusMessage: React.FC<StatusMessageProps> = ({
  variant,
  children,
  className = '',
}) => {
  const tone = {
    error: 'border-[var(--status-error)] text-[var(--status-error)]',
    success: 'border-[var(--status-success)] text-[var(--status-success)]',
    info: 'border-[var(--border-default)] text-[var(--text-secondary)]',
  }[variant];
  return (
    <div
      role={variant === 'error' ? 'alert' : 'status'}
      className={`p-3 bg-[var(--bg-surface)] border text-xs font-mono rounded-[4px] ${tone} ${className}`}
    >
      {children}
    </div>
  );
};
