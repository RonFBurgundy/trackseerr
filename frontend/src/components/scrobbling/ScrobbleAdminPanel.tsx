import React, { useEffect, useState } from 'react';
import { Copy, RefreshCw } from 'lucide-react';
import type {
  AdminScrobbleConfigBody,
  ScrobbleConfig,
  ScrobbleServerConfig,
  ScrobbleServerConfigBody,
} from '@/types/models';
import { MachinedCard, TapeDeckButton, TactileSwitch, ObsidianModal } from '@/components/ui';

const inputClass =
  'w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d] disabled:opacity-50';
const labelClass = 'block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1.5';

export interface ScrobbleAdminPanelProps {
  serverConfig: ScrobbleServerConfig | null;
  webhookUrl: string | null;
  users: ScrobbleConfig[];
  isSaving: boolean;
  onSaveServerConfig: (body: ScrobbleServerConfigBody) => Promise<boolean>;
  onRotateWebhook: () => Promise<void>;
  onSaveUser: (userId: string, body: AdminScrobbleConfigBody) => Promise<boolean>;
  /** False when no media server is connected: the Plex history poll and webhook controls are hidden. */
  showPlexOptions?: boolean;
}

export const ScrobbleAdminPanel: React.FC<ScrobbleAdminPanelProps> = ({
  serverConfig,
  webhookUrl,
  users,
  isSaving,
  onSaveServerConfig,
  onRotateWebhook,
  onSaveUser,
  showPlexOptions = true,
}) => {
  const [apiKey, setApiKey] = useState<string>('');
  const [apiSecret, setApiSecret] = useState<string>('');
  const [pollMinutes, setPollMinutes] = useState<number>(15);
  const [copied, setCopied] = useState<boolean>(false);
  const [confirmRotate, setConfirmRotate] = useState<boolean>(false);

  const [editing, setEditing] = useState<ScrobbleConfig | null>(null);
  const [editEnabled, setEditEnabled] = useState<boolean>(true);
  const [editUnlinkLfm, setEditUnlinkLfm] = useState<boolean>(false);
  const [editLfmName, setEditLfmName] = useState<string>('');
  const [editLfmKey, setEditLfmKey] = useState<string>('');
  const [editLbToken, setEditLbToken] = useState<string>('');
  const [editUnlinkLb, setEditUnlinkLb] = useState<boolean>(false);

  useEffect(() => {
    if (serverConfig) setPollMinutes(serverConfig.plex_history_poll_minutes);
  }, [serverConfig]);

  const fromEnv = serverConfig?.lastfm_from_env ?? false;

  const handleSaveServer = async (e: React.FormEvent) => {
    e.preventDefault();
    const body: ScrobbleServerConfigBody = { plex_history_poll_minutes: pollMinutes };
    if (!fromEnv) {
      if (apiKey.trim()) body.lastfm_api_key = apiKey.trim();
      if (apiSecret.trim()) body.lastfm_api_secret = apiSecret.trim();
    }
    if (await onSaveServerConfig(body)) {
      setApiKey('');
      setApiSecret('');
    }
  };

  const handleCopy = async () => {
    if (!webhookUrl) return;
    try {
      await navigator.clipboard.writeText(webhookUrl);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  const openEdit = (u: ScrobbleConfig) => {
    setEditing(u);
    setEditEnabled(u.scrobbling_enabled);
    setEditUnlinkLfm(false);
    setEditLfmName('');
    setEditLfmKey('');
    setEditLbToken('');
    setEditUnlinkLb(false);
  };

  const handleSaveUser = async () => {
    if (!editing) return;
    const body: AdminScrobbleConfigBody = { scrobbling_enabled: editEnabled };
    if (editUnlinkLfm) body.unlink_lastfm = true;
    if (editLfmName.trim() && editLfmKey.trim()) {
      body.lastfm_username = editLfmName.trim();
      body.lastfm_session_key = editLfmKey.trim();
    }
    if (editUnlinkLb) body.listenbrainz_token = null;
    else if (editLbToken.trim()) body.listenbrainz_token = editLbToken.trim();
    if (await onSaveUser(editing.user_id, body)) setEditing(null);
  };

  return (
    <div className="space-y-6">
      <MachinedCard className="p-3 sm:p-6">
        <h3 className="text-sm font-bold uppercase tracking-wider text-white mb-4">Server Scrobbling</h3>
        <form onSubmit={handleSaveServer} className="space-y-4 max-w-2xl">
          <div>
            <label htmlFor="scrobble-lastfm-api-key" className={labelClass}>Last.fm API Key</label>
            <input
              id="scrobble-lastfm-api-key"
              name="lastfm-api-key"
              autoComplete="off"
              type="text"
              value={apiKey}
              disabled={fromEnv}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={serverConfig?.lastfm_api_key_masked || 'Not set'}
              className={inputClass}
            />
          </div>
          <div>
            <label htmlFor="scrobble-lastfm-api-secret" className={labelClass}>Last.fm API Secret (write-only)</label>
            <input
              id="scrobble-lastfm-api-secret"
              name="lastfm-api-secret"
              type="password"
              autoComplete="new-password"
              value={apiSecret}
              disabled={fromEnv}
              onChange={(e) => setApiSecret(e.target.value)}
              placeholder={serverConfig?.lastfm_configured ? 'Saved - enter to replace' : 'Not set'}
              className={inputClass}
            />
          </div>
          {fromEnv && (
            <p className="text-xs text-neutral-400 font-mono">
              Managed by LASTFM_API_KEY / LASTFM_API_SECRET environment variables.
            </p>
          )}
          {showPlexOptions && (
            <div>
              <label htmlFor="scrobble-plex-poll-minutes" className={labelClass}>Plex History Poll (minutes, 0 = off)</label>
              <input
                id="scrobble-plex-poll-minutes"
                name="plex-poll-minutes"
                type="number"
                min={0}
                value={pollMinutes}
                onChange={(e) => setPollMinutes(Math.max(0, Number(e.target.value) || 0))}
                className={inputClass}
              />
            </div>
          )}
          <TapeDeckButton type="submit" variant="amber" disabled={isSaving}>
            Save Server Settings
          </TapeDeckButton>
        </form>
      </MachinedCard>

      {showPlexOptions && (
        <MachinedCard className="p-3 sm:p-6">
          <h3 className="text-sm font-bold uppercase tracking-wider text-white mb-2">Plex Webhook</h3>
          <p className="text-xs text-neutral-400 mb-3">
            Add this URL in Plex under Settings, Webhooks (requires Plex Pass on the server owner).
          </p>
          <div className="bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-xs font-mono text-neutral-200 break-all select-all">
            {webhookUrl ?? 'Unavailable'}
          </div>
          <div className="flex flex-wrap items-center gap-2 mt-3">
            <TapeDeckButton size="sm" disabled={!webhookUrl} onClick={() => void handleCopy()} icon={<Copy className="h-3.5 w-3.5" />}>
              {copied ? 'Copied' : 'Copy'}
            </TapeDeckButton>
            {confirmRotate ? (
              <>
                <span className="text-xs text-neutral-400">Existing Plex webhook will stop working.</span>
                <TapeDeckButton
                  size="sm"
                  variant="danger"
                  disabled={isSaving}
                  onClick={() => {
                    setConfirmRotate(false);
                    void onRotateWebhook();
                  }}
                >
                  Confirm Rotate
                </TapeDeckButton>
                <TapeDeckButton size="sm" onClick={() => setConfirmRotate(false)}>
                  Cancel
                </TapeDeckButton>
              </>
            ) : (
              <TapeDeckButton size="sm" disabled={isSaving} onClick={() => setConfirmRotate(true)} icon={<RefreshCw className="h-3.5 w-3.5" />}>
                Rotate
              </TapeDeckButton>
            )}
          </div>
        </MachinedCard>
      )}

      <MachinedCard className="p-3 sm:p-6">
        <h3 className="text-sm font-bold uppercase tracking-wider text-white mb-4">Users</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm text-left">
            <thead>
              <tr className="text-xs uppercase font-mono text-neutral-400 border-b border-[#222222]">
                <th className="py-2 pr-3">User</th>
                <th className="py-2 pr-3">Last.fm</th>
                <th className="py-2 pr-3">ListenBrainz</th>
                <th className="py-2 pr-3">Enabled</th>
                <th className="py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-[#222222]">
              {users.map((u) => (
                <tr key={u.user_id}>
                  <td className="py-2 pr-3 text-white">{u.username}</td>
                  <td className="py-2 pr-3 text-neutral-300">{u.lastfm_connected ? u.lastfm_username ?? 'Yes' : '-'}</td>
                  <td className="py-2 pr-3 text-neutral-300">
                    {u.listenbrainz_connected ? u.listenbrainz_username ?? 'Yes' : '-'}
                  </td>
                  <td className="py-2 pr-3 text-neutral-300">{u.scrobbling_enabled ? 'On' : 'Off'}</td>
                  <td className="py-2 text-right">
                    <TapeDeckButton size="sm" onClick={() => openEdit(u)}>
                      Edit
                    </TapeDeckButton>
                  </td>
                </tr>
              ))}
              {users.length === 0 && (
                <tr>
                  <td colSpan={5} className="py-4 text-neutral-500 font-mono text-sm">
                    No users yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </MachinedCard>

      <ObsidianModal
        isOpen={editing !== null}
        onClose={() => setEditing(null)}
        title={editing ? `Edit ${editing.username}` : 'Edit user'}
        subtitle="Scrobbling configuration on behalf of this user"
        footer={
          <>
            <TapeDeckButton onClick={() => setEditing(null)}>Cancel</TapeDeckButton>
            <TapeDeckButton variant="amber" disabled={isSaving} onClick={() => void handleSaveUser()}>
              Save
            </TapeDeckButton>
          </>
        }
      >
        <div className="space-y-5">
          <TactileSwitch checked={editEnabled} onChange={setEditEnabled} label="Scrobbling enabled" />
          <div className="space-y-2">
            <div className={labelClass}>Last.fm</div>
            {editing?.lastfm_connected && (
              <TactileSwitch checked={editUnlinkLfm} onChange={setEditUnlinkLfm} label="Unlink Last.fm" />
            )}
            <input
              id="scrobble-edit-lastfm-username"
              name="lastfm-username"
              aria-label="Last.fm username"
              type="text"
              value={editLfmName}
              onChange={(e) => setEditLfmName(e.target.value)}
              placeholder="Last.fm username"
              className={inputClass}
            />
            <input
              id="scrobble-edit-lastfm-session-key"
              name="lastfm-session-key"
              aria-label="Last.fm session key"
              type="password"
              autoComplete="off"
              value={editLfmKey}
              onChange={(e) => setEditLfmKey(e.target.value)}
              placeholder="Last.fm session key"
              className={inputClass}
            />
          </div>
          <div className="space-y-2">
            <div className={labelClass}>ListenBrainz</div>
            {editing?.listenbrainz_connected && (
              <TactileSwitch checked={editUnlinkLb} onChange={setEditUnlinkLb} label="Unlink ListenBrainz" />
            )}
            <input
              id="scrobble-edit-listenbrainz-token"
              name="listenbrainz-token"
              aria-label="ListenBrainz token"
              type="password"
              autoComplete="off"
              value={editLbToken}
              disabled={editUnlinkLb}
              onChange={(e) => setEditLbToken(e.target.value)}
              placeholder="ListenBrainz token"
              className={inputClass}
            />
          </div>
        </div>
      </ObsidianModal>
    </div>
  );
};
