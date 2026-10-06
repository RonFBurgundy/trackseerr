import React, { useId, useRef, useState } from 'react';
import { Check, Loader2, Plus } from 'lucide-react';
import type { Schema } from '@/types';
import { normalizeTagLabel, tagLabelProblem } from '@/types/tags';
import type { TagMutationResult } from '@/hooks/useTags';
import { inputClass } from './FormField';

interface TagPickerCommon {
  label: string;
  /** Form field name of the text input. */
  name: string;
  /** The existing tag catalogue (from `useTags`). */
  tags: readonly Schema<'TagOut'>[];
  /** Creates a tag server-side; the picker selects it on success and shows `error` otherwise. Omit to disable inline create. */
  onCreate?: (label: string) => Promise<TagMutationResult>;
  loading?: boolean;
  disabled?: boolean;
  hint?: React.ReactNode;
  /** Error from loading the catalogue. */
  loadError?: string | null;
  /** Tiny uppercase label, for the bulk-edit sheet. */
  compact?: boolean;
}

export type TagPickerProps = TagPickerCommon &
  (
    | { mode: 'id'; value: readonly number[]; onChange: (next: number[]) => void }
    | { mode: 'label'; value: readonly string[]; onChange: (next: string[]) => void }
  );

const chipBase =
  "relative inline-flex h-7 max-w-full items-center gap-1 rounded-[3px] border px-2 text-xs font-mono transition-colors after:absolute after:-inset-0.5 after:content-[''] focus-visible:outline-none focus-visible:border-[var(--accent-amber)] disabled:opacity-50";
const chipOff = 'border-[var(--border-default)] bg-[var(--bg-surface-elevated)] text-[var(--text-secondary)] hover:text-white';
const chipOn = 'border-[var(--accent-amber)]/60 bg-[var(--accent-amber)]/10 text-[var(--accent-amber)]';

/**
 * Multi-select of existing tags as toggle chips, plus inline create. Type to filter; Enter toggles the single or
 * exact match, or creates the typed label. `mode` picks the value type: tag ids (artists) or labels (profiles, lists).
 */
export const TagPicker: React.FC<TagPickerProps> = (props) => {
  const { label, name, tags, onCreate, loading = false, disabled = false, hint, loadError = null, compact = false } = props;
  const id = useId();
  const [draft, setDraft] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState<boolean>(false);
  // Latest props for use after an await (create resolves after the parent may have re-rendered).
  const latest = useRef(props);
  latest.current = props;

  const query = normalizeTagLabel(draft);
  const problem = draft.trim() === '' ? null : tagLabelProblem(draft);
  const exact = tags.find((t) => t.label === query) ?? null;
  const visible = query === '' ? tags : tags.filter((t) => t.label.includes(query));
  const canCreate = onCreate !== undefined && query !== '' && exact === null && problem === null;

  const isSelected = (tag: Schema<'TagOut'>): boolean =>
    props.mode === 'id' ? props.value.includes(tag.id) : props.value.includes(tag.label);

  const select = (tag: Schema<'TagOut'>, on: boolean): void => {
    const cur = latest.current;
    if (cur.mode === 'id') {
      const rest = cur.value.filter((v) => v !== tag.id);
      cur.onChange(on ? [...rest, tag.id] : rest);
    } else {
      const rest = cur.value.filter((v) => v !== tag.label);
      cur.onChange(on ? [...rest, tag.label] : rest);
    }
  };

  // Label mode can carry labels that are not in the catalogue (stale or removed); keep them visible and removable.
  const orphans: string[] =
    props.mode === 'label' ? props.value.filter((v) => !tags.some((t) => t.label === v)) : [];

  const create = async (): Promise<void> => {
    if (!onCreate || !canCreate || creating || disabled) return;
    setCreating(true);
    setError(null);
    const res = await onCreate(query);
    setCreating(false);
    if (res.ok) {
      select(res.tag, true);
      setDraft('');
    } else {
      setError(res.error);
    }
  };

  const onEnter = (): void => {
    setError(null);
    if (exact) {
      select(exact, true);
      setDraft('');
    } else if (canCreate) {
      void create();
    } else if (visible.length === 1 && problem === null) {
      select(visible[0], !isSelected(visible[0]));
      setDraft('');
    } else if (problem) {
      setError(problem);
    }
  };

  const shownError = error ?? problem ?? loadError;
  const labelClass = compact
    ? 'mb-0.5 block truncate text-[10px] font-mono uppercase tracking-wider text-neutral-400'
    : 'mb-1 block text-xs font-mono uppercase tracking-wider text-[var(--text-secondary)] sm:mb-1.5';

  return (
    <div className="min-w-0">
      <label htmlFor={id} className={labelClass}>
        {label}
      </label>
      <div className="relative">
        <input
          id={id}
          name={name}
          type="text"
          autoComplete="off"
          autoCapitalize="none"
          enterKeyHint="done"
          maxLength={80}
          disabled={disabled}
          value={draft}
          placeholder={loading ? 'Loading tags...' : 'Filter or create a tag'}
          aria-invalid={problem !== null}
          aria-describedby={shownError ? `${id}-msg` : hint ? `${id}-hint` : undefined}
          onChange={(e) => {
            setDraft(e.target.value);
            setError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              onEnter();
            } else if (e.key === 'Escape' && draft !== '') {
              e.stopPropagation();
              setDraft('');
              setError(null);
            }
          }}
          className={`${inputClass} font-mono`}
        />
        {creating && <Loader2 className="absolute right-2 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-neutral-400" aria-hidden="true" />}
      </div>
      <div role="group" aria-label={`${label} options`} className="mt-1.5 flex max-h-28 flex-wrap gap-1.5 overflow-y-auto">
        {visible.map((t) => {
          const on = isSelected(t);
          return (
            <button
              key={t.id}
              type="button"
              aria-pressed={on}
              disabled={disabled}
              onClick={() => select(t, !on)}
              className={`${chipBase} ${on ? chipOn : chipOff}`}
            >
              {on && <Check className="h-3 w-3 shrink-0" aria-hidden="true" />}
              <span className="truncate">{t.label}</span>
            </button>
          );
        })}
        {query === '' &&
          orphans.map((o) => (
            <button
              key={`orphan-${o}`}
              type="button"
              aria-pressed
              disabled={disabled}
              title="Not in the tag list"
              onClick={() => {
                const cur = latest.current;
                if (cur.mode === 'label') cur.onChange(cur.value.filter((v) => v !== o));
              }}
              className={`${chipBase} ${chipOn}`}
            >
              <Check className="h-3 w-3 shrink-0" aria-hidden="true" />
              <span className="truncate">{o}</span>
            </button>
          ))}
        {canCreate && (
          <button
            type="button"
            disabled={disabled || creating}
            onClick={() => void create()}
            className={`${chipBase} border-dashed border-[var(--accent-amber)]/50 text-[var(--accent-amber)] hover:bg-[var(--accent-amber)]/10`}
          >
            <Plus className="h-3 w-3 shrink-0" aria-hidden="true" />
            <span className="truncate">{`Create "${query}"`}</span>
          </button>
        )}
        {!loading && tags.length === 0 && query === '' && orphans.length === 0 && (
          <span className="text-[11px] font-mono text-[var(--text-muted)]">No tags yet. Type a name to create one.</span>
        )}
        {!loading && tags.length > 0 && visible.length === 0 && !canCreate && problem === null && (
          <span className="text-[11px] font-mono text-[var(--text-muted)]">No matching tags.</span>
        )}
      </div>
      {shownError ? (
        <p id={`${id}-msg`} role="alert" className="mt-1 text-[11px] font-mono text-[var(--status-error)]">
          {shownError}
        </p>
      ) : (
        hint && (
          <div id={`${id}-hint`} className="mt-1 text-[11px] font-mono text-[var(--text-muted)]">
            {hint}
          </div>
        )
      )}
    </div>
  );
};
