import React, { useState } from 'react';
import { Loader2, Plus } from 'lucide-react';
import { useTailoredMixes } from '@/hooks/useTailoredMixes';
import { TapeDeckButton, ActionBar } from '@/components/ui';
import { MixCard } from './MixCard';
import { NewMixForm } from './NewMixForm';

export interface TailoredMixesSectionProps {
  isAdmin: boolean;
}

export const TailoredMixesSection: React.FC<TailoredMixesSectionProps> = ({ isAdmin }) => {
  const m = useTailoredMixes(isAdmin);
  const [showNew, setShowNew] = useState<boolean>(false);

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-4">
        <ActionBar bay className="p-1.5">
          <TapeDeckButton size="sm" variant="amber" onClick={() => setShowNew(true)} icon={<Plus className="h-3.5 w-3.5" />}>
            New Mix
          </TapeDeckButton>
        </ActionBar>
        {isAdmin && (
          <select
            aria-label="Select user"
            value={m.selectedUserId ?? ''}
            onChange={(e) => m.setSelectedUserId(e.target.value || undefined)}
            className="bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 min-h-[44px] sm:min-h-0 text-sm text-white focus:outline-none focus:border-[#e5a00d]"
          >
            <option value="">My mixes</option>
            {m.users.map((u) => (
              <option key={u.user_id} value={u.user_id}>
                {u.username}
              </option>
            ))}
          </select>
        )}
      </div>

      {m.error && (
        <div
          role="alert"
          className="flex items-center gap-2 bg-[#161616] border border-[#ef4444] rounded-[4px] px-4 py-2.5 text-xs font-mono text-[#ef4444]"
        >
          <span className="flex-1">{m.error}</span>
          <button type="button" onClick={m.clearError} className="min-h-[44px] sm:min-h-0 px-2 uppercase">
            Dismiss
          </button>
        </div>
      )}

      {showNew && <NewMixForm onCreate={m.create} onCancel={() => setShowNew(false)} />}

      {m.isLoading && (
        <div className="flex flex-col items-center justify-center py-16 gap-3">
          <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
          <span className="text-xs uppercase tracking-widest text-neutral-400 font-mono">Loading Mixes...</span>
        </div>
      )}

      {!m.isLoading && m.mixes.length === 0 && (
        <div className="text-center py-16 text-neutral-500 font-mono text-sm">
          No tailored mixes yet. Click &apos;New Mix&apos; to create one.
        </div>
      )}

      {!m.isLoading && m.mixes.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {m.mixes.map((mix) => (
            <MixCard
              key={mix.id}
              mix={mix}
              result={m.results[mix.id]}
              preview={m.previews[mix.id]}
              isBusy={m.busyIds.has(mix.id)}
              isGenerating={m.generatingIds.has(mix.id)}
              onUpdate={m.update}
              onDelete={m.remove}
              onPreview={m.preview}
              onGenerate={m.generate}
            />
          ))}
        </div>
      )}
    </div>
  );
};
