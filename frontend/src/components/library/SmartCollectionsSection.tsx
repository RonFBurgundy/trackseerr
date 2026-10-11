import React, { useState } from 'react';
import { Plus, Sparkles } from 'lucide-react';
import type { Playlist, User } from '@/types/models';
import { MachinedCard, TapeDeckButton, CassetteLoader } from '@/components/ui';
import { parseTrackCount } from '@/lib/trackCount';
import { SmartCollectionModal } from '@/components/playlists/SmartCollectionModal';

export interface SmartCollectionsSectionProps {
  collections: Playlist[];
  loading?: boolean;
  isAdmin: boolean;
  onOpen: (collectionId: string) => void;
  onReload: () => Promise<void> | void;
  users?: User[];
  currentUserId?: string;
  canTargetUsers?: boolean;
  serverLabel?: string;
}

export const SmartCollectionsSection: React.FC<SmartCollectionsSectionProps> = ({
  collections,
  loading = false,
  isAdmin,
  onOpen,
  onReload,
  users = [],
  currentUserId,
  canTargetUsers = false,
  serverLabel = 'Plex',
}) => {
  const [modalOpen, setModalOpen] = useState<boolean>(false);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between pb-2 border-b border-[#1f1f1f]">
        <span className="text-xs font-mono uppercase tracking-wider text-neutral-400">
          Smart collections ({collections.length})
        </span>
        {isAdmin && (
          <TapeDeckButton
            size="sm"
            variant="amber"
            onClick={() => setModalOpen(true)}
            icon={<Plus className="h-3.5 w-3.5" />}
          >
            New
          </TapeDeckButton>
        )}
      </div>

      {loading ? (
        <div className="py-12">
          <CassetteLoader size="sm" />
        </div>
      ) : collections.length === 0 ? (
        <div className="text-center py-8 text-neutral-500 font-mono text-xs sm:text-sm">
          Build a collection from library filters &mdash; genre, era, country, band size and more.
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
          {collections.map((col) => {
            const trackCount = parseTrackCount(col.tracks_json);
            return (
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
                    <Sparkles className="h-12 w-12 text-neutral-600 group-hover:text-[#e5a00d] transition-colors" />
                  </div>
                  <h4 className="font-bold text-sm text-white truncate" title={col.name}>
                    {col.name}
                  </h4>
                  {col.description ? (
                    <p className="text-xs text-neutral-400 line-clamp-2 mt-1 font-mono">
                      {col.description}
                    </p>
                  ) : null}
                </div>

                <div className="pt-2 border-t border-[#1f1f1f] space-y-1.5 text-xs font-mono">
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-[2px] bg-[#1a1a1a] border border-[#2a2a2a] text-neutral-300">
                      {col.enabled ? 'Auto-update' : 'One-time'}
                    </span>
                    {trackCount !== null && (
                      <span className="text-[10px] text-[#e5a00d] bg-[#e5a00d]/10 px-2 py-0.5 rounded-[2px] border border-[#e5a00d]/20">
                        {trackCount} tracks
                      </span>
                    )}
                  </div>
                  {col.last_synced_at && (
                    <div className="text-[11px] text-neutral-500 truncate" title={col.last_synced_at}>
                      Last synced: {new Date(col.last_synced_at).toLocaleString()}
                    </div>
                  )}
                </div>
              </MachinedCard>
            );
          })}
        </div>
      )}

      {isAdmin && (
        <SmartCollectionModal
          open={modalOpen}
          onClose={() => setModalOpen(false)}
          onSaved={() => {
            void onReload();
          }}
          users={users}
          currentUserId={currentUserId}
          canTargetUsers={canTargetUsers}
          serverLabel={serverLabel}
        />
      )}
    </div>
  );
};
