import React, { useEffect, useRef } from 'react';
import { Loader2 } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';
import { inputClass } from '@/components/settings/formClasses';

/** Select styling shared by every bulk-editor field. */
export const bulkSelectClass = inputClass;

export interface BulkFieldProps {
  id: string;
  label: string;
  children: React.ReactNode;
  className?: string;
}

/** Compact labelled field cell: tiny uppercase label over the control. */
export const BulkField: React.FC<BulkFieldProps> = ({ id, label, children, className = '' }) => (
  <div className={`min-w-0 ${className}`}>
    <label htmlFor={id} className="mb-0.5 block truncate text-[10px] font-mono uppercase tracking-wider text-neutral-400">
      {label}
    </label>
    {children}
  </div>
);

export interface BulkEditSheetProps {
  ariaLabel: string;
  /** "12 selected" style count. */
  countLabel: string;
  /** Select-all / clear / done keys, right of the count. */
  headerActions: React.ReactNode;
  /** Optional line under the header (hints, warnings). */
  notice?: React.ReactNode;
  /** The field controls; laid out as 2 columns on phones, inline from `sm`. */
  children: React.ReactNode;
  applyDisabled: boolean;
  busy: boolean;
  onApply: () => void;
}

/**
 * Shared mass-editor chrome. Phones: sticky bottom sheet above the audio player (honours
 * `--player-offset` and the safe area). Desktop: one inline card row with Apply on the right.
 */
export const BulkEditSheet: React.FC<BulkEditSheetProps> = ({
  ariaLabel,
  countLabel,
  headerActions,
  notice,
  children,
  applyDisabled,
  busy,
  onApply,
}) => {
  const ref = useRef<HTMLElement>(null);

  // Publish the sheet height so list scrollers can pad their bottom (phones only, via CSS).
  useEffect(() => {
    const root = document.documentElement;
    const el = ref.current;
    if (!el) return undefined;
    const publish = (): void => root.style.setProperty('--bulk-sheet-offset', `${el.offsetHeight}px`);
    publish();
    const observer = new ResizeObserver(publish);
    observer.observe(el);
    return () => {
      observer.disconnect();
      root.style.removeProperty('--bulk-sheet-offset');
    };
  }, []);

  return (
  <section
    ref={ref}
    aria-label={ariaLabel}
    style={{ bottom: 'var(--player-offset, 0px)' }}
    className="fixed inset-x-0 z-30 max-h-[60dvh] overflow-y-auto space-y-2 border-t border-[#2a2a2a] bg-[#121212] p-2 pb-safe shadow-[0_-6px_16px_rgba(0,0,0,0.7)] sm:static sm:z-auto sm:max-h-none sm:overflow-visible sm:rounded-[4px] sm:border sm:p-3 sm:shadow-none"
  >
    <div className="flex items-center justify-between gap-2">
      <span className="min-w-0 truncate text-xs font-mono font-bold uppercase text-[#e5a00d]">{countLabel}</span>
      <div className="flex shrink-0 items-center gap-1.5">{headerActions}</div>
    </div>
    <div className="grid grid-cols-2 items-end gap-2 sm:flex sm:flex-wrap sm:gap-3">
      {children}
      <div className="col-span-2 sm:col-span-1 sm:ml-auto">
        <TapeDeckButton
          type="button"
          size="sm"
          variant="amber"
          disabled={applyDisabled || busy}
          onClick={onApply}
          className="w-full sm:w-auto"
          icon={busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
        >
          Apply
        </TapeDeckButton>
      </div>
    </div>
    {notice}
  </section>
  );
};
