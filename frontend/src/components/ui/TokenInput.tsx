import React, { useId, useState } from 'react';
import { Plus, X } from 'lucide-react';
import { TapeDeckButton } from './TapeDeckButton';
import { inputClass, labelClass } from './FormField';

/** True for a `/pattern/` or `/pattern/flags` term (matches the backend's regex-term syntax). */
export function isRegexTerm(term: string): boolean {
  return /^\/.+\/[a-z]*$/.test(term);
}

export interface TokenInputProps {
  label: string;
  name: string;
  tokens: readonly string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  hint?: React.ReactNode;
  /** Inline validation message (for example from the backend). */
  error?: string | null;
}

/** Labelled token field: Enter (or the Add key) commits a term; `/regex/` terms render in mono with a badge. */
export const TokenInput: React.FC<TokenInputProps> = ({ label, name, tokens, onChange, placeholder, hint, error }) => {
  const id = useId();
  const [draft, setDraft] = useState<string>('');

  const commit = (): void => {
    const term = draft.trim();
    if (term && !tokens.includes(term)) onChange([...tokens, term]);
    setDraft('');
  };

  return (
    <div>
      <label htmlFor={id} className={labelClass}>
        {label}
      </label>
      <div className="flex gap-2">
        <input
          id={id}
          name={name}
          type="text"
          autoComplete="off"
          value={draft}
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              commit();
            }
          }}
          className={`${inputClass} font-mono`}
          aria-describedby={hint ? `${id}-hint` : undefined}
        />
        <TapeDeckButton size="sm" aria-label={`Add ${label} term`} disabled={draft.trim() === ''} onClick={commit} icon={<Plus className="h-3.5 w-3.5" />} />
      </div>
      {tokens.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-1.5" aria-label={`${label} terms`}>
          {tokens.map((t) => (
            <li
              key={t}
              className="inline-flex max-w-full items-center gap-1.5 rounded-[3px] border border-[var(--border-default)] bg-[var(--bg-surface-elevated)] py-1 pl-2 pr-1 text-xs"
            >
              <span className={`truncate ${isRegexTerm(t) ? 'font-mono text-[var(--accent-amber)]' : 'text-[var(--text-primary)]'}`}>{t}</span>
              {isRegexTerm(t) && (
                <span className="rounded-[2px] border border-[var(--accent-amber)]/40 px-1 text-[9px] font-mono uppercase text-[var(--accent-amber)]">regex</span>
              )}
              <button
                type="button"
                aria-label={`Remove ${t}`}
                onClick={() => onChange(tokens.filter((x) => x !== t))}
                className="inline-flex h-6 w-6 items-center justify-center rounded-[2px] text-neutral-500 hover:text-[var(--status-error)]"
              >
                <X className="h-3 w-3" />
              </button>
            </li>
          ))}
        </ul>
      )}
      {hint && (
        <div id={`${id}-hint`} className="mt-1 text-[11px] font-mono text-[var(--text-muted)]">
          {hint}
        </div>
      )}
      {error && (
        <p role="alert" className="mt-1 text-[11px] font-mono text-[var(--status-error)]">
          {error}
        </p>
      )}
    </div>
  );
};
