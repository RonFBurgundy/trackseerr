import React, { useState } from 'react';
import { Loader2, Plus } from 'lucide-react';
import { createCollection } from '@/services/libraryService';
import { errorMessage } from '@/services/apiClient';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';

export interface CreateCollectionModalProps {
  isOpen: boolean;
  onClose: () => void;
  onCreated: (name: string) => void | Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

const INPUT =
  'w-full bg-[#181818] border border-[#2e2e2e] focus:border-[#e5a00d] rounded-[3px] px-3 py-2 text-sm text-white placeholder-neutral-500 outline-none font-mono';
const LABEL = 'block text-xs font-mono uppercase tracking-wider text-neutral-400 mb-1';

export const CreateCollectionModal: React.FC<CreateCollectionModalProps> = ({ isOpen, onClose, onCreated, onToast }) => {
  const [name, setName] = useState<string>('');
  const [summary, setSummary] = useState<string>('');
  const [posterUrl, setPosterUrl] = useState<string>('');
  const [busy, setBusy] = useState<boolean>(false);

  const submit = async (e?: React.FormEvent): Promise<void> => {
    e?.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    setBusy(true);
    try {
      await createCollection({
        name: trimmed,
        summary: summary.trim() || undefined,
        poster_url: posterUrl.trim() || undefined,
        monitored: true,
      });
      setName('');
      setSummary('');
      setPosterUrl('');
      onClose();
      await onCreated(trimmed);
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to create collection'), 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="Create Collection"
      subtitle="Organize albums into custom playlists, box sets, or anthologies"
      footer={
        <>
          <TapeDeckButton size="sm" onClick={onClose}>
            Cancel
          </TapeDeckButton>
          <TapeDeckButton
            size="sm"
            variant="amber"
            disabled={!name.trim() || busy}
            onClick={() => void submit()}
            icon={busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
          >
            Create Collection
          </TapeDeckButton>
        </>
      }
    >
      <form onSubmit={(e) => void submit(e)} className="space-y-4">
        <div>
          <label htmlFor="collection-name" className={LABEL}>Collection Name *</label>
          <input
            id="collection-name"
            name="collection-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. 90s Grunge Essentials"
            required
            className={INPUT}
          />
        </div>
        <div>
          <label htmlFor="collection-summary" className={LABEL}>Summary / Notes (Optional)</label>
          <textarea
            id="collection-summary"
            name="collection-summary"
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            placeholder="Brief description of this collection..."
            rows={3}
            className={`${INPUT} resize-none`}
          />
        </div>
        <div>
          <label htmlFor="collection-poster-url" className={LABEL}>Poster URL (Optional)</label>
          <input
            id="collection-poster-url"
            name="collection-poster-url"
            type="url"
            value={posterUrl}
            onChange={(e) => setPosterUrl(e.target.value)}
            placeholder="https://... (Leave blank to use 2x2 collage of album covers)"
            className={INPUT}
          />
        </div>
      </form>
    </ObsidianModal>
  );
};
