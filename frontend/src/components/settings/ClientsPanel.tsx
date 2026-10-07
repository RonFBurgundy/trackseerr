import React, { useState } from 'react';
import { Trash2, Plus } from 'lucide-react';
import { TapeDeckButton, MachinedCard, ConfirmDangerButton, ActionBar } from '@/components/ui';
import type { DownloadClientItem, DownloadDriverType } from '@/types/models';
import { saveClientSettings, deleteClientSettings, testClientConnection } from '@/services/settingsService';
import { compactInputClass, compactLabelClass } from './formClasses';

export interface ClientsPanelProps {
  clients: DownloadClientItem[];
  reload: () => Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

const DRIVER_TYPES: ReadonlyArray<DownloadDriverType> = ['slskd', 'sabnzbd', 'qbittorrent'];

function toDriverType(value: string): DownloadDriverType {
  return DRIVER_TYPES.find((t) => t === value) ?? 'slskd';
}

export const ClientsPanel: React.FC<ClientsPanelProps> = ({ clients, reload, onToast }) => {
  const [name, setName] = useState<string>('');
  const [type, setType] = useState<DownloadDriverType>('slskd');
  const [hostUrl, setHostUrl] = useState<string>('http://localhost:5030');
  const [isSaving, setIsSaving] = useState<boolean>(false);

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setIsSaving(true);
    try {
      await saveClientSettings({
        name: name.trim(),
        driver_type: type,
        host_url: hostUrl.trim(),
        enabled: true,
        priority: 1,
      });
      setName('');
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
              <input id="client-name" name="name"
                type="text"
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Local Soulseek"
                className={compactInputClass}
              />
            </div>
            <div>
              <label htmlFor="client-type" className={compactLabelClass}>Type</label>
              <select id="client-type" name="type"
                value={type}
                onChange={(e) => setType(toDriverType(e.target.value))}
                className={compactInputClass}
              >
                <option value="slskd">slskd</option>
                <option value="sabnzbd">SABnzbd</option>
                <option value="qbittorrent">qBittorrent</option>
              </select>
            </div>
          </div>
          <div>
            <label htmlFor="client-host" className={compactLabelClass}>Host URL</label>
            <input id="client-host" name="host_url" type="url" required value={hostUrl} onChange={(e) => setHostUrl(e.target.value)} placeholder="http://localhost:5030" className={compactInputClass} />
          </div>
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
