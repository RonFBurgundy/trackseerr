import React, { useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import type { CollectionItem, Playlist, User } from '@/types/models';
import { MachinedCard, TapeDeckButton } from '@/components/ui';
import { CollectionArt } from './CollectionArt';
import { CreateCollectionModal } from './CreateCollectionModal';
import { SmartCollectionsSection } from './SmartCollectionsSection';

export interface CollectionsPanelProps {
  collections: CollectionItem[];
  isAdmin: boolean;
  onOpen: (collectionId: string) => void;
  onDelete: (collectionId: string, name: string) => void;
  onCreated: (name: string) => void | Promise<void>;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
  smartCollections?: Playlist[];
  smartLoading?: boolean;
  onOpenSmartCollection?: (id: string) => void;
  onReloadSmartCollections?: () => Promise<void> | void;
  users?: User[];
  currentUserId?: string;
  canTargetUsers?: boolean;
  serverLabel?: string;
}

/** Collections are a short, user-curated list, so they render as a plain grid (no paging). */
export const CollectionsPanel: React.FC<CollectionsPanelProps> = ({
  collections,
  isAdmin,
  onOpen,
  onDelete,
  onCreated,
  onToast,
  smartCollections = [],
  smartLoading = false,
  onOpenSmartCollection,
  onReloadSmartCollections,
  users = [],
  currentUserId,
  canTargetUsers = false,
  serverLabel = 'Plex',
}) => {
  const [createOpen, setCreateOpen] = useState<boolean>(false);
  return (
    <div className="space-y-8">
      {onOpenSmartCollection && onReloadSmartCollections && (
        <SmartCollectionsSection
          collections={smartCollections}
          loading={smartLoading}
          isAdmin={isAdmin}
          onOpen={onOpenSmartCollection}
          onReload={onReloadSmartCollections}
          users={users}
          currentUserId={currentUserId}
          canTargetUsers={canTargetUsers}
          serverLabel={serverLabel}
        />
      )}

      <div className="space-y-4">
        <div className="flex items-center justify-between pb-2 border-b border-[#1f1f1f]">
          <span className="text-xs font-mono uppercase tracking-wider text-neutral-400">
            Album collections ({collections.length})
          </span>
          {isAdmin && (
            <TapeDeckButton size="sm" variant="amber" onClick={() => setCreateOpen(true)} icon={<Plus className="h-3.5 w-3.5" />}>
              New Collection
            </TapeDeckButton>
          )}
        </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
        {collections.map((col) => (
          <MachinedCard
            key={col.id}
            interactive
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === 'Enter') onOpen(col.id);
            }}
            className="p-4 flex flex-col justify-between gap-3 group"
            onClick={() => onOpen(col.id)}
          >
            <div>
              <div className="h-44 w-full rounded-[3px] bg-[#1a1a1a] border border-[#2a2a2a] overflow-hidden flex items-center justify-center mb-3">
                <CollectionArt
                  collection={col}
                  iconClass="h-12 w-12 text-neutral-600 group-hover:text-[#e5a00d] transition-colors"
                />
              </div>
              <h4 className="font-bold text-sm text-white truncate" title={col.name}>
                {col.name}
              </h4>
              {col.summary && <p className="text-xs text-neutral-400 line-clamp-2 mt-1 font-mono">{col.summary}</p>}
            </div>
            <div className="pt-2 border-t border-[#1f1f1f] flex items-center justify-between">
              <span className="text-[10px] font-mono text-[#e5a00d] bg-[#e5a00d]/10 px-2 py-0.5 rounded-[2px] border border-[#e5a00d]/20">
                {col.album_count || 0} Albums
              </span>
              {isAdmin && (
                <TapeDeckButton
                  size="sm"
                  variant="danger"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete(col.id, col.name);
                  }}
                  icon={<Trash2 className="h-3 w-3" />}
                  title="Delete Collection"
                />
              )}
            </div>
          </MachinedCard>
        ))}
      </div>

      {collections.length === 0 && (
        <div className="text-center py-12 text-neutral-500 font-mono text-sm">
          No collections found. Click &quot;New Collection&quot; to build your first playlist or box set collection.
        </div>
      )}

      <CreateCollectionModal
        isOpen={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={onCreated}
        onToast={onToast}
      />
      </div>
    </div>
  );
};
