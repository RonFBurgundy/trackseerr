import React, { useId, useState } from 'react';
import { FormField, TactileSwitch, TokenInput } from '@/components/ui';
import type { IndexerItem } from '@/types/models';
import type { QualityProfile } from '@/types/qualityProfiles';
import { SEEDED_RELEASE_PROFILE_NAME, type ReleaseProfile, type ReleaseProfileInput } from '@/types/releaseProfiles';
import { useReleaseProfileDraft } from '@/hooks/useReleaseProfileDraft';
import { inputClass } from '@/components/settings/formClasses';
import { EditorModalShell } from './EditorModalShell';

export type ReleaseProfileTarget = ReleaseProfile | 'new';

export interface ReleaseProfileEditorModalProps {
  target: ReleaseProfileTarget | null;
  indexers: readonly IndexerItem[];
  qualityProfiles: readonly QualityProfile[];
  onClose: () => void;
  onSave: (id: number | null, input: ReleaseProfileInput) => Promise<string | null>;
}

interface CheckListProps {
  legend: string;
  hint: string;
  prefix: string;
  options: ReadonlyArray<{ id: string; label: string }>;
  selected: readonly string[];
  onToggle: (id: string) => void;
}

const CheckList: React.FC<CheckListProps> = ({ legend, hint, prefix, options, selected, onToggle }) => (
  <fieldset className="space-y-1">
    <legend className="mb-0.5 block text-xs font-mono uppercase tracking-wider text-neutral-300">{legend}</legend>
    <p className="text-[11px] font-mono text-[var(--text-muted)]">{hint}</p>
    {options.length === 0 ? (
      <p className="text-xs font-mono text-neutral-500">None configured.</p>
    ) : (
      <div className="grid grid-cols-1 gap-x-4 sm:grid-cols-2">
        {options.map((o) => {
          const id = `${prefix}-${o.id}`;
          return (
            <label key={o.id} htmlFor={id} className="flex min-h-[36px] cursor-pointer items-center gap-2 text-xs font-mono text-neutral-200">
              <input
                id={id}
                name={`${prefix}[]`}
                type="checkbox"
                checked={selected.includes(o.id)}
                onChange={() => onToggle(o.id)}
                className="h-5 w-5 accent-[#e5a00d]"
              />
              <span className="truncate">{o.label}</span>
            </label>
          );
        })}
      </div>
    )}
  </fieldset>
);

/** Backend messages read `required term '...': reason` / `ignored term '...': reason`; route each to its own field. */
function termError(message: string | null, field: 'required' | 'ignored'): string | null {
  return message !== null && message.startsWith(`${field} term`) ? message : null;
}

const EditorBody: React.FC<Omit<ReleaseProfileEditorModalProps, 'target'> & { target: ReleaseProfileTarget }> = ({
  target,
  indexers,
  qualityProfiles,
  onClose,
  onSave,
}) => {
  const uid = useId();
  const existing = target === 'new' ? null : target;
  const draft = useReleaseProfileDraft(existing);
  const [saving, setSaving] = useState<boolean>(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const formId = `${uid}-form`;
  const requiredError = termError(serverError, 'required');
  const ignoredError = termError(serverError, 'ignored');
  const generalError = requiredError || ignoredError ? null : serverError;

  const submit = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault();
    const input = draft.toInput();
    if (!input) return;
    setSaving(true);
    setServerError(null);
    const err = await onSave(existing?.id ?? null, input);
    setSaving(false);
    if (err) setServerError(err);
    else onClose();
  };

  return (
    <EditorModalShell
      onClose={onClose}
      title={existing ? 'Edit Release Profile' : 'New Release Profile'}
      subtitle="Applied before scoring. Any ignored term rejects a release; at least one required term must match."
      formId={formId}
      saving={saving}
      disabled={draft.problem !== null}
      error={generalError ?? draft.problem}
    >
      <form id={formId} onSubmit={(e) => void submit(e)} className="space-y-5">
        {existing?.name === SEEDED_RELEASE_PROFILE_NAME && (
          <p className="rounded-[4px] border border-[var(--border-default)] bg-[var(--bg-surface)] p-2 text-[11px] font-mono text-neutral-400">
            Suggested default: this profile ships with TrackSeerr as our suggestion for dropping transcodes and fake lossless. It is not a community standard; edit or disable it freely.
          </p>
        )}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <FormField label="Profile name" htmlFor={`${uid}-name`} className="sm:flex-1">
            <input
              id={`${uid}-name`}
              name="release_profile_name"
              type="text"
              required
              maxLength={120}
              autoComplete="off"
              value={draft.name}
              onChange={(e) => draft.setName(e.target.value)}
              className={inputClass}
            />
          </FormField>
          <TactileSwitch id={`${uid}-enabled`} name="release_profile_enabled" label="Enabled" checked={draft.enabled} onChange={draft.setEnabled} />
        </div>
        <TokenInput
          label="Must contain (any of)"
          name="release_profile_required"
          tokens={draft.required}
          onChange={draft.setRequired}
          placeholder="word, or /regex/ then Enter"
          hint="Case-insensitive substring, or /pattern/ for a regex. A release needs at least one. Leave empty to require nothing."
          error={requiredError}
        />
        <TokenInput
          label="Must not contain"
          name="release_profile_ignored"
          tokens={draft.ignored}
          onChange={draft.setIgnored}
          placeholder="word, or /regex/ then Enter"
          hint="Any match rejects the release."
          error={ignoredError}
        />
        <CheckList
          legend="Indexers"
          hint="Restrict this profile to these indexers. None ticked = every indexer."
          prefix={`${uid}-indexer`}
          options={indexers.map((i) => ({ id: String(i.id), label: i.name }))}
          selected={draft.indexerIds}
          onToggle={draft.toggleIndexer}
        />
        <CheckList
          legend="Quality profiles"
          hint="Apply only when downloading under these quality profiles. None ticked = all profiles."
          prefix={`${uid}-qp`}
          options={qualityProfiles.map((p) => ({ id: p.id, label: p.name }))}
          selected={draft.qualityProfileIds}
          onToggle={draft.toggleQualityProfile}
        />
      </form>
    </EditorModalShell>
  );
};

export const ReleaseProfileEditorModal: React.FC<ReleaseProfileEditorModalProps> = ({ target, ...rest }) => {
  if (target === null) return null;
  return <EditorBody key={target === 'new' ? 'new' : target.id} target={target} {...rest} />;
};
