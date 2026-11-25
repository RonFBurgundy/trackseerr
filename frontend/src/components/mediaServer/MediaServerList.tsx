import React, { useEffect, useState } from 'react';
import { Loader2, Lock, Plus, Save, Trash2, X } from 'lucide-react';
import { ActionBar, ConfirmDangerButton, MachinedCard, PageLoader, TactileSwitch, TapeDeckButton } from '@/components/ui';
import { compactInputClass, compactLabelClass } from '@/components/settings/formClasses';
import { useMediaServerList } from '@/hooks/useMediaServerList';
import { errorMessage } from '@/services/apiClient';
import { deleteMediaServer, saveMediaServer, setLoginMediaServer, testMediaServer } from '@/services/mediaServerService';
import type { MediaServerItem, MediaServerKind, MediaServerPayload } from '@/types/mediaServer';

export interface MediaServerListProps {
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
  onServersChange?: (servers: MediaServerItem[]) => void;
}

const URL_PLACEHOLDERS: Record<MediaServerKind, string> = {
  plex: 'http://plex:32400',
  subsonic: 'http://navidrome:4533',
  jellyfin: 'http://jellyfin:8096',
};

function toMediaServerKind(value: string): MediaServerKind {
  if (value === 'subsonic' || value === 'jellyfin') return value;
  return 'plex';
}

function itemToTestPayload(item: MediaServerItem): MediaServerPayload {
  return {
    id: item.id,
    kind: item.kind,
    name: item.name,
    url: item.url,
    username: item.username,
    password: item.password,
    api_key: item.api_key,
    music_section: item.music_section,
    verify_ssl: item.verify_ssl,
    enabled: item.enabled,
    use_refresh: item.use_refresh,
    use_playlists: item.use_playlists,
    use_users: item.use_users,
    use_scrobbles: item.use_scrobbles,
    sort_order: item.sort_order,
  };
}

export const MediaServerList: React.FC<MediaServerListProps> = ({ onToast, onServersChange }) => {
  const draft = useMediaServerList();
  const {
    servers,
    isLoading,
    error,
    reload,
    editingId,
    kind,
    name,
    url,
    username,
    password,
    apiKey,
    musicSection,
    enabled,
    useRefresh,
    usePlaylists,
    useUsers,
    useScrobbles,
  } = draft;

  const editing = servers.find((s) => s.id === editingId) ?? null;
  const isLocked = editing?.locked ?? false;
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [isTestingDraft, setIsTestingDraft] = useState<boolean>(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [loginUpdatingId, setLoginUpdatingId] = useState<string | null>(null);

  useEffect(() => {
    onServersChange?.(servers);
  }, [servers, onServersChange]);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setIsSaving(true);
    try {
      await saveMediaServer(draft.toPayload(editing));
      draft.reset();
      await reload();
      onToast('Media server saved');
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to save media server'), 'error');
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteMediaServer(id);
      if (editingId === id) draft.reset();
      await reload();
      onToast('Media server removed');
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to delete media server'), 'error');
    }
  };

  const handleTestCard = async (item: MediaServerItem) => {
    setTestingId(item.id);
    try {
      const res = await testMediaServer(itemToTestPayload(item));
      onToast(res.message || (res.ok ? 'Connected' : 'Connection failed'), res.ok ? 'ok' : 'error');
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Connection test failed'), 'error');
    } finally {
      setTestingId(null);
    }
  };

  const handleTestDraft = async () => {
    setIsTestingDraft(true);
    try {
      const res = await testMediaServer(draft.toPayload(editing));
      onToast(res.message || (res.ok ? 'Connected' : 'Connection failed'), res.ok ? 'ok' : 'error');
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Connection test failed'), 'error');
    } finally {
      setIsTestingDraft(false);
    }
  };

  const handleSetLogin = async (id: string) => {
    setLoginUpdatingId(id);
    try {
      await setLoginMediaServer(id);
      await reload();
      onToast('Plex sign-in server updated');
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to update Plex sign-in server'), 'error');
    } finally {
      setLoginUpdatingId(null);
    }
  };

  if (isLoading) {
    return <PageLoader message="Loading media servers" />;
  }

  return (
    <div className="space-y-6">
      {error && (
        <p role="alert" className="text-xs font-mono text-red-300">
          {error}
        </p>
      )}

      {servers.length === 0 ? (
        <p className="text-xs font-mono text-neutral-500 py-4">
          No media servers yet. Trackseerr still manages your library without one.
        </p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 content-start">
          {servers.map((srv) => (
            <MachinedCard
              key={srv.id}
              className={`p-3 sm:p-4 flex items-center justify-between gap-3 ${srv.enabled ? '' : 'opacity-60'}`}
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-bold text-sm text-white">{srv.name}</span>
                  <span className="px-1.5 py-0.5 rounded-[2px] bg-[#1a1a1a] text-[10px] font-mono uppercase text-[#e5a00d]">
                    {srv.kind}
                  </span>
                  {srv.is_login && (
                    <span className="px-1.5 py-0.5 rounded-[2px] bg-[#1a1a1a] text-[10px] font-mono uppercase text-[#e5a00d]">
                      Sign-in
                    </span>
                  )}
                  {srv.locked && (
                    <span className="px-1.5 py-0.5 rounded-[2px] bg-[#1a1a1a] text-[10px] font-mono uppercase text-neutral-300">
                      Env
                    </span>
                  )}
                  {!srv.enabled && (
                    <span className="px-1.5 py-0.5 rounded-[2px] bg-[#1a1a1a] text-[10px] font-mono uppercase text-neutral-500">
                      Disabled
                    </span>
                  )}
                </div>
                <p className="text-xs text-neutral-400 font-mono mt-1 truncate max-w-xs">{srv.url}</p>
              </div>
              <div className="flex flex-wrap items-center gap-2 shrink-0">
                <TapeDeckButton type="button" size="sm" onClick={() => draft.startEdit(srv)}>
                  Edit
                </TapeDeckButton>
                <TapeDeckButton
                  type="button"
                  size="sm"
                  disabled={testingId === srv.id}
                  onClick={() => void handleTestCard(srv)}
                >
                  Test
                </TapeDeckButton>
                {srv.kind === 'plex' && !srv.is_login && (
                  <TapeDeckButton
                    type="button"
                    size="sm"
                    disabled={loginUpdatingId === srv.id}
                    onClick={() => void handleSetLogin(srv.id)}
                  >
                    Use for sign-in
                  </TapeDeckButton>
                )}
                {!srv.locked && (
                  <ConfirmDangerButton
                    onConfirm={() => void handleDelete(srv.id)}
                    icon={<Trash2 className="h-3 w-3" />}
                    ariaLabel={`Delete ${srv.name}`}
                    confirmLabel="Confirm Delete"
                  />
                )}
              </div>
            </MachinedCard>
          ))}
        </div>
      )}

      <MachinedCard className="p-3 sm:p-5 max-w-xl">
        <h4 className="text-xs font-bold uppercase font-mono text-white mb-4">
          {editing ? `Edit media server (${editing.name})` : 'Add media server'}
        </h4>

        {isLocked && (
          <p className="flex items-start gap-2 text-xs font-mono text-amber-300 mb-4" role="note">
            <Lock className="h-4 w-4 shrink-0" aria-hidden="true" />
            <span>
              Set by environment variables. You can rename it, turn it off or change what it&apos;s used for here.
            </span>
          </p>
        )}

        <form onSubmit={(e) => void handleSave(e)} className="space-y-4">
          <div>
            <label htmlFor="ms-kind" className={compactLabelClass}>
              Server type
            </label>
            <select
              id="ms-kind"
              name="kind"
              value={kind}
              disabled={editing !== null}
              onChange={(e) => draft.setKind(toMediaServerKind(e.target.value))}
              className={compactInputClass}
            >
              <option value="plex">Plex</option>
              <option value="subsonic">Subsonic API (Navidrome, Gonic, Airsonic)</option>
              <option value="jellyfin">Jellyfin</option>
            </select>
          </div>

          <div>
            <label htmlFor="ms-name" className={compactLabelClass}>
              Name
            </label>
            <input
              id="ms-name"
              name="name"
              type="text"
              required
              value={name}
              onChange={(e) => draft.setName(e.target.value)}
              placeholder="e.g. Home Plex"
              className={compactInputClass}
              autoComplete="off"
            />
          </div>

          <fieldset disabled={isLocked} className="m-0 min-w-0 border-0 p-0 space-y-4">
            <div>
              <label htmlFor="ms-url" className={compactLabelClass}>
                Server URL
              </label>
              <input
                id="ms-url"
                name="url"
                type="text"
                value={url}
                onChange={(e) => draft.setUrl(e.target.value)}
                placeholder={URL_PLACEHOLDERS[kind]}
                className={compactInputClass}
                autoComplete="off"
              />
            </div>

            {kind === 'plex' && (
              <>
                <div>
                  <label htmlFor="ms-plex-token" className={compactLabelClass}>
                    Plex token
                  </label>
                  <input
                    id="ms-plex-token"
                    name="api_key"
                    type="password"
                    value={apiKey}
                    onChange={(e) => draft.setApiKey(e.target.value)}
                    placeholder="Leave masked to keep the saved token"
                    className={compactInputClass}
                    autoComplete="new-password"
                  />
                </div>
                <div>
                  <label htmlFor="ms-music-section" className={compactLabelClass}>
                    Music library name (optional)
                  </label>
                  <input
                    id="ms-music-section"
                    name="music_section"
                    type="text"
                    value={musicSection}
                    onChange={(e) => draft.setMusicSection(e.target.value)}
                    placeholder="Music"
                    className={compactInputClass}
                    autoComplete="off"
                  />
                </div>
              </>
            )}

            {kind === 'subsonic' && (
              <>
                <div>
                  <label htmlFor="ms-subsonic-user" className={compactLabelClass}>
                    Username
                  </label>
                  <input
                    id="ms-subsonic-user"
                    name="username"
                    type="text"
                    value={username}
                    onChange={(e) => draft.setUsername(e.target.value)}
                    className={compactInputClass}
                    autoComplete="off"
                  />
                </div>
                <div>
                  <label htmlFor="ms-password" className={compactLabelClass}>
                    Password
                  </label>
                  <input
                    id="ms-password"
                    name="password"
                    type="password"
                    value={password}
                    onChange={(e) => draft.setPassword(e.target.value)}
                    placeholder="Leave masked to keep the saved password"
                    className={compactInputClass}
                    autoComplete="new-password"
                  />
                </div>
                <div>
                  <label htmlFor="ms-subsonic-api-key" className={compactLabelClass}>
                    API key (optional, instead of a password)
                  </label>
                  <input
                    id="ms-subsonic-api-key"
                    name="api_key"
                    type="password"
                    value={apiKey}
                    onChange={(e) => draft.setApiKey(e.target.value)}
                    placeholder="Only for servers that support OpenSubsonic API keys"
                    className={compactInputClass}
                    autoComplete="new-password"
                  />
                </div>
                <p className="text-[11px] font-mono text-neutral-500">
                  Playlists are written to this account only; Subsonic servers keep playlists per user.
                </p>
              </>
            )}

            {kind === 'jellyfin' && (
              <>
                <div>
                  <label htmlFor="ms-jellyfin-api-key" className={compactLabelClass}>
                    API key
                  </label>
                  <input
                    id="ms-jellyfin-api-key"
                    name="api_key"
                    type="password"
                    value={apiKey}
                    onChange={(e) => draft.setApiKey(e.target.value)}
                    placeholder="Jellyfin Dashboard > API Keys. Leave masked to keep the saved key"
                    className={compactInputClass}
                    autoComplete="new-password"
                  />
                </div>
                <div>
                  <label htmlFor="ms-jellyfin-user" className={compactLabelClass}>
                    Default account (optional)
                  </label>
                  <input
                    id="ms-jellyfin-user"
                    name="username"
                    type="text"
                    value={username}
                    onChange={(e) => draft.setUsername(e.target.value)}
                    placeholder="Used when a playlist has no sync target; defaults to the first administrator"
                    className={compactInputClass}
                    autoComplete="off"
                  />
                </div>
                <p className="text-[11px] font-mono text-neutral-500">
                  Playlists are created per Jellyfin user: pick sync targets on each playlist. Changing the URL requires
                  entering the API key again.
                </p>
              </>
            )}
          </fieldset>

          <div className="space-y-3 pt-1">
            <TactileSwitch id="ms-enabled" name="enabled" label="Enabled" checked={enabled} onChange={draft.setEnabled} />
            <TactileSwitch
              id="ms-use-refresh"
              name="use_refresh"
              label="Refresh library after imports"
              checked={useRefresh}
              onChange={draft.setUseRefresh}
            />
            <TactileSwitch
              id="ms-use-playlists"
              name="use_playlists"
              label="Push playlists"
              checked={usePlaylists}
              onChange={draft.setUsePlaylists}
            />
            <TactileSwitch
              id="ms-use-users"
              name="use_users"
              label="Import users"
              checked={useUsers}
              onChange={draft.setUseUsers}
            />
            <TactileSwitch
              id="ms-use-scrobbles"
              name="use_scrobbles"
              label="Read play history"
              checked={useScrobbles}
              onChange={draft.setUseScrobbles}
            />
          </div>

          <ActionBar align="end" className="pt-2">
            {editing && (
              <TapeDeckButton type="button" size="sm" onClick={draft.reset} icon={<X className="h-3.5 w-3.5" />}>
                Cancel
              </TapeDeckButton>
            )}
            <TapeDeckButton
              type="button"
              size="sm"
              disabled={isTestingDraft}
              onClick={() => void handleTestDraft()}
              icon={isTestingDraft ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
            >
              Test
            </TapeDeckButton>
            <TapeDeckButton
              type="submit"
              size="sm"
              variant="amber"
              disabled={isSaving}
              icon={editing ? <Save className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
            >
              Save
            </TapeDeckButton>
          </ActionBar>
        </form>
      </MachinedCard>
    </div>
  );
};
