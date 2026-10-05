import React, { useState, useEffect, useRef } from 'react';
import { Loader2, Save, AlertTriangle, RotateCw } from 'lucide-react';
import { TapeDeckButton, MachinedCard, TactileSwitch, ActionBar } from '@/components/ui';
import type { LidarrSettings, LibraryManagerMode } from '@/types/models';
import { updateLidarrSettings, testLidarrConnection } from '@/services/settingsService';
import { useLidarrDefaults } from '@/hooks/useLidarrDefaults';
import { InactiveBanner } from './InactiveGate';
import { inputClass, labelClass, compactInputClass, compactLabelClass } from './formClasses';

const DEFAULT_LIDARR: LidarrSettings = {
  url: '',
  search_on_add: true,
  auto_trickle: false,
  trickle_rate_seconds: 3.0,
  trickle_batch_size: 25,
};

export interface LidarrPanelProps {
  settings: LidarrSettings | null;
  onChange: React.Dispatch<React.SetStateAction<LidarrSettings | null>>;
  /** True when Lidarr is the active library manager. */
  isActive: boolean;
  activeManager: LibraryManagerMode;
  onRequestSwitch: (mode: LibraryManagerMode) => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

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
  // Defaults come from the *saved* Lidarr connection, so only enable once a saved URL exists.
  const [hasSavedUrl, setHasSavedUrl] = useState<boolean>(false);
  const seeded = useRef<boolean>(false);
  useEffect(() => {
    if (settings && !seeded.current) {
      seeded.current = true;
      setHasSavedUrl(Boolean(settings.url));
    }
  }, [settings]);
  const defaultsHook = useLidarrDefaults(hasSavedUrl);
  const { defaults, error: defaultsError, isLoading: defaultsLoading } = defaultsHook;

  const patch = (p: Partial<LidarrSettings>) => onChange((prev) => ({ ...(prev ?? DEFAULT_LIDARR), ...p }));

  const searchOnAdd = settings?.search_on_add ?? true;

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!settings) return;
    setIsSaving(true);
    try {
      // The server echoes the legacy auto_search mirror back; never send it, search_on_add is the only source.
      const { auto_search: _legacyAutoSearch, ...payload } = settings;
      const updated = await updateLidarrSettings(payload);
      onChange(updated);
      if (hasSavedUrl) void defaultsHook.refresh();
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

  const rootFolders = defaults?.root_folders ?? [];
  const rootFolderKnown = rootFolders.includes(settings?.root_folder ?? '');
  const monitorLabel = (value: string): string => value.charAt(0).toUpperCase() + value.slice(1);

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
            <label htmlFor="lidarr-lidarr-host-url" className={labelClass}>Lidarr Host URL</label>
            <input id="lidarr-lidarr-host-url" name="lidarr-host-url"
              type="text"
              value={settings?.url || ''}
              onChange={(e) => patch({ url: e.target.value })}
              placeholder="http://localhost:8686"
              className={inputClass}
            />
          </div>

          <div>
            <label htmlFor="lidarr-lidarr-api-key" className={labelClass}>Lidarr API Key</label>
            <input id="lidarr-lidarr-api-key" name="lidarr-api-key"
              type="password"
              autoComplete="off"
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
                <h5 className="text-xs font-bold uppercase font-mono text-white">Root Folder</h5>
                <TapeDeckButton
                  type="button"
                  size="sm"
                  onClick={() => void defaultsHook.refresh()}
                  disabled={!hasSavedUrl || defaultsLoading}
                  icon={<RotateCw className={`h-3 w-3 ${defaultsLoading ? 'animate-spin' : ''}`} />}
                >
                  Reload from Lidarr
                </TapeDeckButton>
              </div>

              {!hasSavedUrl && (
                <p className="text-[11px] font-mono text-neutral-500">
                  Enter the Lidarr URL and API key, save, then reload to pick a root folder.
                </p>
              )}

              {defaultsError && (
                <p className="flex items-start gap-2 text-xs font-mono text-red-300" role="alert">
                  <AlertTriangle className="h-4 w-4 shrink-0" />
                  <span>Could not load defaults from Lidarr: {defaultsError}</span>
                </p>
              )}

              <div>
                <label htmlFor="lidarr-root-folder" className={compactLabelClass}>Root Folder</label>
                <select id="lidarr-root-folder" name="root-folder"
                  value={settings?.root_folder ?? ''}
                  disabled={!defaults}
                  onChange={(e) => patch({ root_folder: e.target.value || undefined })}
                  className={compactInputClass}
                >
                  <option value="">First Lidarr root folder</option>
                  {settings?.root_folder && !rootFolderKnown && (
                    <option value={settings.root_folder}>{settings.root_folder}</option>
                  )}
                  {rootFolders.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </div>

              {defaults && (
                <div className="border border-[#222222] bg-[#0d0d0d] rounded-[4px] p-3 space-y-2" aria-label="From your Lidarr">
                  <span className={compactLabelClass}>From your Lidarr</span>
                  {defaults.source === 'fallback' && (
                    <p className="flex items-start gap-2 text-[11px] font-mono text-amber-300" role="alert">
                      <AlertTriangle className="h-4 w-4 shrink-0" />
                      <span>
                        This Lidarr does not report root-folder defaults. Trackseerr is using its first quality and
                        metadata profiles, monitoring all albums, and no tags. Set defaults on the root folder in Lidarr
                        or update Lidarr.
                      </span>
                    </p>
                  )}
                  <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1 text-xs font-mono">
                    <div className="flex justify-between gap-3">
                      <dt className="text-neutral-500">Root folder</dt>
                      <dd className="text-white truncate" title={defaults.root_folder}>{defaults.root_folder}</dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt className="text-neutral-500">Quality profile</dt>
                      <dd className="text-white truncate">{defaults.quality_profile.name}</dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt className="text-neutral-500">Metadata profile</dt>
                      <dd className="text-white truncate">{defaults.metadata_profile.name}</dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt className="text-neutral-500">Monitor</dt>
                      <dd className="text-white">{monitorLabel(defaults.monitor)}</dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt className="text-neutral-500">New albums</dt>
                      <dd className="text-white">{monitorLabel(defaults.new_item_monitor)}</dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt className="text-neutral-500">Tags</dt>
                      <dd className="text-white truncate">
                        {defaults.tags.length > 0 ? defaults.tags.map((t) => t.label).join(', ') : 'None'}
                      </dd>
                    </div>
                  </dl>
                  <p className="text-[11px] font-mono text-neutral-400">
                    Trackseerr adds artists with these root-folder defaults from Lidarr (Settings → Media Management →
                    Root Folders). Song and album requests add new artists unmonitored and monitor only the release
                    needed.
                  </p>
                </div>
              )}

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

              {defaults?.singles_enabled === true && (
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <span className="text-xs font-mono font-medium text-white block">
                      Prefer singles for song requests
                    </span>
                    <span className="text-[11px] text-neutral-400 font-mono">
                      When a song was released as a single, monitor the single instead of the album it appears on.
                    </span>
                  </div>
                  <TactileSwitch
                    checked={settings?.prefer_singles ?? true}
                    onChange={(val) => patch({ prefer_singles: val })}
                    label="Prefer singles for song requests"
                  />
                </div>
              )}
              {defaults?.singles_enabled === false && (
                <p className="text-[11px] text-neutral-400 font-mono">
                  Your Lidarr metadata profile excludes singles, so song requests always use albums.
                </p>
              )}
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
                  <label htmlFor="lidarr-trickle-rate-seconds" className={compactLabelClass}>Trickle Rate (Seconds)</label>
                  <input id="lidarr-trickle-rate-seconds" name="trickle-rate-seconds"
                    type="number"
                    step="0.5"
                    min="0.5"
                    value={settings?.trickle_rate_seconds ?? 3.0}
                    onChange={(e) => patch({ trickle_rate_seconds: parseFloat(e.target.value) || 3.0 })}
                    className={compactInputClass}
                  />
                </div>
                <div>
                  <label htmlFor="lidarr-trickle-batch-size" className={compactLabelClass}>Trickle Batch Size</label>
                  <input id="lidarr-trickle-batch-size" name="trickle-batch-size"
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

          <ActionBar align="end" className="pt-3">
            <TapeDeckButton
              type="submit"
              variant="amber"
              size="md"
              disabled={isSaving}
              icon={isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            >
              Save Lidarr Settings
            </TapeDeckButton>
          </ActionBar>
        </form>
      </MachinedCard>
    </div>
  );
};
