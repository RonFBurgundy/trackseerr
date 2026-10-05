import React, { useId, useState } from 'react';
import { Loader2, Save } from 'lucide-react';
import { TapeDeckButton, MachinedCard, TactileSwitch, ActionBar, FormField, MonitorOptionSelect, ScrollFill } from '@/components/ui';
import type { MediaManagementSettings } from '@/types/models';
import { updateMediaManagementSettings } from '@/services/settingsService';
import { NamingFormatsEditor } from '@/components/naming/NamingFormatsEditor';
import { inputClass, labelClass } from './formClasses';

export interface MediaFoldersPanelProps {
  settings: MediaManagementSettings | null;
  onChange: React.Dispatch<React.SetStateAction<MediaManagementSettings | null>>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Root Folders & Naming (formerly the "Media" tab). */
export const MediaFoldersPanel: React.FC<MediaFoldersPanelProps> = ({ settings, onChange, onToast }) => {
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const rootId = useId();
  const stagingId = useId();
  const importModeId = useId();
  const scanMonitorId = useId();
  const addMonitorId = useId();

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!settings) return;
    setIsSaving(true);
    try {
      // library_mode is owned by /api/settings/library-manager; the media PUT ignores (and logs) it.
      const { library_mode: _libraryMode, ...payload } = settings;
      const updated = await updateMediaManagementSettings(payload);
      onChange(updated);
      onToast('Media management settings saved');
    } catch {
      onToast('Failed to save media management settings', 'error');
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <MachinedCard className="p-6 max-w-2xl space-y-5">
      <div className="border-b border-[#222222] pb-3">
        <h4 className="text-sm font-bold uppercase font-mono text-white">Root Folders &amp; Naming</h4>
        <p className="text-xs text-neutral-400 font-mono mt-0.5">
          Configure library paths, naming templates, and audio tagging
        </p>
      </div>

      <ScrollFill ariaLabel="Root folders and naming settings" className="-mr-2 pr-2">
      <form onSubmit={handleSave} className="space-y-4">
        <div>
          <label htmlFor={rootId} className={labelClass}>Root Music Folder</label>
          <input
            id={rootId}
            name="root_folder_path"
            type="text"
            value={settings?.root_folder_path || ''}
            onChange={(e) => onChange((prev) => (prev ? { ...prev, root_folder_path: e.target.value } : null))}
            placeholder="/data/media/music"
            className={`${inputClass} font-mono`}
          />
        </div>

        <div>
          <label htmlFor={stagingId} className={labelClass}>Staging / Downloads Folder</label>
          <input
            id={stagingId}
            name="staging_folder_path"
            type="text"
            value={settings?.staging_folder_path || ''}
            onChange={(e) => onChange((prev) => (prev ? { ...prev, staging_folder_path: e.target.value } : null))}
            placeholder="/data/downloads"
            className={`${inputClass} font-mono`}
          />
        </div>

        <NamingFormatsEditor
          value={{
            artist_folder_format: settings?.artist_folder_format || '',
            standard_track_format: settings?.standard_track_format || '',
            multi_disc_track_format: settings?.multi_disc_track_format || '',
            compilation_track_format: settings?.compilation_track_format || '',
          }}
          context={{
            root_folder_path: settings?.root_folder_path,
            colon_replacement_format: settings?.colon_replacement_format,
            clean_artist_names: settings?.clean_artist_names,
          }}
          onChange={(patch) => onChange((prev) => (prev ? { ...prev, ...patch } : null))}
        />

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 pt-2 border-t border-[#1f1f1f]">
          <div>
            <label htmlFor={importModeId} className={labelClass}>Import Mode</label>
            <select
              id={importModeId}
              name="import_mode"
              value={settings?.import_mode || 'move'}
              onChange={(e) =>
                onChange((prev) =>
                  prev ? { ...prev, import_mode: e.target.value as 'move' | 'hardlink' | 'copy' } : null
                )
              }
              className={inputClass}
            >
              <option value="move">Move</option>
              <option value="hardlink">Hardlink</option>
              <option value="copy">Copy</option>
            </select>
          </div>

          <div className="flex flex-col justify-end">
            <div className="flex items-center justify-between pb-2">
              <span className="text-xs font-mono text-neutral-300">Normalize Audio Tags</span>
              <TactileSwitch
                checked={settings?.write_audio_tags ?? true}
                onChange={(val) => onChange((prev) => (prev ? { ...prev, write_audio_tags: val } : null))}
                label="Write Tags"
              />
            </div>
          </div>

          <div className="flex flex-col justify-end">
            <div className="flex items-center justify-between pb-2">
              <span className="text-xs font-mono text-neutral-300">Embed Artwork</span>
              <TactileSwitch
                checked={settings?.embed_artwork ?? true}
                onChange={(val) => onChange((prev) => (prev ? { ...prev, embed_artwork: val } : null))}
                label="Embed Artwork"
              />
            </div>
          </div>
        </div>

        <div className="pt-2 border-t border-[#1f1f1f] space-y-3">
          <div>
            <h5 className="text-xs font-bold uppercase font-mono text-white">Monitoring</h5>
            <p className="text-[11px] text-neutral-400 font-mono mt-0.5">
              &ldquo;Existing albums only&rdquo; monitors only the albums already on disk; future releases stay unmonitored.
            </p>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <FormField label="Artists found by library scan" htmlFor={scanMonitorId}>
              <MonitorOptionSelect
                id={scanMonitorId}
                value={settings?.scan_monitor_option ?? 'existing'}
                onChange={(v) => onChange((prev) => (prev ? { ...prev, scan_monitor_option: v } : null))}
              />
            </FormField>
            <FormField label="Artists added manually" htmlFor={addMonitorId}>
              <MonitorOptionSelect
                id={addMonitorId}
                value={settings?.add_monitor_option ?? 'all'}
                onChange={(v) => onChange((prev) => (prev ? { ...prev, add_monitor_option: v } : null))}
              />
            </FormField>
          </div>
        </div>

        <ActionBar align="end" className="pt-3">
          <TapeDeckButton
            type="submit"
            variant="amber"
            size="md"
            disabled={isSaving}
            icon={isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          >
            Save Media Settings
          </TapeDeckButton>
        </ActionBar>
      </form>
      </ScrollFill>
    </MachinedCard>
  );
};
