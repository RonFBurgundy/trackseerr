import React, { useEffect, useId, useRef, useState } from 'react';
import { ListFilter, Loader2, X } from 'lucide-react';

export interface DiscographyFilterProps {
  value: string;
  onChange: (value: string) => void;
  onClear: () => void;
  /** Track lookup in flight. */
  searching?: boolean;
}

const INPUT =
  'min-w-0 flex-1 bg-transparent text-[13px] font-mono text-white placeholder:text-[var(--text-muted)] outline-none';

/**
 * Narrows the discography by album or track title. A funnel icon (never a magnifier: this is not the global search).
 * Desktop shows the field inline; on phones it collapses to the icon key and expands over the header row.
 * The parent row must be `relative`.
 */
export const DiscographyFilter: React.FC<DiscographyFilterProps> = ({ value, onChange, onClear, searching = false }) => {
  const desktopId = useId();
  const mobileId = useId();
  const [open, setOpen] = useState<boolean>(false);
  const mobileInput = useRef<HTMLInputElement | null>(null);
  const hasValue = value.length > 0;

  useEffect(() => {
    if (open) mobileInput.current?.focus();
  }, [open]);

  const closeMobile = (): void => {
    onClear();
    setOpen(false);
  };

  return (
    <>
      <div className="hidden sm:flex h-8 shrink-0 items-center gap-1.5 rounded-[3px] border border-[#2a2a2a] bg-[#0d0d0d] px-2 focus-within:border-[var(--accent-amber)]">
        {searching ? (
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-[var(--accent-amber)]" aria-hidden="true" />
        ) : (
          <ListFilter className={`h-3.5 w-3.5 shrink-0 ${hasValue ? 'text-[var(--accent-amber)]' : 'text-[var(--text-muted)]'}`} aria-hidden="true" />
        )}
        <label htmlFor={desktopId} className="sr-only">
          Filter this artist&apos;s albums and tracks
        </label>
        <input
          id={desktopId}
          name="discography_filter"
          type="text"
          autoComplete="off"
          spellCheck={false}
          placeholder="Filter this artist…"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && hasValue) {
              e.preventDefault();
              onClear();
            }
          }}
          className={`${INPUT} w-36 lg:w-44`}
        />
        {hasValue && (
          <button type="button" aria-label="Clear filter" onClick={onClear} className="shrink-0 text-[var(--text-secondary)] hover:text-white">
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      <button
        type="button"
        aria-label="Filter this artist"
        title="Filter this artist"
        aria-expanded={open}
        onClick={() => setOpen(true)}
        className="relative sm:hidden flex h-9 w-9 shrink-0 items-center justify-center rounded-[3px] border border-[#2a2a2a] bg-[#141414] text-[var(--text-secondary)] after:absolute after:-inset-0.5 after:content-['']"
      >
        <ListFilter className={`h-4 w-4 ${hasValue ? 'text-[var(--accent-amber)]' : ''}`} />
        {hasValue && <span className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-[var(--accent-amber)]" aria-hidden="true" />}
      </button>

      {(open || hasValue) && (
        <div className="absolute inset-0 z-20 flex items-center gap-1.5 bg-[#0d0d0d] px-2 sm:hidden">
          {searching ? (
            <Loader2 className="h-4 w-4 shrink-0 animate-spin text-[var(--accent-amber)]" aria-hidden="true" />
          ) : (
            <ListFilter className="h-4 w-4 shrink-0 text-[var(--accent-amber)]" aria-hidden="true" />
          )}
          <label htmlFor={mobileId} className="sr-only">
            Filter this artist&apos;s albums and tracks
          </label>
          <input
            id={mobileId}
            ref={mobileInput}
            name="discography_filter_mobile"
            type="text"
            autoComplete="off"
            spellCheck={false}
            placeholder="Filter this artist…"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            className={INPUT}
          />
          <button type="button" aria-label="Clear and close filter" onClick={closeMobile} className="flex h-9 w-9 shrink-0 items-center justify-center text-[var(--text-secondary)] hover:text-white">
            <X className="h-4 w-4" />
          </button>
        </div>
      )}
    </>
  );
};
