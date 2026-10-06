import React, { useId, useState } from 'react';
import { Eye, EyeOff, Loader2, RefreshCw, Save, Trash2 } from 'lucide-react';
import { ConfirmDialog, TapeDeckButton, MachinedCard, TactileSwitch, ActionBar, FormField, MonitorOptionSelect } from '@/components/ui';
import type { MediaManagementSettings, SeedCompleteAction } from '@/types/models';
import { useDownloadClientRoots } from '@/hooks/useDownloadClientRoots';
import { updateMediaManagementSettings } from '@/services/settingsService';
import { NamingFormatsEditor } from '@/components/naming/NamingFormatsEditor';
import { ApiError } from '@/services/apiClient';
import { RecycleBinSection } from './RecycleBinSection';
import { inputClass, labelClass } from './formClasses';

export interface MediaFoldersPanelProps {
  settings: MediaManagementSettings | null;
  onChange: React.Dispatch<React.SetStateAction<MediaManagementSettings | null>>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** A server-masked secret starts with a run of mask characters (e.g. "\u2022\u2022\u2022\u20221234"). */
const isMaskedKey = (value: string | null | undefined): boolean => !!value && /^[*\u2022\u25CF]+/.test(value);

/** Root Folders & Naming (formerly the "Media" tab). */
export const MediaFoldersPanel: React.FC<MediaFoldersPanelProps> = ({ settings, onChange, onToast }) => {
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const rootId = useId();
  const stagingId = useId();
  const importModeId = useId();
  const hardlinkTagsId = useId();
  const seedActionId = useId();
  const scanMonitorId = useId();
  const addMonitorId = useId();
  const acoustidKeyId = useId();
  const roots = useDownloadClientRoots();
  const [saveWarnings, setSaveWarnings] = useState<string[]>([]);
  const [showKey, setShowKey] = useState<boolean>(false);
  const [confirmRemoveKey, setConfirmRemoveKey] = useState<boolean>(false);
  const [removingKey, setRemovingKey] = useState<boolean>(false);
  // null = untouched: the server value (possibly masked) is shown and never sent back.
  const [keyDraft, setKeyDraft] = useState<string | null>(null);
  const serverKey = settings?.acoustid_api_key ?? '';
  const keyMasked = keyDraft === null && isMaskedKey(serverKey);
  const keyValue = keyDraft ?? serverKey;
  const keyConfigured = keyValue.trim().length > 0;

  const handleRemoveKey = async (): Promise<void> => {
    setRemovingKey(true);
    try {
      // An explicit empty string clears the stored key server-side.
      const updated = await updateMediaManagementSettings({ acoustid_api_key: '' });
      onChange((prev) => (prev ? { ...prev, acoustid_api_key: updated.acoustid_api_key ?? '', fingerprint_on_weak_match: updated.fingerprint_on_weak_match } : updated));
      setKeyDraft(null);
      setConfirmRemoveKey(false);
      onToast('AcoustID key removed');
    } catch {
      onToast('Failed to remove AcoustID key', 'error');
    } finally {
      setRemovingKey(false);
    }
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!settings) return;
    setIsSaving(true);
    try {
      // library_mode is owned by /api/settings/library-manager; the media PUT ignores (and logs) it.
      const {
        library_mode: _libraryMode,
        acoustid_api_key: _serverKey,
        effective_recycle_bin_path: _effRecycle,
        effective_quarantine_folder_path: _effQuarantine,
        warnings: _warnings,
        ...rest
      } = settings;
      // Only an edited key is sent ('' clears it); the untouched/masked server value is omitted.
      const payload: Partial<MediaManagementSettings> =
        keyDraft === null ? rest : { ...rest, acoustid_api_key: keyDraft };
      const updated = await updateMediaManagementSettings(payload);
      onChange(updated);
      setKeyDraft(null);
      setSaveWarnings(updated.warnings ?? []);
      onToast('Media management settings saved');
    } catch (err) {
      onToast(err instanceof ApiError ? err.message : 'Failed to save media management settings', 'error');
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <MachinedCard className="p-3 sm:p-6 max-w-2xl space-y-5">
      <div className="border-b border-[#222222] pb-3">
        <h4 className="text-sm font-bold uppercase font-mono text-white">Root Folders &amp; Naming</h4>
        <p className="text-xs text-neutral-400 font-mono mt-0.5">
          Configure library paths, naming templates, and audio tagging
        </p>
      </div>

      <div>
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

        <div className="space-y-2">
          <div className="flex items-center justify-between gap-2">
            <span className={labelClass}>Download folders (read from your clients)</span>
            <TapeDeckButton
              type="button"
              size="sm"
              aria-label="Re-read download folders from clients"
              title="Re-read from clients"
              onClick={() => void roots.refresh()}
              disabled={roots.loading}
              icon={<RefreshCw className={`h-4 w-4 ${roots.loading ? 'animate-spin' : ''}`} />}
            />
          </div>
          <p className="text-[11px] text-neutral-500 font-mono">
            Finished downloads are imported from the folders each download client reports, so there is nothing to type here.
          </p>
          {roots.error && (
            <p role="alert" className="text-[11px] font-mono text-[#ef4444]">{roots.error}</p>
          )}
          {!roots.error && !roots.loading && roots.clients.length === 0 && (
            <p className="text-[11px] font-mono text-neutral-500">No download clients configured.</p>
          )}
          <ul className="space-y-1.5">
            {roots.clients.map((c) => (
              <li key={c.client_id} className="px-2 py-1.5 rounded-[3px] border border-[#222222] bg-[#0d0d0d]">
                <div className="text-[11px] font-mono font-bold text-neutral-300">{c.name}</div>
                {c.roots.map((r) => (
                  <div key={r} className="text-[11px] font-mono text-white break-all">{r}</div>
                ))}
                {c.error && (
                  <div className="text-[11px] font-mono text-[#e5a00d] break-words">
                    Could not read download folder: {c.error}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>

        <div>
          <label htmlFor={stagingId} className={labelClass}>Extra import folder (advanced)</label>
          <input
            id={stagingId}
            name="staging_folder_path"
            type="text"
            value={settings?.staging_folder_path || ''}
            onChange={(e) => onChange((prev) => (prev ? { ...prev, staging_folder_path: e.target.value } : null))}
            placeholder="Optional"
            className={`${inputClass} font-mono`}
          />
          <p className="mt-1.5 text-[11px] font-mono text-neutral-500">
            Optional. Download folders are read from each download client automatically; set this only to also allow
            imports from another folder. It can never be inside your library.
          </p>
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
            <label htmlFor={importModeId} className={labelClass}>Import mode (torrents)</label>
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
            <p className="mt-1.5 text-[11px] font-mono text-neutral-500">
              Usenet and Soulseek downloads are always moved &mdash; nothing seeds from them.
            </p>
            {settings?.seed_rule_conflict && (settings?.import_mode ?? 'move') === 'move' && (
              <p
                role="alert"
                className="mt-1.5 px-2 py-1.5 rounded-[3px] border border-[#e5a00d]/50 bg-[#e5a00d]/10 text-[11px] font-mono text-[#e5a00d]"
              >
                Move breaks seeding for indexers with seed rules &mdash; use Hardlink or Copy.
              </p>
            )}
          </div>

          {settings?.import_mode === 'hardlink' && (
            <div>
              <label htmlFor={hardlinkTagsId} className={labelClass}>Tagging hardlinked torrent files</label>
              <select
                id={hardlinkTagsId}
                name="torrent_hardlink_tags"
                value={settings.torrent_hardlink_tags ?? 'copy_and_tag'}
                onChange={(e) =>
                  onChange((prev) =>
                    prev
                      ? { ...prev, torrent_hardlink_tags: e.target.value === 'keep_hardlink' ? 'keep_hardlink' : 'copy_and_tag' }
                      : null
                  )
                }
                className={inputClass}
              >
                <option value="copy_and_tag">Write tags (temporary copy while seeding)</option>
                <option value="keep_hardlink">Keep hardlink (don&apos;t write tags)</option>
              </select>
              <p className="mt-1.5 text-[11px] font-mono text-neutral-500">
                Writing tags into a hardlink would change the torrent&apos;s data and break seeding, so TrackSeerr writes tags
                to a separate copy. The copy&apos;s extra space is freed once seeding cleanup removes the torrent.
              </p>
            </div>
          )}

          <div>
            <label htmlFor={seedActionId} className={labelClass}>When seeding is done</label>
            <select
              id={seedActionId}
              name="seed_complete_action"
              value={settings?.seed_complete_action ?? 'keep'}
              onChange={(e) => {
                const v = e.target.value;
                const action: SeedCompleteAction = v === 'remove' || v === 'remove_and_delete' ? v : 'keep';
                onChange((prev) => (prev ? { ...prev, seed_complete_action: action } : null));
              }}
              className={inputClass}
            >
              <option value="keep">Keep seeding</option>
              <option value="remove">Remove torrent (keep files)</option>
              <option value="remove_and_delete">Remove torrent and its files</option>
            </select>
            <p className="mt-1.5 text-[11px] font-mono text-neutral-500">
              Files are only deleted once the seed goal is met and your library has its own copy. Never in Move mode, and never
              while files wait for manual import.
            </p>
            {settings?.seed_complete_action === 'remove_and_delete' && (settings.import_mode ?? 'move') === 'move' && (
              <p
                role="alert"
                className="mt-1.5 px-2 py-1.5 rounded-[3px] border border-[#e5a00d]/50 bg-[#e5a00d]/10 text-[11px] font-mono text-[#e5a00d]"
              >
                In Move mode the library holds the only copy, so files are never deleted &mdash; the torrent is removed only.
              </p>
            )}
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

        <RecycleBinSection settings={settings} onChange={onChange} onToast={onToast} warnings={saveWarnings} />

        <div className="pt-2 border-t border-[#1f1f1f] space-y-3">
          <div>
            <h5 className="text-xs font-bold uppercase font-mono text-white">Monitoring</h5>
            <p className="text-[11px] text-neutral-400 font-mono mt-0.5">
              &ldquo;Existing tracks&rdquo; monitors exactly the tracks you have files for; every other track and album stays unmonitored until you choose it.
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
                value={settings?.add_monitor_option ?? 'existing'}
                onChange={(v) => onChange((prev) => (prev ? { ...prev, add_monitor_option: v } : null))}
              />
            </FormField>
          </div>
        </div>

        <div className="pt-2 border-t border-[#1f1f1f] space-y-3">
          <div>
            <h5 className="text-xs font-bold uppercase font-mono text-white">Audio fingerprinting</h5>
            <p className="text-[11px] text-neutral-400 font-mono mt-0.5">
              Identify downloads by their audio via AcoustID when tags are not enough.
            </p>
          </div>
          <div>
            <label htmlFor={acoustidKeyId} className={labelClass}>AcoustID API key</label>
            <div className="flex items-stretch gap-2">
              <input
                id={acoustidKeyId}
                name="acoustid_api_key"
                type={showKey ? 'text' : 'password'}
                autoComplete="off"
                spellCheck={false}
                value={keyValue}
                onChange={(e) => setKeyDraft(e.target.value)}
                onFocus={() => {
                  // Replacing a masked key discards the whole masked value, including its visible tail.
                  if (keyMasked) setKeyDraft('');
                }}
                onBlur={() => {
                  // Left empty after focusing a masked key: keep the configured key unchanged.
                  if (keyDraft === '' && isMaskedKey(serverKey)) setKeyDraft(null);
                }}
                placeholder="Application API key"
                className={`${inputClass} font-mono`}
              />
              <TapeDeckButton
                type="button"
                size="sm"
                aria-label={showKey ? 'Hide AcoustID API key' : 'Show AcoustID API key'}
                title={showKey ? 'Hide key' : 'Show key'}
                onClick={() => setShowKey((v) => !v)}
                icon={showKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              />
              {serverKey.trim().length > 0 && (
                <TapeDeckButton
                  type="button"
                  size="sm"
                  aria-label="Remove AcoustID API key"
                  title="Remove key"
                  onClick={() => setConfirmRemoveKey(true)}
                  icon={<Trash2 className="h-4 w-4" />}
                >
                  Remove key
                </TapeDeckButton>
              )}
            </div>
            <p className="text-[11px] text-neutral-500 font-mono mt-1">
              {keyMasked ? 'A key is configured; click the field to replace it. ' : ''}
              Get a free application key at acoustid.org/new-application
            </p>
          </div>
          <div>
            <div className="flex items-center justify-between gap-3">
              <span className="text-xs font-mono text-neutral-300">Identify by fingerprint when tags are ambiguous</span>
              <TactileSwitch
                checked={keyConfigured && (settings?.fingerprint_on_weak_match ?? false)}
                disabled={!keyConfigured}
                onChange={(val) => onChange((prev) => (prev ? { ...prev, fingerprint_on_weak_match: val } : null))}
                label="Identify by fingerprint when tags are ambiguous"
                name="fingerprint_on_weak_match"
              />
            </div>
            <p className="text-[11px] text-neutral-500 font-mono mt-1">
              When a downloaded file&apos;s tags don&apos;t confidently match a track, look it up by audio fingerprint
              (AcoustID, ~3 lookups/sec). Off by default.
              {!keyConfigured ? ' Add an AcoustID API key to enable this.' : ''}
            </p>
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
      </div>
      <ConfirmDialog
        isOpen={confirmRemoveKey}
        title="Remove AcoustID key?"
        confirmLabel="Remove key"
        onConfirm={() => void handleRemoveKey()}
        onCancel={() => setConfirmRemoveKey(false)}
        busy={removingKey}
      >
        <p>Fingerprint identification stops until a new key is added.</p>
      </ConfirmDialog>
    </MachinedCard>
  );
};
