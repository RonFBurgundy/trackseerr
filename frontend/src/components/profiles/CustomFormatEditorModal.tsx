import React, { useId, useState } from 'react';
import { Plus } from 'lucide-react';
import { FormField, TactileSwitch, TapeDeckButton } from '@/components/ui';
import type { CustomFormat, CustomFormatInput } from '@/types/customFormats';
import type { QualityDefinition } from '@/types/qualityDefinitions';
import { useCustomFormatDraft } from '@/hooks/useCustomFormatDraft';
import { inputClass } from '@/components/settings/formClasses';
import { CustomFormatSpecRow } from './CustomFormatSpecRow';
import { EditorModalShell } from './EditorModalShell';

export type CustomFormatTarget = CustomFormat | 'new';

export interface CustomFormatEditorModalProps {
  target: CustomFormatTarget | null;
  qualities: readonly QualityDefinition[];
  onClose: () => void;
  onSave: (id: number | null, input: CustomFormatInput) => Promise<string | null>;
}

const EditorBody: React.FC<Omit<CustomFormatEditorModalProps, 'target'> & { target: CustomFormatTarget }> = ({ target, qualities, onClose, onSave }) => {
  const uid = useId();
  const existing = target === 'new' ? null : target;
  const draft = useCustomFormatDraft(existing, qualities[0]?.quality ?? '');
  const [saving, setSaving] = useState<boolean>(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const formId = `${uid}-form`;

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
      maxWidth="sm:max-w-3xl"
      title={existing ? 'Edit Custom Format' : 'New Custom Format'}
      subtitle="Matches when every required specification passes and at least one other passes. Score it in a quality profile."
      formId={formId}
      saving={saving}
      disabled={draft.problem !== null}
      error={serverError ?? draft.problem}
    >
      <form id={formId} onSubmit={(e) => void submit(e)} className="space-y-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <FormField label="Format name" htmlFor={`${uid}-name`} className="sm:flex-1">
            <input
              id={`${uid}-name`}
              name="custom_format_name"
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
            id={`${uid}-rename`}
            name="include_in_rename"
            label="Include in rename"
            checked={draft.includeInRename}
            onChange={draft.setIncludeInRename}
          />
        </div>
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-2">
            <h4 className="text-xs font-bold uppercase tracking-wider text-white">Specifications ({draft.specs.length})</h4>
            <TapeDeckButton size="sm" onClick={draft.addSpec} icon={<Plus className="h-3.5 w-3.5" />}>
              Add specification
            </TapeDeckButton>
          </div>
          {draft.specs.length === 0 ? (
            <p className="text-xs font-mono text-neutral-500">No specifications yet: a format without any never matches.</p>
          ) : (
            draft.specs.map((s, i) => <CustomFormatSpecRow key={s.key} spec={s} index={i} qualities={qualities} draft={draft} />)
          )}
        </div>
      </form>
    </EditorModalShell>
  );
};

export const CustomFormatEditorModal: React.FC<CustomFormatEditorModalProps> = ({ target, ...rest }) => {
  if (target === null) return null;
  return <EditorBody key={target === 'new' ? 'new' : target.id} target={target} {...rest} />;
};
