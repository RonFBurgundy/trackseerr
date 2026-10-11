import React, { useState } from 'react';
import { Trash2, Plus, Save, X } from 'lucide-react';
import { TapeDeckButton, MachinedCard, ConfirmDangerButton, ActionBar } from '@/components/ui';
import type { DownloadClientItem } from '@/types/models';
import { saveClientSettings, deleteClientSettings, testClientConnection } from '@/services/settingsService';
import { ApiError } from '@/services/apiClient';
import { useClientDraft, toDriverType } from '@/hooks/useClientDraft';
import { compactInputClass, compactLabelClass } from './formClasses';

export interface ClientsPanelProps {
  clients: DownloadClientItem[];
  reload: () => Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

interface TestStatus {
  ok: boolean;
  message: string;
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

export const ClientsPanel: React.FC<ClientsPanelProps> = ({ clients, reload, onToast }) => {
  const draft = useClientDraft();
  const { type } = draft;
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [isTesting, setIsTesting] = useState<boolean>(false);
  // The serialized payload that last passed a form Test; any field change makes it stale.
  const [testedKey, setTestedKey] = useState<string | null>(null);
  const [status, setStatus] = useState<TestStatus | null>(null);

  const payload = draft.toPayload();
  const payloadKey = JSON.stringify(payload);
  const testPassed = testedKey !== null && testedKey === payloadKey;
  const editing = draft.editingId ? clients.find((c) => c.id === draft.editingId) : undefined;

  const resetForm = () => {
    draft.reset();
    setTestedKey(null);
    setStatus(null);
  };

  const handleStartEdit = (client: DownloadClientItem) => {
    draft.startEdit(client);
    setTestedKey(null);
    setStatus(null);
  };

  const handleFormTest = async () => {
    setIsTesting(true);
    const key = payloadKey;
    try {
      const res = await testClientConnection(payload);
      const message = res.success ? 'Connection verified' : `Connection failed: ${res.message}`;
      setStatus({ ok: res.success, message });
      setTestedKey(res.success ? key : null);
      onToast(res.success ? 'Connection verified!' : message, res.success ? 'ok' : 'error');
    } catch (err) {
      const message = errorMessage(err, 'Connection test error');
      setStatus({ ok: false, message });
      setTestedKey(null);
      onToast(message, 'error');
    } finally {
      setIsTesting(false);
    }
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!draft.name.trim() || !testPassed) return;
    setIsSaving(true);
    const wasEditing = draft.editingId !== null;
    try {
      await saveClientSettings(payload);
      resetForm();
      await reload();
      onToast(wasEditing ? 'Download client saved' : 'Download client added');
    } catch (err) {
      onToast(errorMessage(err, wasEditing ? 'Failed to save client' : 'Failed to add client'), 'error');
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteClientSettings(id);
      if (draft.editingId === id) resetForm();
      await reload();
      onToast('Client deleted');
    } catch {
      onToast('Failed to delete client', 'error');
    }
  };

  const handleCardTest = async (client: DownloadClientItem) => {
    try {
      const res = await testClientConnection({ ...client, id: client.id });
      onToast(res.success ? 'Connection verified!' : `Connection failed: ${res.message}`, res.success ? 'ok' : 'error');
    } catch (err) {
      onToast(errorMessage(err, 'Connection test error'), 'error');
    }
  };

  return (
    <div className="space-y-6">
      {clients.length === 0 ? (
        <p className="text-xs font-mono text-neutral-500 py-4">No download clients configured yet.</p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 content-start">
          {clients.map((c) => (
            <MachinedCard key={c.id} className="p-3 sm:p-4 flex items-center justify-between gap-3">
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-bold text-sm text-white">{c.name}</span>
                  <span className="px-1.5 py-0.5 rounded-[2px] bg-[#1a1a1a] text-[10px] font-mono uppercase text-[#e5a00d]">
                    {c.driver_type}
                  </span>
                </div>
                <p className="text-xs text-neutral-400 font-mono mt-1">
                  {c.host_url}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <TapeDeckButton size="sm" onClick={() => handleStartEdit(c)}>
                  Edit
                </TapeDeckButton>
                <TapeDeckButton size="sm" onClick={() => void handleCardTest(c)}>
                  Test
                </TapeDeckButton>
                <ConfirmDangerButton
                  onConfirm={() => void handleDelete(c.id)}
                  icon={<Trash2 className="h-3 w-3" />}
                  ariaLabel="Delete client"
                  confirmLabel="Confirm Delete"
                />
              </div>
            </MachinedCard>
          ))}
        </div>
      )}

      <MachinedCard className="p-3 sm:p-5 max-w-xl">
        <h4 className="text-xs font-bold uppercase font-mono text-white mb-4">
          {draft.editingId ? `Edit Download Client (${editing?.name ?? draft.name})` : 'Add Download Client'}
        </h4>
        <form onSubmit={handleSave} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="client-name" className={compactLabelClass}>Name</label>
              <input
                id="client-name"
                name="name"
                type="text"
                required
                value={draft.name}
                onChange={(e) => draft.setName(e.target.value)}
                placeholder="e.g. Local BitTorrent"
                className={compactInputClass}
              />
            </div>
            <div>
              <label htmlFor="client-type" className={compactLabelClass}>Type</label>
              <select
                id="client-type"
                name="type"
                value={type}
                disabled={draft.editingId !== null}
                onChange={(e) => draft.setType(toDriverType(e.target.value))}
                className={compactInputClass}
              >
                <option value="slskd">slskd</option>
                <option value="sabnzbd">SABnzbd</option>
                <option value="qbittorrent">qBittorrent</option>
                <option value="transmission">Transmission</option>
                <option value="deluge">Deluge</option>
                <option value="nzbget">NZBGet</option>
              </select>
            </div>
          </div>

          <div>
            <label htmlFor="client-host" className={compactLabelClass}>Host URL</label>
            <input
              id="client-host"
              name="host_url"
              type="url"
              required
              value={draft.hostUrl}
              onChange={(e) => draft.setHostUrl(e.target.value)}
              placeholder="http://localhost:8080"
              className={compactInputClass}
            />
          </div>

          {type === 'transmission' && (
            <div>
              <label htmlFor="client-rpc-path" className={compactLabelClass}>RPC Path</label>
              <input
                id="client-rpc-path"
                name="rpc_path"
                type="text"
                value={draft.rpcPath}
                onChange={(e) => draft.setRpcPath(e.target.value)}
                placeholder="/transmission/rpc"
                className={compactInputClass}
              />
            </div>
          )}

          {(type === 'sabnzbd' || type === 'slskd') && (
            <div>
              <label htmlFor="client-api-key" className={compactLabelClass}>
                API Key {type === 'slskd' && <span className="text-[#e5a00d]">*</span>}
              </label>
              <input
                id="client-api-key"
                name="api_key"
                type="password"
                required={type === 'slskd'}
                value={draft.apiKey}
                onChange={(e) => draft.setApiKey(e.target.value)}
                placeholder={type === 'slskd' ? 'slskd API key' : 'SABnzbd API key'}
                autoComplete="new-password"
                className={compactInputClass}
              />
              {type === 'slskd' && (
                <p className="text-[10px] font-mono text-neutral-500 mt-1">
                  slskd → Options → web.authentication.api_keys (role readwrite)
                </p>
              )}
            </div>
          )}

          {(type === 'qbittorrent' || type === 'transmission' || type === 'nzbget') && (
            <div>
              <label htmlFor="client-username" className={compactLabelClass}>
                Username {type === 'nzbget' && <span className="text-[#e5a00d]">*</span>}
              </label>
              <input
                id="client-username"
                name="username"
                type="text"
                required={type === 'nzbget'}
                value={draft.username}
                onChange={(e) => draft.setUsername(e.target.value)}
                placeholder="Username"
                autoComplete="username"
                className={compactInputClass}
              />
            </div>
          )}

          {(type === 'qbittorrent' ||
            type === 'transmission' ||
            type === 'deluge' ||
            type === 'nzbget') && (
            <div>
              <label htmlFor="client-password" className={compactLabelClass}>
                Password {(type === 'deluge' || type === 'nzbget') && <span className="text-[#e5a00d]">*</span>}
              </label>
              <input
                id="client-password"
                name="password"
                type="password"
                required={type === 'deluge' || type === 'nzbget'}
                value={draft.password}
                onChange={(e) => draft.setPassword(e.target.value)}
                placeholder="Password"
                autoComplete="current-password"
                className={compactInputClass}
              />
            </div>
          )}

          {(type === 'sabnzbd' ||
            type === 'qbittorrent' ||
            type === 'transmission' ||
            type === 'deluge' ||
            type === 'nzbget') && (
            <div>
              <label htmlFor="client-category" className={compactLabelClass}>
                {type === 'deluge' ? 'Label' : type === 'transmission' ? 'Category / Label' : 'Category'}
              </label>
              <input
                id="client-category"
                name="category"
                type="text"
                value={draft.category}
                onChange={(e) => draft.setCategory(e.target.value)}
                placeholder="e.g. trackseerr"
                className={compactInputClass}
              />
            </div>
          )}

          <ActionBar align="end" className="pt-2">
            {draft.editingId && (
              <TapeDeckButton type="button" size="sm" onClick={resetForm} icon={<X className="h-3.5 w-3.5" />}>
                Cancel
              </TapeDeckButton>
            )}
            <TapeDeckButton type="button" size="sm" disabled={isTesting || isSaving} onClick={() => void handleFormTest()}>
              {isTesting ? 'Testing...' : 'Test'}
            </TapeDeckButton>
            <TapeDeckButton
              type="submit"
              size="sm"
              variant="amber"
              disabled={isSaving || !testPassed}
              icon={draft.editingId ? <Save className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
            >
              {draft.editingId ? 'Save' : 'Add Client'}
            </TapeDeckButton>
          </ActionBar>
          {status && (!status.ok || testPassed) && (
            <p
              role="status"
              className={`text-xs font-mono ${status.ok ? 'text-[#22c55e]' : 'text-[#ef4444]'}`}
            >
              {status.message}
            </p>
          )}
        </form>
      </MachinedCard>
    </div>
  );
};
