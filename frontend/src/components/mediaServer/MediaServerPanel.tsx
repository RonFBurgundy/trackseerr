import React, { useEffect, useState } from 'react';
import { Loader2, Save, Lock } from 'lucide-react';
import { MachinedCard, TapeDeckButton } from '@/components/ui';
import { inputClass, labelClass } from '@/components/settings/formClasses';
import type { MediaServerSettings, MediaServerSettingsInput, SettableMediaServerType } from '@/types/mediaServer';
import { useMediaServerSettings } from '@/hooks/useMediaServerSettings';

export interface MediaServerPanelProps {
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

const EMPTY: MediaServerSettingsInput = { type: 'none', url: '', username: '', password: '', api_key: '' };

function toInput(settings: MediaServerSettings | null): MediaServerSettingsInput {
  if (!settings) return EMPTY;
  return {
    type: toSettableType(settings.type),
    url: settings.url,
    username: settings.username,
    password: settings.password,
    api_key: settings.api_key,
  };
}

const TYPE_LABELS: Record<string, string> = { plex: 'Plex', subsonic: 'Subsonic', jellyfin: 'Jellyfin', none: 'None' };

function toSettableType(value: string): SettableMediaServerType {
  if (value === 'subsonic' || value === 'jellyfin') return value;
  return 'none';
}

/** Settings > Media Server: pick Subsonic (Navidrome, Gonic, Airsonic), Jellyfin or none; Plex is set through the environment. */
export const MediaServerPanel: React.FC<MediaServerPanelProps> = ({ onToast }) => {
  const hook = useMediaServerSettings(true);
  const { settings } = hook;
  const [form, setForm] = useState<MediaServerSettingsInput>(EMPTY);

  useEffect(() => {
    if (settings) setForm(toInput(settings));
  }, [settings]);

  if (hook.isLoading) {
    return (
      <div className="flex items-center gap-2 py-8 text-xs font-mono text-neutral-400">
        <Loader2 className="h-4 w-4 animate-spin text-[#e5a00d]" />
        <span>Reading media server settings...</span>
      </div>
    );
  }

  const locked = settings?.locked_by_env ?? false;
  const patch = (p: Partial<MediaServerSettingsInput>) => setForm((prev) => ({ ...prev, ...p }));
  const isSubsonic = form.type === 'subsonic';
  const isJellyfin = form.type === 'jellyfin';
  const hasServer = isSubsonic || isJellyfin;

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    const res = await hook.save(form);
    onToast(res.message, res.ok ? 'ok' : 'error');
  };

  const handleTest = async () => {
    const res = await hook.test(form);
    onToast(res.message || (res.ok ? 'Connected' : 'Connection failed'), res.ok ? 'ok' : 'error');
  };

  return (
    <div className="space-y-4 max-w-2xl">
      {hook.error && (
        <p role="alert" className="text-xs font-mono text-red-300">
          {hook.error}
        </p>
      )}

      <MachinedCard className="p-6 space-y-5">
        <div className="border-b border-[#222222] pb-3">
          <h4 className="text-sm font-bold uppercase font-mono text-white">Media Server</h4>
          <p className="text-xs text-neutral-400 font-mono mt-0.5">
            Active: {TYPE_LABELS[settings?.effective_type ?? 'none']}. Playlists are pushed here and library scans are
            triggered after imports.
          </p>
        </div>

        {locked && (
          <p className="flex items-start gap-2 text-xs font-mono text-amber-300" role="note">
            <Lock className="h-4 w-4 shrink-0" aria-hidden="true" />
            <span>
              The media server is set by environment variables (MEDIA_SERVER, PLEX_*, SUBSONIC_*, JELLYFIN_*). Remove them to manage
              it here.
            </span>
          </p>
        )}

        <form onSubmit={(e) => void handleSave(e)} className="space-y-4">
          <fieldset disabled={locked} className="m-0 min-w-0 border-0 p-0 space-y-4">
            <div>
              <label htmlFor="media-server-type" className={labelClass}>
                Server type
              </label>
              <select
                id="media-server-type"
                name="media-server-type"
                value={form.type}
                onChange={(e) => patch({ type: toSettableType(e.target.value) })}
                className={inputClass}
              >
                <option value="none">None (Trackseerr manages the library only)</option>
                <option value="subsonic">Subsonic API (Navidrome, Gonic, Airsonic)</option>
                <option value="jellyfin">Jellyfin</option>
              </select>
              <p className="mt-1 text-[11px] font-mono text-neutral-500">
                Plex is configured with PLEX_URL and PLEX_TOKEN in the environment.
              </p>
            </div>

            {hasServer && (
              <>
                <div>
                  <label htmlFor="media-server-url" className={labelClass}>
                    Server URL
                  </label>
                  <input
                    id="media-server-url"
                    name="media-server-url"
                    type="text"
                    value={form.url}
                    onChange={(e) => patch({ url: e.target.value })}
                    placeholder={isJellyfin ? 'http://jellyfin:8096' : 'http://navidrome:4533'}
                    className={inputClass}
                    autoComplete="off"
                  />
                </div>
                {isJellyfin && (
                  <>
                    <div>
                      <label htmlFor="media-server-api-key" className={labelClass}>
                        API key
                      </label>
                      <input
                        id="media-server-api-key"
                        name="media-server-api-key"
                        type="password"
                        value={form.api_key}
                        onChange={(e) => patch({ api_key: e.target.value })}
                        placeholder="Jellyfin Dashboard > API Keys. Leave masked to keep the saved key"
                        className={inputClass}
                        autoComplete="new-password"
                      />
                    </div>
                    <div>
                      <label htmlFor="media-server-user" className={labelClass}>
                        Default user (optional)
                      </label>
                      <input
                        id="media-server-user"
                        name="media-server-user"
                        type="text"
                        value={form.username}
                        onChange={(e) => patch({ username: e.target.value })}
                        placeholder="Used when a playlist has no sync target; defaults to the first administrator"
                        className={inputClass}
                        autoComplete="off"
                      />
                    </div>
                    <p className="text-[11px] font-mono text-neutral-500">
                      Playlists are created per Jellyfin user: pick sync targets on each playlist. Changing the URL
                      requires entering the API key again.
                    </p>
                  </>
                )}
                {isSubsonic && (
                  <>
                <div>
                  <label htmlFor="media-server-subsonic-user" className={labelClass}>
                    Username
                  </label>
                  <input
                    id="media-server-subsonic-user"
                    name="media-server-subsonic-user"
                    type="text"
                    value={form.username}
                    onChange={(e) => patch({ username: e.target.value })}
                    className={inputClass}
                    autoComplete="off"
                  />
                </div>
                <div>
                  <label htmlFor="media-server-password" className={labelClass}>
                    Password
                  </label>
                  <input
                    id="media-server-password"
                    name="media-server-password"
                    type="password"
                    value={form.password}
                    onChange={(e) => patch({ password: e.target.value })}
                    placeholder="Leave masked to keep the saved password"
                    className={inputClass}
                    autoComplete="new-password"
                  />
                </div>
                <div>
                  <label htmlFor="media-server-subsonic-api-key" className={labelClass}>
                    API key (optional, instead of a password)
                  </label>
                  <input
                    id="media-server-subsonic-api-key"
                    name="media-server-subsonic-api-key"
                    type="password"
                    value={form.api_key}
                    onChange={(e) => patch({ api_key: e.target.value })}
                    placeholder="Only for servers that support OpenSubsonic API keys"
                    className={inputClass}
                    autoComplete="new-password"
                  />
                </div>
                <p className="text-[11px] font-mono text-neutral-500">
                  Playlists are written to this account only; Subsonic servers keep playlists per user.
                </p>
                  </>
                )}
              </>
            )}
          </fieldset>

          <div className="flex flex-wrap gap-2 pt-2 border-t border-[#1f1f1f]">
            <TapeDeckButton
              type="button"
              size="sm"
              disabled={locked || !hasServer || hook.isTesting}
              onClick={() => void handleTest()}
              icon={hook.isTesting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
            >
              Test Connection
            </TapeDeckButton>
            <TapeDeckButton
              type="submit"
              size="sm"
              active
              disabled={locked || hook.isSaving}
              icon={hook.isSaving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
            >
              Save
            </TapeDeckButton>
          </div>
        </form>
      </MachinedCard>
    </div>
  );
};
