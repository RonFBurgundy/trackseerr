import React, { useState } from 'react';
import { Trash2, Plus } from 'lucide-react';
import { TapeDeckButton, MachinedCard, ConfirmDangerButton, ActionBar } from '@/components/ui';
import type { DownloadClientItem, DownloadDriverType } from '@/types/models';
import type { Schema } from '@/types/apiSchema';
import { saveClientSettings, deleteClientSettings, testClientConnection } from '@/services/settingsService';
import { compactInputClass, compactLabelClass } from './formClasses';

export interface ClientsPanelProps {
  clients: DownloadClientItem[];
  reload: () => Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

const DRIVER_TYPES: ReadonlyArray<DownloadDriverType> = [
  'slskd',
  'sabnzbd',
  'qbittorrent',
  'transmission',
  'deluge',
  'nzbget',
];

const DEFAULT_URLS: Record<DownloadDriverType, string> = {
  slskd: 'http://localhost:5030',
  sabnzbd: 'http://localhost:8080',
  qbittorrent: 'http://localhost:8080',
  transmission: 'http://localhost:9091',
  deluge: 'http://localhost:8112',
  nzbget: 'http://localhost:6789',
  lidarr: 'http://localhost:8686',
};

const DEFAULT_CATEGORIES: Partial<Record<DownloadDriverType, string>> = {
  sabnzbd: 'music',
  qbittorrent: 'trackseerr',
  transmission: 'trackseerr',
  deluge: 'trackseerr',
  nzbget: 'music',
};

function toDriverType(value: string): DownloadDriverType {
  return DRIVER_TYPES.find((t) => t === value) ?? 'slskd';
}

export const ClientsPanel: React.FC<ClientsPanelProps> = ({ clients, reload, onToast }) => {
  const [name, setName] = useState<string>('');
  const [type, setType] = useState<DownloadDriverType>('slskd');
  const [hostUrl, setHostUrl] = useState<string>(DEFAULT_URLS.slskd);
  const [rpcPath, setRpcPath] = useState<string>('/transmission/rpc');
  const [username, setUsername] = useState<string>('');
  const [password, setPassword] = useState<string>('');
  const [apiKey, setApiKey] = useState<string>('');
  const [category, setCategory] = useState<string>('');
  const [isSaving, setIsSaving] = useState<boolean>(false);

  const handleTypeChange = (newType: DownloadDriverType) => {
    setType(newType);
    setHostUrl(DEFAULT_URLS[newType]);
    setCategory(DEFAULT_CATEGORIES[newType] ?? '');
  };

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setIsSaving(true);
    try {
      const extraSettings: Record<string, string> = {};
      if (type === 'transmission' && rpcPath.trim()) {
        extraSettings.rpc_path = rpcPath.trim();
      }
      if (category.trim()) {
        extraSettings.category = category.trim();
      }

      const payload: Schema<'DownloadClientPayload'> = {
        name: name.trim(),
        driver_type: type,
        host_url: hostUrl.trim(),
        enabled: true,
        priority: 1,
        username:
          (type === 'transmission' || type === 'nzbget' || type === 'qbittorrent' || type === 'slskd') &&
          username.trim()
            ? username.trim()
            : undefined,
        password:
          (type === 'deluge' ||
            type === 'transmission' ||
            type === 'nzbget' ||
            type === 'qbittorrent' ||
            type === 'slskd') &&
          password.trim()
            ? password.trim()
            : undefined,
        api_key: type === 'sabnzbd' && apiKey.trim() ? apiKey.trim() : undefined,
        category: category.trim() || undefined,
        extra_settings_json: Object.keys(extraSettings).length > 0 ? JSON.stringify(extraSettings) : undefined,
      };

      await saveClientSettings(payload);
      setName('');
      setUsername('');
      setPassword('');
      setApiKey('');
      setCategory(DEFAULT_CATEGORIES[type] ?? '');
      await reload();
      onToast('Download client added');
    } catch {
      onToast('Failed to add client', 'error');
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteClientSettings(id);
      await reload();
      onToast('Client deleted');
    } catch {
      onToast('Failed to delete client', 'error');
    }
  };

  const handleTest = async (client: DownloadClientItem) => {
    try {
      const res = await testClientConnection(client);
      onToast(res.success ? 'Connection verified!' : `Connection failed: ${res.message}`, res.success ? 'ok' : 'error');
    } catch {
      onToast('Connection test error', 'error');
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
                <TapeDeckButton size="sm" onClick={() => void handleTest(c)}>
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
        <h4 className="text-xs font-bold uppercase font-mono text-white mb-4">Add Download Client</h4>
        <form onSubmit={handleAdd} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="client-name" className={compactLabelClass}>Name</label>
              <input
                id="client-name"
                name="name"
                type="text"
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
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
                onChange={(e) => handleTypeChange(toDriverType(e.target.value))}
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
              value={hostUrl}
              onChange={(e) => setHostUrl(e.target.value)}
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
                value={rpcPath}
                onChange={(e) => setRpcPath(e.target.value)}
                placeholder="/transmission/rpc"
                className={compactInputClass}
              />
            </div>
          )}

          {type === 'sabnzbd' && (
            <div>
              <label htmlFor="client-api-key" className={compactLabelClass}>API Key</label>
              <input
                id="client-api-key"
                name="api_key"
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="SABnzbd API key"
                autoComplete="new-password"
                className={compactInputClass}
              />
            </div>
          )}

          {(type === 'slskd' || type === 'qbittorrent' || type === 'transmission' || type === 'nzbget') && (
            <div>
              <label htmlFor="client-username" className={compactLabelClass}>
                Username {type === 'nzbget' && <span className="text-[#e5a00d]">*</span>}
              </label>
              <input
                id="client-username"
                name="username"
                type="text"
                required={type === 'nzbget'}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="Username"
                autoComplete="username"
                className={compactInputClass}
              />
            </div>
          )}

          {(type === 'slskd' ||
            type === 'qbittorrent' ||
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
                value={password}
                onChange={(e) => setPassword(e.target.value)}
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
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                placeholder="e.g. trackseerr"
                className={compactInputClass}
              />
            </div>
          )}

          <ActionBar align="end" className="pt-2">
            <TapeDeckButton
              type="submit"
              size="sm"
              variant="amber"
              disabled={isSaving}
              icon={<Plus className="h-3.5 w-3.5" />}
            >
              Add Client
            </TapeDeckButton>
          </ActionBar>
        </form>
      </MachinedCard>
    </div>
  );
};
