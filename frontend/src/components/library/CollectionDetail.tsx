import React from 'react';
import { Disc, Loader2, Trash2 } from 'lucide-react';
import type { CollectionItem } from '@/types/models';
import { useCollectionDetail } from '@/hooks/useCollectionDetail';
import { MachinedCard, TapeDeckButton } from '@/components/ui';
import { DetailHeaderBar, PageFrame } from '@/components/layout';

import { CollectionArt } from './CollectionArt';

export interface CollectionDetailProps {
  collectionId: string;
  /** The list entry, shown in the header while the detail loads. */
  fallback: CollectionItem | undefined;
  isAdmin: boolean;
  onBack: () => void;
  onDelete: (collectionId: string, name: string) => void;
  onChanged: () => void | Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

export const CollectionDetail: React.FC<CollectionDetailProps> = ({
  collectionId,
  fallback,
  isAdmin,
  onBack,
  onDelete,
  onChanged,
  onToast,
}) => {
  const { collection, loading, removeAlbum } = useCollectionDetail(collectionId, onToast, onChanged);
  const col = collection ?? fallback;

  const deleteAction =
    isAdmin && col ? (
      <TapeDeckButton
        size="sm"
        variant="danger"
        onClick={() => onDelete(col.id, col.name)}
        icon={<Trash2 className="h-3.5 w-3.5" />}
        collapseLabel="sm"
        aria-label="Delete collection"
        title="Delete collection"
      >
        Delete
      </TapeDeckButton>
    ) : undefined;

  return (
    <PageFrame nav={<DetailHeaderBar parentLabel="Collections" title={col?.name} onBack={onBack} actions={deleteAction} />}>
    <div className="space-y-6">
      <MachinedCard className="p-3 sm:p-6">
        <div className="flex flex-col sm:flex-row items-center sm:items-start gap-6">
          <div className="h-32 w-32 rounded-[4px] bg-[#1a1a1a] border border-[#2a2a2a] overflow-hidden flex-shrink-0 flex items-center justify-center shadow-xl">
            {col && <CollectionArt collection={col} iconClass="h-16 w-16 text-neutral-600" gridBg="bg-[#1f1f1f]" />}
          </div>
          <div className="flex-1 text-center sm:text-left space-y-2 min-w-0">
            <span className="text-[10px] font-mono uppercase tracking-widest text-[#e5a00d]">Collection Drilldown</span>
            <h2 className="text-2xl sm:text-3xl font-black text-white font-mono tracking-tight truncate">{col?.name}</h2>
            {col?.summary && <p className="text-xs text-neutral-300 font-mono">{col.summary}</p>}
            <div className="flex items-center justify-center sm:justify-start gap-3 pt-1">
              <span className="px-2.5 py-0.5 rounded-[2px] text-xs font-mono font-bold bg-[#e5a00d]/10 text-[#e5a00d] border border-[#e5a00d]/30">
                {col?.album_count ?? col?.albums?.length ?? 0} Albums
              </span>
            </div>
          </div>
        </div>
      </MachinedCard>

      {loading ? (
        <div className="flex flex-col items-center justify-center py-20 gap-3">
          <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
          <span className="text-xs uppercase tracking-widest text-neutral-400 font-mono">Loading Collection Albums...</span>
        </div>
      ) : (
        <div className="space-y-3">
          <h3 className="text-sm font-mono uppercase tracking-wider text-neutral-400">
            Included Albums ({col?.albums?.length || 0})
          </h3>
          {col?.albums && col.albums.length > 0 ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
              {col.albums.map((alb) => (
                <MachinedCard key={alb.id} className="p-3 sm:p-4 flex flex-col justify-between gap-3 group">
                  <div className="flex items-center gap-3 min-w-0">
                    <div className="h-12 w-12 rounded-[3px] bg-[#1a1a1a] border border-[#2a2a2a] overflow-hidden flex-shrink-0 flex items-center justify-center">
                      {alb.cover_url ? (
                        <img src={alb.cover_url} alt={alb.title ?? ''} className="w-full h-full object-cover" />
                      ) : (
                        <Disc className="h-6 w-6 text-neutral-600" />
                      )}
                    </div>
                    <div className="min-w-0 flex-1">
                      <h4 className="font-bold text-sm text-white truncate" title={alb.title ?? undefined}>
                        {alb.title}
                      </h4>
                      <p className="text-xs text-neutral-400 truncate mt-0.5">
                        {alb.artist_name || 'Unknown Artist'} {alb.release_date ? `(${alb.release_date.slice(0, 4)})` : ''}
                      </p>
                    </div>
                  </div>
                  <div className="pt-2 border-t border-[#1f1f1f] flex items-center justify-end">
                    <TapeDeckButton
                      size="sm"
                      variant="danger"
                      onClick={() => void removeAlbum(alb.id)}
                      icon={<Trash2 className="h-3.5 w-3.5" />}
                      title="Remove from collection"
                    >
                      Remove
                    </TapeDeckButton>
                  </div>
                </MachinedCard>
              ))}
            </div>
          ) : (
            <div className="text-center py-12 text-neutral-500 font-mono text-sm border border-dashed border-[#222] rounded-[4px]">
              No albums in this collection yet. Drill down into an artist to add albums.
            </div>
          )}
        </div>
      )}
    </div>
    </PageFrame>
  );
};
