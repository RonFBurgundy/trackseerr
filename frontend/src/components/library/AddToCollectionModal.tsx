import React from 'react';
import { Layers, Loader2 } from 'lucide-react';
import type { UseAddToCollectionReturn } from '@/hooks/useAddToCollection';
import { ObsidianModal } from '@/components/ui';

export interface AddToCollectionModalProps {
  picker: UseAddToCollectionReturn;
}

export const AddToCollectionModal: React.FC<AddToCollectionModalProps> = ({ picker }) => (
  <ObsidianModal
    isOpen={picker.album !== null}
    onClose={picker.close}
    title="Add to Collection"
    subtitle={`Select a collection to add "${picker.album?.title ?? ''}"`}
  >
    <div className="space-y-3">
      {picker.loading ? (
        <div className="flex justify-center py-8">
          <Loader2 className="h-6 w-6 text-[#e5a00d] animate-spin" />
        </div>
      ) : picker.collections.length > 0 ? (
        <div className="divide-y divide-[#222] border border-[#262626] rounded-[4px] max-h-80 overflow-y-auto bg-[#141414]">
          {picker.collections.map((col) => (
            <button
              type="button"
              key={col.id}
              onClick={() => void picker.add(col.id, col.name)}
              className="w-full text-left p-3 min-h-[44px] flex items-center justify-between gap-3 hover:bg-[#1c1c1c] transition-colors"
            >
              <div className="flex items-center gap-2.5 min-w-0">
                <Layers className="h-4 w-4 text-[#e5a00d] flex-shrink-0" />
                <div className="min-w-0">
                  <h4 className="text-sm font-bold text-white truncate">{col.name}</h4>
                  {col.summary && <p className="text-xs text-neutral-400 truncate">{col.summary}</p>}
                </div>
              </div>
              <span className="text-[10px] font-mono text-neutral-400 bg-neutral-800 px-2 py-0.5 rounded-[2px] flex-shrink-0">
                {col.album_count || 0} Albums
              </span>
            </button>
          ))}
        </div>
      ) : (
        <div className="text-center py-8 text-neutral-500 font-mono text-xs">
          No collections available. Create one in the Collections tab first.
        </div>
      )}
    </div>
  </ObsidianModal>
);
