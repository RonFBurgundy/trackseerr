import React, { useId, useState } from 'react';
import { Loader2, Save } from 'lucide-react';
import { ObsidianModal, TapeDeckButton, FormField } from '@/components/ui';
import {
  RELEASE_PRIMARY_LABELS,
  RELEASE_PRIMARY_TYPES,
  RELEASE_SECONDARY_LABELS,
  RELEASE_SECONDARY_TYPES,
  type ReleaseOrNew,
  type ReleasePrimaryType,
  type MetadataProfileInput,
  type ReleaseSecondaryType,
} from '@/types/metadataProfiles';
import { inputClass } from '@/components/settings/formClasses';

export interface MetadataProfileEditorModalProps {
  /** The profile being edited, or `'new'` for a blank one. Closed when null. */
  target: ReleaseOrNew | null;
  onClose: () => void;
  onSave: (id: number | null, input: MetadataProfileInput) => Promise<boolean>;
}

interface CheckGroupProps<T extends string> {
  legend: string;
  hint: string;
  prefix: string;
  options: readonly T[];
  labels: Readonly<Record<T, string>>;
  selected: readonly T[];
  onToggle: (value: T) => void;
}

function CheckGroup<T extends string>({
  legend,
  hint,
  prefix,
  options,
  labels,
  selected,
  onToggle,
}: CheckGroupProps<T>): React.ReactElement {
  return (
    <fieldset className="space-y-1.5">
      <legend className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-0.5">{legend}</legend>
      <p className="text-[11px] font-mono text-neutral-500">{hint}</p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4">
        {options.map((o) => {
          const id = `${prefix}-${o.replace(/[^a-z0-9]+/g, '-')}`;
          return (
            <label key={o} htmlFor={id} className="flex items-center gap-2 min-h-[36px] sm:min-h-[34px] text-xs font-mono text-neutral-200 cursor-pointer">
              <input
                id={id}
                name={`${prefix}[]`}
                type="checkbox"
                checked={selected.includes(o)}
                onChange={() => onToggle(o)}
                className="h-5 w-5 accent-[#e5a00d]"
              />
              {labels[o]}
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

function toggled<T extends string>(list: readonly T[], value: T): T[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

/** Name plus two labelled checkbox groups. Remounted per target so the form always starts from the saved values. */
const EditorBody: React.FC<{ target: ReleaseOrNew; onClose: () => void; onSave: MetadataProfileEditorModalProps['onSave'] }> = ({
  target,
  onClose,
  onSave,
}) => {
  const uid = useId();
  const existing = target === 'new' ? null : target;
  const [name, setName] = useState<string>(existing?.name ?? '');
  const [primary, setPrimary] = useState<ReleasePrimaryType[]>(existing?.primary_types ?? ['album']);
  const [secondary, setSecondary] = useState<ReleaseSecondaryType[]>(existing?.secondary_types ?? ['studio']);
  const [saving, setSaving] = useState<boolean>(false);

  const valid = name.trim() !== '' && primary.length > 0 && secondary.length > 0;
  const formId = `${uid}-form`;

  const submit = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault();
    if (!valid) return;
    setSaving(true);
    const ok = await onSave(existing?.id ?? null, { name: name.trim(), primary_types: primary, secondary_types: secondary });
    setSaving(false);
    if (ok) onClose();
  };

  return (
    <ObsidianModal
      isOpen
      onClose={onClose}
      title={existing ? 'Edit Metadata Profile' : 'New Metadata Profile'}
      subtitle="Shapes automatic monitoring only. Releases outside the profile stay in the catalog and can still be monitored by hand."
      footer={
        <>
          <TapeDeckButton type="button" onClick={onClose} disabled={saving}>
            Cancel
          </TapeDeckButton>
          <TapeDeckButton
            type="submit"
            form={formId}
            variant="amber"
            disabled={!valid || saving}
            icon={saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          >
            Save
          </TapeDeckButton>
        </>
      }
    >
      <form id={formId} onSubmit={(e) => void submit(e)} className="space-y-5">
        <FormField label="Profile name" htmlFor={`${uid}-name`}>
          <input
            id={`${uid}-name`}
            name="metadata_profile_name"
            type="text"
            required
            maxLength={100}
            autoComplete="off"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={inputClass}
          />
        </FormField>
        <CheckGroup
          legend="Primary types"
          hint="Which kinds of release are eligible."
          prefix={`${uid}-primary`}
          options={RELEASE_PRIMARY_TYPES}
          labels={RELEASE_PRIMARY_LABELS}
          selected={primary}
          onToggle={(v) => setPrimary((prev) => toggled(prev, v))}
        />
        <CheckGroup
          legend="Secondary types"
          hint="A release must have only allowed secondary types. Studio means it has none."
          prefix={`${uid}-secondary`}
          options={RELEASE_SECONDARY_TYPES}
          labels={RELEASE_SECONDARY_LABELS}
          selected={secondary}
          onToggle={(v) => setSecondary((prev) => toggled(prev, v))}
        />
      </form>
    </ObsidianModal>
  );
};

export const MetadataProfileEditorModal: React.FC<MetadataProfileEditorModalProps> = ({ target, onClose, onSave }) => {
  if (target === null) return null;
  return <EditorBody key={target === 'new' ? 'new' : target.id} target={target} onClose={onClose} onSave={onSave} />;
};
