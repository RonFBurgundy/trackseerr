import React from 'react';
import { Loader2, FlaskConical } from 'lucide-react';
import { ObsidianModal, TapeDeckButton, TactileSwitch, FormField, MonitorOptionSelect, MonitorModeSelect } from '@/components/ui';
import { StatusMessage } from '@/components/ui/FormField';
import type { UseImportListEditorReturn } from '@/hooks/useImportListEditor';
import type { ProviderMeta, ProviderField, ImportListItemOut } from '@/types/importLists';
import { SECRET_MASK, SYNC_INTERVAL_OPTIONS } from '@/types/importLists';
import type { LibraryManagerMode } from '@/types/models';
import type { QualityProfile } from '@/types/qualityProfiles';
import type { MonitorOption } from '@/types/monitoring';
import { inputClass } from './formClasses';

export interface ImportListEditorModalProps {
  editor: UseImportListEditorReturn;
  providers: ProviderMeta[];
  profiles: QualityProfile[];
  /** In Lidarr mode the artist monitor option does not apply: Lidarr's root-folder defaults decide. */
  libraryMode: LibraryManagerMode;
}

function describeItem(it: ImportListItemOut): string {
  return [it.artist_name, it.album_title, it.track_title].filter((p): p is string => !!p).join(' - ') || it.mbid || it.kind;
}

const ProviderFieldInput: React.FC<{
  field: ProviderField;
  value: string | number | undefined;
  onChange: (v: string | number) => void;
}> = ({ field, value, onChange }) => {
  const id = `il-field-${field.key}`;
  const v = value ?? '';
  return (
    <FormField label={`${field.label}${field.required ? ' *' : ''}`} htmlFor={id}>
      {field.type === 'select' ? (
        <select id={id} name={field.key} className={inputClass} value={String(v)} onChange={(e) => onChange(e.target.value)}>
          {(field.options ?? []).map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      ) : field.type === 'number' ? (
        <input
          id={id}
          name={field.key}
          type="number"
          inputMode="numeric"
          className={inputClass}
          value={v}
          onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))}
        />
      ) : (
        <input
          id={id}
          name={field.key}
          type={field.type === 'secret' ? 'password' : 'text'}
          autoComplete={field.type === 'secret' ? 'new-password' : 'off'}
          className={inputClass}
          value={v}
          onFocus={(e) => {
            // Clear the placeholder mask on focus so typing replaces it instead of appending.
            if (field.type === 'secret' && e.target.value === SECRET_MASK) onChange('');
          }}
          onBlur={(e) => {
            if (field.type === 'secret' && e.target.value === '' && value === '') onChange(SECRET_MASK);
          }}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {field.type === 'secret' && v === SECRET_MASK && (
        <div className="mt-1 text-[11px] font-mono text-neutral-500">Stored secret kept unless you type a new one.</div>
      )}
    </FormField>
  );
};

export const ImportListEditorModal: React.FC<ImportListEditorModalProps> = ({ editor, providers, profiles, libraryMode }) => {
  const { draft, provider, testResult } = editor;
  const intervalOptions = SYNC_INTERVAL_OPTIONS.some((o) => o.value === draft.sync_interval_minutes)
    ? SYNC_INTERVAL_OPTIONS
    : [...SYNC_INTERVAL_OPTIONS, { value: draft.sync_interval_minutes, label: `Every ${draft.sync_interval_minutes} min` }];

  return (
    <ObsidianModal
      isOpen={editor.isOpen}
      onClose={editor.close}
      title={editor.editingId ? 'Edit Import List' : 'Add Import List'}
      footer={
        <>
          <TapeDeckButton variant="amber" disabled={editor.saving} onClick={() => void editor.save()}>
            {editor.saving ? 'Saving...' : 'Save'}
          </TapeDeckButton>
          <TapeDeckButton
            disabled={editor.testing || !provider}
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

        <FormField label="Name *" htmlFor="il-name">
          <input id="il-name" name="name" className={inputClass} value={draft.name} onChange={(e) => editor.patch({ name: e.target.value })} />
        </FormField>

        <FormField label="Provider" htmlFor="il-provider">
          <select
            id="il-provider"
            name="provider"
            className={inputClass}
            value={draft.provider}
            disabled={!!editor.editingId}
            onChange={(e) => editor.setProvider(e.target.value)}
          >
            {providers.map((p) => (
              <option key={p.provider} value={p.provider}>
                {p.label}
              </option>
            ))}
          </select>
        </FormField>

        {provider?.fields.map((f) => (
          <ProviderFieldInput
            key={f.key}
            field={f}
            value={draft.config[f.key]}
            onChange={(v) => editor.setConfigValue(f.key, v)}
          />
        ))}

        <FormField label="Monitor mode" htmlFor="il-mode">
          <MonitorModeSelect id="il-mode" value={draft.monitor_mode} onChange={editor.setMonitorMode} />
        </FormField>

        {draft.monitor_mode === 'artist' && libraryMode !== 'lidarr' && (
          <FormField label="Artist monitor option" htmlFor="il-artist-opt">
            <MonitorOptionSelect
              id="il-artist-opt"
              value={draft.artist_monitor_option ?? 'existing'}
              onChange={(v: MonitorOption) => editor.patch({ artist_monitor_option: v })}
            />
          </FormField>
        )}

        {draft.monitor_mode === 'artist' && libraryMode === 'lidarr' && (
          <p className="text-[11px] font-mono text-neutral-400">
            New artists are added with the defaults of your Lidarr root folder.
          </p>
        )}

        <FormField label="Quality profile" htmlFor="il-quality">
          <select
            id="il-quality"
            name="quality-profile"
            className={inputClass}
            value={draft.quality_profile_id ?? ''}
            onChange={(e) => editor.patch({ quality_profile_id: e.target.value === '' ? null : e.target.value })}
          >
            <option value="">Default</option>
            {profiles.map((p) => (
              <option key={p.id} value={String(p.id)}>
                {p.name}
              </option>
            ))}
          </select>
        </FormField>

        <FormField label="Sync interval" htmlFor="il-interval">
          <select
            id="il-interval"
            name="sync-interval"
            className={inputClass}
            value={draft.sync_interval_minutes}
            onChange={(e) => editor.patch({ sync_interval_minutes: Number(e.target.value) })}
          >
            {intervalOptions.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </FormField>

        <TactileSwitch label="Enabled" checked={draft.enabled} onChange={(v) => editor.patch({ enabled: v })} />

        {testResult && (
          <div className="space-y-2" aria-live="polite">
            {testResult.ok ? (
              <StatusMessage variant="success">Connection OK - {testResult.item_count} items found</StatusMessage>
            ) : (
              <StatusMessage variant="error">{testResult.error ?? 'Test failed'}</StatusMessage>
            )}
            {testResult.ok && testResult.sample.length > 0 && (
              <ul className="border border-[#222222] rounded-[4px] divide-y divide-[#1f1f1f] bg-[#0d0d0d]">
                {testResult.sample.map((it) => (
                  <li key={it.id} className="px-3 py-2 text-xs font-mono text-neutral-300 flex items-center gap-2 min-w-0">
                    <span className="shrink-0 uppercase text-[10px] text-neutral-500">{it.kind}</span>
                    <span className="truncate">{describeItem(it)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </ObsidianModal>
  );
};
