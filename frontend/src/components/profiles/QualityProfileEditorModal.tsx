import React, { useId, useState } from 'react';
import { FormField, TactileSwitch } from '@/components/ui';
import type { CustomFormat } from '@/types/customFormats';
import type { QualityDefinition } from '@/types/qualityDefinitions';
import type { QualityProfile, QualityProfileInput } from '@/types/qualityProfiles';
import { useQualityProfileDraft } from '@/hooks/useQualityProfileDraft';
import { inputClass } from '@/components/settings/formClasses';
import { EditorModalShell } from './EditorModalShell';
import { FormatScoreTable } from './FormatScoreTable';
import { QualityEntryList } from './QualityEntryList';
import { ReleaseTester } from './ReleaseTester';

export type QualityProfileTarget = QualityProfile | 'new';

export interface QualityProfileEditorModalProps {
  target: QualityProfileTarget | null;
  definitions: readonly QualityDefinition[];
  formats: readonly CustomFormat[];
  onClose: () => void;
  /** Resolves an inline error message, or null on success. */
  onSave: (input: QualityProfileInput) => Promise<string | null>;
}

const Group: React.FC<{ title: string; hint?: string; children: React.ReactNode }> = ({ title, hint, children }) => (
  <section className="space-y-2">
    <div>
      <h4 className="text-xs font-bold uppercase tracking-wider text-white">{title}</h4>
      {hint && <p className="text-[11px] font-mono text-[var(--text-muted)]">{hint}</p>}
    </div>
    {children}
  </section>
);

interface BodyProps extends Omit<QualityProfileEditorModalProps, 'target'> {
  target: QualityProfileTarget;
}

const EditorBody: React.FC<BodyProps> = ({ target, definitions, formats, onClose, onSave }) => {
  const uid = useId();
  const existing = target === 'new' ? null : target;
  const draft = useQualityProfileDraft(existing, definitions);
  const [saving, setSaving] = useState<boolean>(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const formId = `${uid}-form`;
  const titleOf = (quality: string): string => definitions.find((d) => d.quality === quality)?.title ?? quality;

  const submit = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault();
    const input = draft.toInput();
    if (!input) return;
    setSaving(true);
    setServerError(null);
    const err = await onSave(input);
    setSaving(false);
    if (err) setServerError(err);
    else onClose();
  };

  return (
    <EditorModalShell
      onClose={onClose}
      maxWidth="sm:max-w-3xl"
      title={existing ? 'Edit Quality Profile' : 'New Quality Profile'}
      subtitle="Order is preference: the top quality is the best. Quality order beats custom-format score."
      formId={formId}
      saving={saving}
      disabled={draft.problem !== null}
      error={serverError ?? draft.problem}
    >
      <div className="space-y-5">
      <form id={formId} onSubmit={(e) => void submit(e)} className="space-y-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <FormField label="Profile name" htmlFor={`${uid}-name`} className="sm:flex-1">
            <input
              id={`${uid}-name`}
              name="quality_profile_name"
              type="text"
              required
              maxLength={120}
              autoComplete="off"
              value={draft.name}
              onChange={(e) => draft.setName(e.target.value)}
              className={inputClass}
            />
          </FormField>
          <TactileSwitch
            id={`${uid}-upgrades`}
            name="upgrade_allowed"
            label="Upgrades"
            checked={draft.upgradeAllowed}
            onChange={draft.setUpgradeAllowed}
            title="Replace files with better releases until the cutoff is met"
          />
        </div>

        <Group title="Qualities" hint="Drag the grip or use the arrows. Tick two or more boxes to group equivalent qualities into one tier.">
          <QualityEntryList
            entries={draft.entries}
            titleOf={titleOf}
            selected={draft.selected}
            onReorder={draft.reorder}
            onToggleAllowed={draft.toggleAllowed}
            onToggleSelected={draft.toggleSelected}
            onRenameGroup={draft.renameGroup}
            onCreateGroup={draft.createGroup}
            onUngroup={draft.ungroup}
          />
        </Group>

        <FormField label="Upgrade until quality (cutoff)" htmlFor={`${uid}-cutoff`} hint="Upgrades stop once a file reaches this entry. Only allowed entries are listed.">
          <select
            id={`${uid}-cutoff`}
            name="cutoff"
            value={draft.cutoff}
            onChange={(e) => draft.setCutoff(e.target.value)}
            className={inputClass}
          >
            {draft.cutoffOptions.map((label) => (
              <option key={label} value={label}>
                {titleOf(label)}
              </option>
            ))}
          </select>
        </FormField>

        <Group title="Custom format scores" hint="Scores add up per release. Positive scores are preferred, negative ones penalised. Blank = 0.">
          <FormatScoreTable formats={formats} scores={draft.scores} onScore={draft.setScore} />
        </Group>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <FormField label="Minimum format score" htmlFor={`${uid}-min`} hint="Releases scoring below this are rejected. -100 keeps penalties soft; 0 makes them bans.">
            <input id={`${uid}-min`} name="min_format_score" type="number" step={1} value={draft.minScore} onChange={(e) => draft.setMinScore(e.target.value)} className={`${inputClass} font-mono`} />
          </FormField>
          <FormField label="Upgrade until score" htmlFor={`${uid}-until`} hint="A file stays wanted while its score is below this.">
            <input id={`${uid}-until`} name="cutoff_format_score" type="number" step={1} value={draft.cutoffScore} onChange={(e) => draft.setCutoffScore(e.target.value)} className={`${inputClass} font-mono`} />
          </FormField>
          <FormField label="Min upgrade increment" htmlFor={`${uid}-inc`} hint="A new release must beat the current score by at least this (1 or more).">
            <input id={`${uid}-inc`} name="min_upgrade_format_score" type="number" min={1} step={1} value={draft.minUpgrade} onChange={(e) => draft.setMinUpgrade(e.target.value)} className={`${inputClass} font-mono`} />
          </FormField>
        </div>

      </form>
      <Group title="Test a release">
        <ReleaseTester profileId={existing ? existing.id : null} />
      </Group>
      </div>
    </EditorModalShell>
  );
};

/** Remounted per target so the draft always starts from the saved values. */
export const QualityProfileEditorModal: React.FC<QualityProfileEditorModalProps> = ({ target, ...rest }) => {
  if (target === null) return null;
  return <EditorBody key={target === 'new' ? 'new' : target.id} target={target} {...rest} />;
};
