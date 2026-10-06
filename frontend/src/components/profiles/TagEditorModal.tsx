import React, { useId, useState } from 'react';
import { FormField } from '@/components/ui';
import type { TagEditorTarget } from '@/hooks/useTagManager';
import { TAG_LABEL_MAX, normalizeTagLabel, tagLabelProblem } from '@/types/tags';
import { inputClass } from '@/components/settings/formClasses';
import { EditorModalShell } from './EditorModalShell';

export interface TagEditorModalProps {
  target: TagEditorTarget | null;
  onClose: () => void;
  /** Resolves an inline error message (the server's 409/422 detail), or null on success. */
  onSave: (label: string) => Promise<string | null>;
}

const EditorBody: React.FC<{ target: TagEditorTarget; onClose: () => void; onSave: TagEditorModalProps['onSave'] }> = ({
  target,
  onClose,
  onSave,
}) => {
  const uid = useId();
  const [label, setLabel] = useState<string>(target === 'new' ? '' : target.label);
  const [saving, setSaving] = useState<boolean>(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const formId = `${uid}-form`;
  const problem = label === '' ? 'Enter a tag label.' : tagLabelProblem(label);

  const submit = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault();
    if (problem) return;
    setSaving(true);
    setServerError(null);
    const err = await onSave(normalizeTagLabel(label));
    setSaving(false);
    if (err) setServerError(err);
    else onClose();
  };

  return (
    <EditorModalShell
      title={target === 'new' ? 'New Tag' : 'Rename Tag'}
      maxWidth="sm:max-w-md"
      onClose={onClose}
      formId={formId}
      saving={saving}
      disabled={problem !== null}
      error={serverError ?? (label === '' ? null : problem)}
    >
      <form id={formId} onSubmit={(e) => void submit(e)}>
        <FormField label="Label" htmlFor={`${uid}-label`} hint="Lower case letters, numbers, spaces, &, hyphens and underscores.">
          <input
            id={`${uid}-label`}
            name="tag_label"
            type="text"
            autoComplete="off"
            autoCapitalize="none"
            maxLength={TAG_LABEL_MAX + 10}
            value={label}
            onChange={(e) => {
              setLabel(e.target.value);
              setServerError(null);
            }}
            className={`${inputClass} font-mono`}
          />
        </FormField>
      </form>
    </EditorModalShell>
  );
};

export const TagEditorModal: React.FC<TagEditorModalProps> = ({ target, onClose, onSave }) => {
  if (target === null) return null;
  return <EditorBody key={target === 'new' ? 'new' : target.id} target={target} onClose={onClose} onSave={onSave} />;
};
