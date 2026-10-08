import React from 'react';
import { Loader2, FlaskConical } from 'lucide-react';
import { ObsidianModal, TapeDeckButton, TactileSwitch, FormField } from '@/components/ui';
import { StatusMessage } from '@/components/ui/FormField';
import { inputClass } from '@/components/settings/formClasses';
import type { UseUserChannelEditorReturn } from '@/hooks/useUserChannelEditor';
import {
  USER_NOTIFICATION_CHANNEL_TYPES,
  USER_NOTIFICATION_EVENTS,
  type UserNotificationChannelType,
} from '@/types/userNotifications';
import {
  NOTIFICATION_CHANNEL_TYPE_LABELS,
  NOTIFICATION_FIELDS,
  hasStoredSecret,
  type NotificationFieldDef,
} from '@/types/notifications';

export interface UserChannelModalProps {
  editor: UseUserChannelEditorReturn;
}

const FieldInput: React.FC<{
  field: NotificationFieldDef;
  value: string | boolean | undefined;
  stored: boolean;
  onChange: (value: string | boolean) => void;
}> = ({ field, value, stored, onChange }) => {
  const id = `user-nc-field-${field.key}`;
  if (field.kind === 'boolean') {
    return (
      <TactileSwitch
        id={id}
        name={field.key}
        label={field.label}
        checked={value === true}
        onChange={onChange}
      />
    );
  }
  const text = typeof value === 'string' ? value : '';
  const isSecret = field.kind === 'secret';
  return (
    <FormField
      label={`${field.label}${field.required && !(isSecret && stored) ? ' *' : ''}`}
      htmlFor={id}
      hint={isSecret && stored ? 'Stored value kept unless you type a new one.' : undefined}
    >
      <input
        id={id}
        name={field.key}
        type={isSecret ? 'password' : field.kind === 'number' ? 'number' : 'text'}
        inputMode={field.kind === 'number' ? 'numeric' : undefined}
        autoComplete={isSecret ? 'new-password' : 'off'}
        className={inputClass}
        value={text}
        placeholder={isSecret && stored ? 'unchanged' : field.placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </FormField>
  );
};

export const UserChannelModal: React.FC<UserChannelModalProps> = ({ editor }) => {
  const { draft, editing } = editor;
  const channelType = draft.channelType as UserNotificationChannelType;
  const fields = NOTIFICATION_FIELDS[channelType] || [];

  return (
    <ObsidianModal
      isOpen={editor.isOpen}
      onClose={editor.close}
      title={editing ? 'Edit Notification Channel' : 'Add Notification Channel'}
      footer={
        <>
          <TapeDeckButton variant="amber" disabled={editor.saving} onClick={() => void editor.save()}>
            {editor.saving ? 'Saving...' : 'Save'}
          </TapeDeckButton>
          <TapeDeckButton
            disabled={editor.testing}
            onClick={() => void editor.runTest()}
            icon={editor.testing ? <Loader2 className="h-4 w-4 animate-spin" /> : <FlaskConical className="h-4 w-4" />}
          >
            Test
          </TapeDeckButton>
          <TapeDeckButton onClick={editor.close}>Cancel</TapeDeckButton>
        </>
      }
    >
      <div className="space-y-4">
        {editor.error && <StatusMessage variant="error">{editor.error}</StatusMessage>}

        <FormField label="Name *" htmlFor="user-nc-name">
          <input
            id="user-nc-name"
            name="name"
            className={inputClass}
            value={draft.name}
            maxLength={120}
            onChange={(e) => editor.setName(e.target.value)}
          />
        </FormField>

        <FormField label="Type" htmlFor="user-nc-type">
          <select
            id="user-nc-type"
            name="channel_type"
            className={inputClass}
            value={draft.channelType}
            disabled={!!editing}
            onChange={(e) => {
              const val = e.target.value as UserNotificationChannelType;
              if (USER_NOTIFICATION_CHANNEL_TYPES.includes(val)) {
                editor.setType(val);
              }
            }}
          >
            {USER_NOTIFICATION_CHANNEL_TYPES.map((t) => (
              <option key={t} value={t}>
                {NOTIFICATION_CHANNEL_TYPE_LABELS[t]}
              </option>
            ))}
          </select>
        </FormField>

        {fields.map((f) => (
          <FieldInput
            key={`${draft.channelType}-${f.key}`}
            field={f}
            value={draft.values[f.key]}
            stored={hasStoredSecret(editing, f.key)}
            onChange={(v) => editor.setValue(f.key, v)}
          />
        ))}

        <fieldset className="space-y-2">
          <legend className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1">
            Events
          </legend>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1">
            {USER_NOTIFICATION_EVENTS.map((ev) => {
              const id = `user-nc-event-${ev.id}`;
              return (
                <label
                  key={ev.id}
                  htmlFor={id}
                  className="flex items-center gap-2 min-h-[36px] text-xs font-mono text-neutral-200 cursor-pointer"
                >
                  <input
                    id={id}
                    name="events"
                    type="checkbox"
                    value={ev.id}
                    checked={draft.events.includes(ev.id)}
                    onChange={() => editor.toggleEvent(ev.id)}
                    className="h-4 w-4 accent-[#e5a00d]"
                  />
                  {ev.label}
                </label>
              );
            })}
          </div>
        </fieldset>
      </div>
    </ObsidianModal>
  );
};
