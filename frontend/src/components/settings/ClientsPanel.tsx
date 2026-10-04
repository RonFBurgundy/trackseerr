import React, { useState } from 'react';
import { Trash2, Plus } from 'lucide-react';
import { TapeDeckButton, MachinedCard, ConfirmDangerButton } from '@/components/ui';
import type { DownloadClientItem } from '@/types/models';
import { saveClientSettings, deleteClientSettings, testClientConnection } from '@/services/settingsService';
import { compactInputClass, compactLabelClass } from './formClasses';

export interface ClientsPanelProps {
  clients: DownloadClientItem[];
  reload: () => Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

export const ClientsPanel: React.FC<ClientsPanelProps> = ({ clients, reload, onToast }) => {
  const [name, setName] = useState<string>('');
  const [type, setType] = useState<DownloadClientItem['client_type']>('slskd');
  const [host, setHost] = useState<string>('localhost');
  const [port, setPort] = useState<number>(5030);
  const [isSaving, setIsSaving] = useState<boolean>(false);

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setIsSaving(true);
    try {
      await saveClientSettings({
        name: name.trim(),
        client_type: type,
        host,
        port,
        use_ssl: false,
        is_enabled: true,
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

  const handleDelete = async (id: number) => {
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
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {clients.map((c) => (
            <MachinedCard key={c.id} className="p-4 flex items-center justify-between gap-3">
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-bold text-sm text-white">{c.name}</span>
                  <span className="px-1.5 py-0.5 rounded-[2px] bg-[#1a1a1a] text-[10px] font-mono uppercase text-[#e5a00d]">
                    {c.client_type}
                  </span>
                </div>
                <p className="text-xs text-neutral-400 font-mono mt-1">
                  {c.host}:{c.port}
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

      <MachinedCard className="p-5 max-w-xl">
        <h4 className="text-xs font-bold uppercase font-mono text-white mb-4">Add Download Client</h4>
        <form onSubmit={handleAdd} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={compactLabelClass}>Name</label>
              <input
                type="text"
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Local Soulseek"
                className={compactInputClass}
              />
            </div>
            <div>
              <label className={compactLabelClass}>Type</label>
              <select
                value={type}
                onChange={(e) => setType(e.target.value as DownloadClientItem['client_type'])}
                className={compactInputClass}
              >
                <option value="slskd">slskd</option>
                <option value="sabnzbd">SABnzbd</option>
                <option value="qbittorrent">qBittorrent</option>
              </select>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={compactLabelClass}>Host</label>
              <input type="text" required value={host} onChange={(e) => setHost(e.target.value)} className={compactInputClass} />
            </div>
            <div>
              <label className={compactLabelClass}>Port</label>
              <input
                type="number"
                required
                value={port}
                onChange={(e) => setPort(Number(e.target.value))}
                className={compactInputClass}
              />
            </div>
          </div>
          <div className="flex justify-end pt-2">
            <TapeDeckButton
              type="submit"
              size="sm"
              variant="amber"
              disabled={isSaving}
              icon={<Plus className="h-3.5 w-3.5" />}
            >
              Add Client
            </TapeDeckButton>
          </div>
        </form>
      </MachinedCard>
    </div>
  );
};
