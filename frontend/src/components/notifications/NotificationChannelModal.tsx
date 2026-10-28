import React from 'react';
import { Loader2, FlaskConical } from 'lucide-react';
import { ObsidianModal, TapeDeckButton, TactileSwitch, FormField } from '@/components/ui';
import { StatusMessage } from '@/components/ui/FormField';
import { inputClass } from '@/components/settings/formClasses';
import type { UseNotificationChannelEditorReturn } from '@/hooks/useNotificationChannelEditor';
import {
  NOTIFICATION_CHANNEL_TYPES,
  NOTIFICATION_CHANNEL_TYPE_LABELS,
  NOTIFICATION_EVENTS,
  NOTIFICATION_EVENT_GROUPS,
  NOTIFICATION_FIELDS,
  hasStoredSecret,
  isNotificationChannelType,
  type NotificationFieldDef,
} from '@/types/notifications';

export interface NotificationChannelModalProps {
  editor: UseNotificationChannelEditorReturn;
}

const FieldInput: React.FC<{
  field: NotificationFieldDef;
  value: string | boolean | undefined;
  stored: boolean;
  onChange: (value: string | boolean) => void;
}> = ({ field, value, stored, onChange }) => {
  const id = `nc-field-${field.key}`;
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

export const NotificationChannelModal: React.FC<NotificationChannelModalProps> = ({ editor }) => {
  const { draft, editing } = editor;
  const fields = NOTIFICATION_FIELDS[draft.channelType];

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

        <FormField label="Name *" htmlFor="nc-name">
          <input
            id="nc-name"
            name="name"
            className={inputClass}
            value={draft.name}
            maxLength={120}
            onChange={(e) => editor.setName(e.target.value)}
          />
        </FormField>

        <FormField label="Type" htmlFor="nc-type">
          <select
            id="nc-type"
            name="channel_type"
            className={inputClass}
            value={draft.channelType}
            disabled={!!editing}
            onChange={(e) => {
              if (isNotificationChannelType(e.target.value)) editor.setType(e.target.value);
            }}
          >
            {NOTIFICATION_CHANNEL_TYPES.map((t) => (
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

        {NOTIFICATION_EVENT_GROUPS.map((group) => {
          const allSelected = group.events.every((e) => draft.events.includes(e));
          const noneSelected = group.events.every((e) => !draft.events.includes(e));

          const selectAll = () => {
            const next = Array.from(new Set([...draft.events, ...group.events]));
            editor.setEvents(next);
          };

          const selectNone = () => {
            const next = draft.events.filter((e) => !group.events.includes(e));
            editor.setEvents(next);
          };

          return (
            <fieldset key={group.title} className="space-y-2">
              <legend className="w-full">
                <div className="flex items-center justify-between mb-1">
                  <span className="block text-xs uppercase font-mono tracking-wider text-neutral-300">
                    {group.title}
                  </span>
                  <div className="flex items-center gap-2 text-[11px] font-mono">
                    <button
                      type="button"
                      onClick={selectAll}
                      disabled={allSelected}
                      className="text-neutral-400 hover:text-white disabled:opacity-40 disabled:hover:text-neutral-400 uppercase tracking-wider"
                    >
                      All
                    </button>
                    <span className="text-neutral-600">/</span>
                    <button
                      type="button"
                      onClick={selectNone}
                      disabled={noneSelected}
                      className="text-neutral-400 hover:text-white disabled:opacity-40 disabled:hover:text-neutral-400 uppercase tracking-wider"
                    >
                      None
                    </button>
                  </div>
                </div>
              </legend>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1">
                {group.events.map((eventId) => {
                  const ev = NOTIFICATION_EVENTS.find((e) => e.id === eventId);
                  const label = ev ? ev.label : eventId;
                  const id = `nc-event-${eventId}`;
                  return (
                    <div key={eventId} className="flex items-center min-h-[36px]">
                      <TactileSwitch
                        id={id}
                        name="events"
                        label={label}
                        checked={draft.events.includes(eventId)}
                        onChange={() => editor.toggleEvent(eventId)}
                      />
                    </div>
                  );
                })}
              </div>
            </fieldset>
          );
        })}

        <TactileSwitch
          id="nc-enabled"
          name="enabled"
          label="Enabled"
          checked={draft.enabled}
          onChange={editor.setEnabled}
        />
      </div>
    </ObsidianModal>
  );
};
