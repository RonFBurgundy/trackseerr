import React, { useId } from 'react';

export const inputClass =
  'w-full bg-[var(--bg-canvas)] border border-[var(--border-default)] rounded-[3px] px-2.5 sm:px-3 py-1.5 sm:py-2 text-[13px] sm:text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-[var(--accent-amber)] min-h-[36px] sm:min-h-[38px] disabled:opacity-50';

export const labelClass =
  'block text-xs uppercase font-mono tracking-wider text-[var(--text-secondary)] mb-1 sm:mb-1.5';

export interface FormFieldProps {
  label: string;
  /** Overrides the generated id. When the child is a single element it receives this id automatically. */
  htmlFor?: string;
  /** Form name injected into a native input/select/textarea child that has none. Default: slug of the label. */
  name?: string;
  hint?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}

interface FieldChildProps {
  id?: string;
  name?: string;
  'aria-describedby'?: string;
}

const slug = (text: string): string =>
  text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'field';

const NATIVE_FIELDS: ReadonlyArray<string> = ['input', 'select', 'textarea'];

/**
 * Labelled field wrapper. Generates a stable id (or uses `htmlFor`), wires `<label htmlFor>`, and injects
 * `id` / `name` into a single child element that lacks them so call sites cannot forget.
 */
export const FormField: React.FC<FormFieldProps> = ({
  label,
  htmlFor,
  name,
  hint,
  children,
  className = '',
}) => {
  const generatedId = useId();
  const child = React.isValidElement<FieldChildProps>(children) ? children : null;
  const id = htmlFor ?? child?.props.id ?? generatedId;
  const hintId = hint ? `${id}-hint` : undefined;
  let content: React.ReactNode = children;
  if (child) {
    const native = typeof child.type === 'string' && NATIVE_FIELDS.includes(child.type);
    content = React.cloneElement(child, {
      id,
      ...(native && !child.props.name ? { name: name ?? slug(label) } : {}),
      ...(hintId && !child.props['aria-describedby'] ? { 'aria-describedby': hintId } : {}),
    });
  }
  return (
    <div className={className}>
      <label htmlFor={id} className={labelClass}>
        {label}
      </label>
      {content}
      {hint && (
        <div id={hintId} className="mt-1 text-[11px] font-mono text-[var(--text-muted)]">
          {hint}
        </div>
      )}
    </div>
  );
};

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
      className={`p-2.5 sm:p-3 bg-[var(--bg-surface)] border text-xs font-mono rounded-[4px] ${tone} ${className}`}
    >
      {children}
    </div>
  );
};
