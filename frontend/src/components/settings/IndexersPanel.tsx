import React, { useState } from 'react';
import { Trash2, Plus } from 'lucide-react';
import { TapeDeckButton, MachinedCard, ConfirmDangerButton } from '@/components/ui';
import type { IndexerItem } from '@/types/models';
import { saveIndexer, deleteIndexer, testIndexer } from '@/services/settingsService';
import { compactInputClass, compactLabelClass } from './formClasses';

export interface IndexersPanelProps {
  indexers: IndexerItem[];
  reload: () => Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

export const IndexersPanel: React.FC<IndexersPanelProps> = ({ indexers, reload, onToast }) => {
  const [name, setName] = useState<string>('');
  const [url, setUrl] = useState<string>('');
  const [apiKey, setApiKey] = useState<string>('');
  const [isSaving, setIsSaving] = useState<boolean>(false);

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !url.trim()) return;
    setIsSaving(true);
    try {
      await saveIndexer({
        name: name.trim(),
        url: url.trim(),
        api_key: apiKey.trim(),
        indexer_type: 'torznab',
        is_enabled: true,
        priority: 1,
      });
      setName('');
      setUrl('');
      setApiKey('');
      await reload();
      onToast('Indexer registered');
    } catch {
      onToast('Failed to save indexer', 'error');
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await deleteIndexer(id);
      await reload();
      onToast('Indexer removed');
    } catch {
      onToast('Failed to delete indexer', 'error');
    }
  };

  const handleTest = async (indexer: IndexerItem) => {
    try {
      const res = await testIndexer(indexer);
      onToast(res.success ? 'Indexer responded OK' : `Indexer error: ${res.message}`, res.success ? 'ok' : 'error');
    } catch {
      onToast('Test indexer failed', 'error');
    }
  };

  return (
    <div className="space-y-6">
      {indexers.length === 0 ? (
        <p className="text-xs font-mono text-neutral-500 py-4">No indexers registered yet.</p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {indexers.map((idx) => (
            <MachinedCard key={idx.id} className="p-4 flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-bold text-sm text-white">{idx.name}</span>
                  <span className="px-1.5 py-0.5 rounded-[2px] bg-[#1a1a1a] text-[10px] font-mono uppercase text-[#e5a00d]">
                    {idx.indexer_type}
                  </span>
                </div>
                <p className="text-xs text-neutral-400 font-mono mt-1 truncate max-w-xs">{idx.url}</p>
              </div>
              <div className="flex items-center gap-2">
                <TapeDeckButton size="sm" onClick={() => void handleTest(idx)}>
                  Test
                </TapeDeckButton>
                <ConfirmDangerButton
                  onConfirm={() => void handleDelete(idx.id)}
                  icon={<Trash2 className="h-3 w-3" />}
                  ariaLabel="Delete indexer"
                  confirmLabel="Confirm Delete"
                />
              </div>
            </MachinedCard>
          ))}
        </div>
      )}

      <MachinedCard className="p-5 max-w-xl">
        <h4 className="text-xs font-bold uppercase font-mono text-white mb-4">Add New Indexer (Torznab / Newznab)</h4>
        <form onSubmit={handleAdd} className="space-y-4">
          <div>
            <label className={compactLabelClass}>Name</label>
            <input
              type="text"
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Redacted Torznab"
              className={compactInputClass}
            />
          </div>
          <div>
            <label className={compactLabelClass}>URL</label>
            <input
              type="url"
              required
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="http://prowlarr:9696/1/api"
              className={compactInputClass}
            />
          </div>
          <div>
            <label className={compactLabelClass}>API Key</label>
            <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} className={compactInputClass} />
          </div>
          <div className="flex justify-end pt-2">
            <TapeDeckButton
              type="submit"
              size="sm"
              variant="amber"
              disabled={isSaving}
              icon={<Plus className="h-3.5 w-3.5" />}
            >
              Save Indexer
            </TapeDeckButton>
          </div>
        </form>
      </MachinedCard>
    </div>
  );
};
