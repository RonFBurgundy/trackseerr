import React, { useId, useState } from 'react';
import { FormField, TactileSwitch, TagPicker } from '@/components/ui';
import type { LibraryManagerMode } from '@/types/models';
import type { DelayProfile, DelayProfileInput } from '@/types/delayProfiles';
import { RELEASE_PROTOCOLS, type ReleaseProtocol } from '@/types/qualityProfiles';
import { useDelayProfileDraft } from '@/hooks/useDelayProfileDraft';
import { useTags } from '@/hooks/useTags';
import { inputClass } from '@/components/settings/formClasses';
import { EditorModalShell } from './EditorModalShell';

export type DelayProfileTarget = DelayProfile | 'new';

export interface DelayProfileEditorModalProps {
  target: DelayProfileTarget | null;
  onClose: () => void;
  onSave: (id: number | null, input: DelayProfileInput) => Promise<string | null>;
  /** In Lidarr mode native tags do not apply: the picker is hidden and existing tags are kept as they are. */
  libraryMode: LibraryManagerMode;
}

function isProtocol(value: string): value is ReleaseProtocol {
  return RELEASE_PROTOCOLS.some((p) => p === value);
}

const EditorBody: React.FC<{
  target: DelayProfileTarget;
  onClose: () => void;
  onSave: DelayProfileEditorModalProps['onSave'];
  libraryMode: LibraryManagerMode;
}> = ({ target, onClose, onSave, libraryMode }) => {
  const uid = useId();
  const existing = target === 'new' ? null : target;
  const { draft, patch, problem, toInput } = useDelayProfileDraft(existing);
  const tagCatalogue = useTags(libraryMode !== 'lidarr');
  const [saving, setSaving] = useState<boolean>(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const formId = `${uid}-form`;
  const isDefault = existing?.is_default ?? false;

  const submit = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault();
    const input = toInput();
    if (!input) return;
    setSaving(true);
    setServerError(null);
    const err = await onSave(existing?.id ?? null, input);
    setSaving(false);
    if (err) setServerError(err);
    else onClose();
  };

  const delayFields: Array<{ key: ReleaseProtocol; label: string }> = [
    { key: 'usenet', label: 'Usenet delay (min)' },
    { key: 'torrent', label: 'Torrent delay (min)' },
    { key: 'soulseek', label: 'Soulseek delay (min)' },
  ];

  return (
    <EditorModalShell
      title={existing ? (isDefault ? 'Edit Default Delay Profile' : 'Edit Delay Profile') : 'New Delay Profile'}
      onClose={onClose}
      formId={formId}
      saving={saving}
      disabled={problem !== null}
      error={serverError ?? problem}
    >
      <form id={formId} onSubmit={(e) => void submit(e)} className="space-y-4">
        {!isDefault && (
          <FormField label="Profile name" htmlFor={`${uid}-name`}>
            <input
              id={`${uid}-name`}
              name="delay_profile_name"
              type="text"
              required
              maxLength={120}
              autoComplete="off"
              value={draft.name}
              onChange={(e) => patch({ name: e.target.value })}
              className={inputClass}
            />
          </FormField>
        )}
        <FormField label="Preferred protocol" htmlFor={`${uid}-protocol`} hint="Tried first when a release is available on several protocols.">
          <select
            id={`${uid}-protocol`}
            name="preferred_protocol"
            value={draft.preferred}
            onChange={(e) => {
              if (isProtocol(e.target.value)) patch({ preferred: e.target.value });
            }}
            className={inputClass}
          >
            {RELEASE_PROTOCOLS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </FormField>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {delayFields.map((f) => (
            <FormField key={f.key} label={f.label} htmlFor={`${uid}-${f.key}`}>
              <input
                id={`${uid}-${f.key}`}
                name={`delay_${f.key}_minutes`}
                type="number"
                min={0}
                step={1}
                value={draft[f.key]}
                onChange={(e) => patch({ [f.key]: e.target.value })}
                className={`${inputClass} font-mono`}
              />
            </FormField>
          ))}
        </div>
        <p className="-mt-2 text-[11px] font-mono text-[var(--text-muted)]">A release waits this long before it is grabbed, unless a bypass below applies. 0 = grab immediately.</p>
        <TactileSwitch
          id={`${uid}-bypass-highest`}
          name="bypass_if_highest_quality"
          label="Bypass if highest quality"
          checked={draft.bypassHighest}
          onChange={(v) => patch({ bypassHighest: v })}
        />
        <FormField label="Bypass if score above" htmlFor={`${uid}-bypass-score`} hint="Custom-format score that skips the delay. Blank = never bypass on score.">
          <input
            id={`${uid}-bypass-score`}
            name="bypass_if_above_score"
            type="number"
            step={1}
            placeholder="none"
            value={draft.bypassScore}
            onChange={(e) => patch({ bypassScore: e.target.value })}
            className={`${inputClass} font-mono`}
          />
        </FormField>
        {!isDefault && libraryMode !== 'lidarr' && (
          <TagPicker
            mode="label"
            label="Tags"
            name="delay_profile_tags"
            tags={tagCatalogue.tags}
            loading={tagCatalogue.loading}
            loadError={tagCatalogue.loadError}
            onCreate={tagCatalogue.create}
            value={draft.tags}
            onChange={(tags) => patch({ tags })}
            hint="Applies to artists with any of these tags; untagged profile is the fallback"
          />
        )}
      </form>
    </EditorModalShell>
  );
};

export const DelayProfileEditorModal: React.FC<DelayProfileEditorModalProps> = ({ target, onClose, onSave, libraryMode }) => {
  if (target === null) return null;
  return (
    <EditorBody
      key={target === 'new' ? 'new' : target.id}
      target={target}
      onClose={onClose}
      onSave={onSave}
      libraryMode={libraryMode}
    />
  );
};
