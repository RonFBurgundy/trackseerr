import React, { useState } from 'react';
import { Trash2, Plus, Save, X } from 'lucide-react';
import { TapeDeckButton, MachinedCard, ConfirmDangerButton, ActionBar } from '@/components/ui';
import type { IndexerItem, MediaManagementSettings } from '@/types/models';
import { useIndexerDraft, seedingDraftToPayload } from '@/hooks/useIndexerDraft';
import { IndexerSeedingFields } from './IndexerSeedingFields';
import { saveIndexer, deleteIndexer, testIndexer } from '@/services/settingsService';
import { compactInputClass, compactLabelClass } from './formClasses';

export interface IndexersPanelProps {
  indexers: IndexerItem[];
  /** Media management settings, used for the global seed limits shown as placeholders. */
  media?: MediaManagementSettings | null;
  reload: () => Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

export const IndexersPanel: React.FC<IndexersPanelProps> = ({ indexers, media, reload, onToast }) => {
  const draft = useIndexerDraft();
  const { editingId, name, url, apiKey, indexerType, seeding } = draft;
  const editing = indexers.find((i) => i.id === editingId) ?? null;
  const showSeeding = indexerType === 'torznab';
  const [isSaving, setIsSaving] = useState<boolean>(false);

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !url.trim()) return;
    const seedPayload = showSeeding ? seedingDraftToPayload(seeding) : {};
    if (seedPayload === null) {
      onToast('Seeding values must be non-negative numbers (whole numbers for minutes and seeders)', 'error');
      return;
    }
    setIsSaving(true);
    try {
      await saveIndexer({
        ...(editing ? { id: editing.id } : {}),
        name: name.trim(),
        host_url: url.trim(),
        api_key: apiKey.trim(),
        indexer_type: indexerType,
        categories: editing ? editing.categories : undefined,
        enabled: editing ? editing.enabled : true,
        priority: editing ? editing.priority : 1,
        ...seedPayload,
      });
      const wasEditing = editing !== null;
      draft.reset();
      await reload();
      onToast(wasEditing ? 'Indexer updated' : 'Indexer registered');
    } catch {
      onToast('Failed to save indexer', 'error');
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
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
      const res = await testIndexer({
        id: indexer.id,
        indexer_type: indexer.indexer_type,
        host_url: indexer.host_url,
        api_key: indexer.api_key,
        categories: indexer.categories,
      });
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
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 content-start">
          {indexers.map((idx) => (
            <MachinedCard key={idx.id} className="p-3 sm:p-4 flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-bold text-sm text-white">{idx.name}</span>
                  <span className="px-1.5 py-0.5 rounded-[2px] bg-[#1a1a1a] text-[10px] font-mono uppercase text-[#e5a00d]">
                    {idx.indexer_type}
                  </span>
                </div>
                <p className="text-xs text-neutral-400 font-mono mt-1 truncate max-w-xs">{idx.host_url}</p>
              </div>
              <div className="flex items-center gap-2">
                <TapeDeckButton size="sm" onClick={() => draft.startEdit(idx)}>
                  Edit
                </TapeDeckButton>
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

      <MachinedCard className="p-3 sm:p-5 max-w-xl">
        <h4 className="text-xs font-bold uppercase font-mono text-white mb-4">{editing ? `Edit Indexer (${editing.name})` : 'Add New Indexer (Torznab / Newznab)'}</h4>
        <form onSubmit={handleAdd} className="space-y-4">
          <div>
            <label htmlFor="indexer-name" className={compactLabelClass}>Name</label>
            <input id="indexer-name" name="name"
              type="text"
              required
              value={name}
              onChange={(e) => draft.setName(e.target.value)}
              placeholder="e.g. Redacted Torznab"
              className={compactInputClass}
            />
          </div>
          <div>
            <label htmlFor="indexer-url" className={compactLabelClass}>URL</label>
            <input id="indexer-url" name="url"
              type="url"
              required
              value={url}
              onChange={(e) => draft.setUrl(e.target.value)}
              placeholder="http://prowlarr:9696/1/api"
              className={compactInputClass}
            />
          </div>
          <div>
            <label htmlFor="indexer-api-key" className={compactLabelClass}>API Key</label>
            <input id="indexer-api-key" name="api-key" type="password" autoComplete="off" value={apiKey} onChange={(e) => draft.setApiKey(e.target.value)} className={compactInputClass} />
          </div>
          {showSeeding && (
            <IndexerSeedingFields
              value={seeding}
              onChange={draft.setSeedingField}
              globalSeedRatio={media?.seed_ratio_limit}
              globalSeedTimeMinutes={media?.seed_time_limit_minutes}
            />
          )}
          <ActionBar align="end" className="pt-2">
            {editing && (
              <TapeDeckButton type="button" size="sm" onClick={draft.reset} icon={<X className="h-3.5 w-3.5" />}>
                Cancel
              </TapeDeckButton>
            )}
            <TapeDeckButton
              type="submit"
              size="sm"
              variant="amber"
              disabled={isSaving}
              icon={editing ? <Save className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
            >
              Save Indexer
            </TapeDeckButton>
          </ActionBar>
        </form>
      </MachinedCard>
    </div>
  );
};
