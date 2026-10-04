import React, { useState, useEffect, useRef } from 'react';
import { Loader2, Save, AlertTriangle, RotateCw } from 'lucide-react';
import { TapeDeckButton, MachinedCard, TactileSwitch } from '@/components/ui';
import type {
  LidarrSettings,
  LidarrMonitorOption,
  LibraryManagerMode,
  LidarrNamedOption,
} from '@/types/models';
import { updateLidarrSettings, testLidarrConnection } from '@/services/settingsService';
import { useLidarrOptions } from '@/hooks/useLidarrOptions';
import { InactiveBanner } from './InactiveGate';
import { inputClass, labelClass, compactInputClass, compactLabelClass } from './formClasses';

const DEFAULT_LIDARR: LidarrSettings = {
  url: '',
  search_on_add: true,
  auto_trickle: false,
  trickle_rate_seconds: 3.0,
  trickle_batch_size: 25,
};

const MONITOR_OPTIONS: Array<{ value: LidarrMonitorOption; label: string }> = [
  { value: 'all', label: 'All albums' },
  { value: 'future', label: 'Future albums' },
  { value: 'missing', label: 'Missing albums' },
  { value: 'existing', label: 'Existing albums' },
  { value: 'first', label: 'First album' },
  { value: 'latest', label: 'Latest album' },
  { value: 'none', label: 'None' },
];

export interface LidarrPanelProps {
  settings: LidarrSettings | null;
  onChange: React.Dispatch<React.SetStateAction<LidarrSettings | null>>;
  /** True when Lidarr is the active library manager. */
  isActive: boolean;
  activeManager: LibraryManagerMode;
  onRequestSwitch: (mode: LibraryManagerMode) => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

interface ProfileSelectProps {
  label: string;
  value: number | undefined;
  options: LidarrNamedOption[] | null;
  onChange: (id: number | undefined) => void;
}

const ProfileSelect: React.FC<ProfileSelectProps> = ({ label, value, options, onChange }) => {
  const known = options?.some((o) => o.id === value) ?? false;
  return (
    <div>
      <label className={compactLabelClass}>{label}</label>
      <select
        value={value ?? ''}
        disabled={!options}
        onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))}
        className={compactInputClass}
      >
        <option value="">Default</option>
        {value !== undefined && !known && <option value={value}>Profile #{value}</option>}
        {options?.map((o) => (
          <option key={o.id} value={o.id}>
            {o.name}
          </option>
        ))}
      </select>
    </div>
  );
};

export const LidarrPanel: React.FC<LidarrPanelProps> = ({
  settings,
  onChange,
  isActive,
  activeManager,
  onRequestSwitch,
  onToast,
}) => {
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [isTesting, setIsTesting] = useState<boolean>(false);
  // Options come from the *saved* Lidarr connection, so only enable once a saved URL exists.
  const [hasSavedUrl, setHasSavedUrl] = useState<boolean>(false);
  const seeded = useRef<boolean>(false);
  useEffect(() => {
    if (settings && !seeded.current) {
      seeded.current = true;
      setHasSavedUrl(Boolean(settings.url));
    }
  }, [settings]);
  const optionsHook = useLidarrOptions(hasSavedUrl);
  const { options, error: optionsError, isLoading: optionsLoading } = optionsHook;

  const patch = (p: Partial<LidarrSettings>) => onChange((prev) => ({ ...(prev ?? DEFAULT_LIDARR), ...p }));

  const searchOnAdd = settings?.search_on_add ?? true;
  const tagIds = settings?.tag_ids ?? [];

  const toggleTag = (id: number) => {
    patch({ tag_ids: tagIds.includes(id) ? tagIds.filter((t) => t !== id) : [...tagIds, id] });
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!settings) return;
    setIsSaving(true);
    try {
      // The server echoes the legacy auto_search mirror back; never send it, search_on_add is the only source.
      const { auto_search: _legacyAutoSearch, ...payload } = settings;
      const updated = await updateLidarrSettings(payload);
      onChange(updated);
      if (hasSavedUrl) void optionsHook.refresh();
      setHasSavedUrl(Boolean(updated.url));
      onToast('Lidarr settings saved');
    } catch {
      onToast('Failed to save Lidarr settings', 'error');
    } finally {
      setIsSaving(false);
    }
  };

  const handleTest = async () => {
    if (!settings?.url) {
      onToast('Lidarr URL is required to test', 'error');
      return;
    }
    setIsTesting(true);
    try {
      const res = await testLidarrConnection({ url: settings.url, api_key: settings.api_key || '' });
      if (res.online) onToast(`Lidarr online! Version: ${res.version || 'OK'}`);
      else onToast(`Connection failed: ${res.error || 'Offline'}`, 'error');
    } catch {
      onToast('Error testing Lidarr connection', 'error');
    } finally {
      setIsTesting(false);
    }
  };

  const rootFolderKnown = options?.root_folders.some((r) => r.path === settings?.root_folder) ?? false;

  return (
    <div className="space-y-4 max-w-2xl">
      {!isActive && (
        <InactiveBanner
          activeManager={activeManager}
          onRequestSwitch={onRequestSwitch}
          note="Lidarr connection settings stay editable so you can switch."
        />
      )}

      <MachinedCard className="p-6 space-y-5">
        <div className="flex items-center justify-between gap-3 border-b border-[#222222] pb-3">
          <div>
            <h4 className="text-sm font-bold uppercase font-mono text-white">Lidarr Integration</h4>
            <p className="text-xs text-neutral-400 font-mono mt-0.5">Automated music acquisition &amp; trickle sync</p>
          </div>
          <TapeDeckButton
            type="button"
            size="sm"
            disabled={isTesting || !settings?.url}
            onClick={() => void handleTest()}
            icon={isTesting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
          >
            Test Connection
          </TapeDeckButton>
        </div>

        <form onSubmit={handleSave} className="space-y-4">
          <div>
            <label className={labelClass}>Lidarr Host URL</label>
            <input
              type="text"
              value={settings?.url || ''}
              onChange={(e) => patch({ url: e.target.value })}
              placeholder="http://localhost:8686"
              className={inputClass}
            />
          </div>

          <div>
            <label className={labelClass}>Lidarr API Key</label>
            <input
              type="password"
              value={settings?.api_key || ''}
              onChange={(e) => patch({ api_key: e.target.value })}
              placeholder="Leave blank or masked to keep current key"
              className={inputClass}
            />
          </div>

          <fieldset
            disabled={!isActive}
            className={`m-0 min-w-0 border-0 p-0 space-y-4 ${isActive ? '' : 'opacity-50 grayscale'}`}
          >
            <div className="pt-2 border-t border-[#1f1f1f] space-y-4">
              <div className="flex items-center justify-between gap-3">
                <h5 className="text-xs font-bold uppercase font-mono text-white">Add Defaults</h5>
                <TapeDeckButton
                  type="button"
                  size="sm"
                  onClick={() => void optionsHook.refresh()}
                  disabled={!hasSavedUrl || optionsLoading}
                  icon={<RotateCw className={`h-3 w-3 ${optionsLoading ? 'animate-spin' : ''}`} />}
                >
                  Reload options
                </TapeDeckButton>
              </div>

              {!hasSavedUrl && (
                <p className="text-[11px] font-mono text-neutral-500">
                  Enter the Lidarr URL and API key, save, then reload options to pick a root folder and profiles.
                </p>
              )}

              {optionsError && (
                <p className="flex items-start gap-2 text-xs font-mono text-red-300" role="alert">
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  <span>Could not load options from Lidarr: {optionsError}</span>
                </p>
              )}

              <div>
                <label className={compactLabelClass}>Root Folder</label>
                <select
                  value={settings?.root_folder ?? ''}
                  disabled={!options}
                  onChange={(e) => patch({ root_folder: e.target.value || undefined })}
                  className={compactInputClass}
                >
                  <option value="">Default</option>
                  {settings?.root_folder && !rootFolderKnown && (
                    <option value={settings.root_folder}>{settings.root_folder}</option>
                  )}
                  {options?.root_folders.map((r) => (
                    <option key={r.path} value={r.path}>
                      {r.path}
                    </option>
                  ))}
                </select>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <ProfileSelect
                  label="Quality Profile"
                  value={settings?.quality_profile_id}
                  options={options?.quality_profiles ?? null}
                  onChange={(id) => patch({ quality_profile_id: id })}
                />
                <ProfileSelect
                  label="Metadata Profile"
                  value={settings?.metadata_profile_id}
                  options={options?.metadata_profiles ?? null}
                  onChange={(id) => patch({ metadata_profile_id: id })}
                />
              </div>

              <div>
                <label className={compactLabelClass}>Monitor</label>
                <select
                  value={settings?.monitor_option ?? 'all'}
                  onChange={(e) => patch({ monitor_option: e.target.value as LidarrMonitorOption })}
                  className={compactInputClass}
                >
                  {MONITOR_OPTIONS.map((m) => (
                    <option key={m.value} value={m.value}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <span className={compactLabelClass}>Tags</span>
                {options && options.tags.length === 0 && (
                  <p className="text-[11px] font-mono text-neutral-500">No tags are defined in Lidarr.</p>
                )}
                {!options && (
                  <p className="text-[11px] font-mono text-neutral-500">
                    {tagIds.length > 0 ? `${tagIds.length} tag(s) selected. ` : ''}Tag list unavailable until options load.
                  </p>
                )}
                {options && options.tags.length > 0 && (
                  <div className="flex flex-wrap gap-1.5" role="group" aria-label="Lidarr tags">
                    {options.tags.map((t) => (
                      <TapeDeckButton
                        key={t.id}
                        type="button"
                        size="sm"
                        active={tagIds.includes(t.id)}
                        aria-pressed={tagIds.includes(t.id)}
                        onClick={() => toggleTag(t.id)}
                      >
                        {t.label}
                      </TapeDeckButton>
                    ))}
                  </div>
                )}
              </div>

              <div className="flex items-center justify-between gap-3">
                <div>
                  <span className="text-xs font-mono font-medium text-white block">Search on Add</span>
                  <span className="text-[11px] text-neutral-400 font-mono">
                    Trigger a search in Lidarr as soon as a release is requested
                  </span>
                </div>
                <TactileSwitch
                  checked={searchOnAdd}
                  onChange={(val) => patch({ search_on_add: val })}
                  label="Search on Add"
                />
              </div>
            </div>

            <div className="pt-2 border-t border-[#1f1f1f] space-y-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <span className="text-xs font-mono font-medium text-white block">Auto Trickle Sync</span>
                  <span className="text-[11px] text-neutral-400 font-mono">
                    Pace artist ingest calls to prevent Lidarr rate-limiting
                  </span>
                </div>
                <TactileSwitch
                  checked={settings?.auto_trickle ?? false}
                  onChange={(val) => patch({ auto_trickle: val })}
                  label="Auto Trickle"
                />
              </div>

              <div className="grid grid-cols-2 gap-3 pt-2">
                <div>
                  <label className={compactLabelClass}>Trickle Rate (Seconds)</label>
                  <input
                    type="number"
                    step="0.5"
                    min="0.5"
                    value={settings?.trickle_rate_seconds ?? 3.0}
                    onChange={(e) => patch({ trickle_rate_seconds: parseFloat(e.target.value) || 3.0 })}
                    className={compactInputClass}
                  />
                </div>
                <div>
                  <label className={compactLabelClass}>Trickle Batch Size</label>
                  <input
                    type="number"
                    min="1"
                    value={settings?.trickle_batch_size ?? 25}
                    onChange={(e) => patch({ trickle_batch_size: parseInt(e.target.value, 10) || 25 })}
                    className={compactInputClass}
                  />
                </div>
              </div>
            </div>
          </fieldset>

          <div className="flex justify-end pt-3">
            <TapeDeckButton
              type="submit"
              variant="amber"
              size="md"
              disabled={isSaving}
              icon={isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            >
              Save Lidarr Settings
            </TapeDeckButton>
          </div>
        </form>
      </MachinedCard>
    </div>
  );
};
