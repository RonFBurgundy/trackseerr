import React, { useEffect, useState } from 'react';
import { RefreshCw, X } from 'lucide-react';
import { usePlexPlaylists } from '@/hooks/usePlexPlaylists';
import { TabStrip, TapeDeckButton, MachinedCard, TactileSwitch, CassetteLoader } from '@/components/ui';
import { PlexKindBadge, PlexOwnerBadge } from './PlexBadges';
import { PlexPlaylistDetailModal } from './PlexPlaylistDetailModal';
import { PlexMixesPanel } from './PlexMixesPanel';

export interface PlexPlaylistsSectionProps {
  isAdmin: boolean;
}

type PlexSubTab = 'playlists' | 'mixes';

export const PlexPlaylistsSection: React.FC<PlexPlaylistsSectionProps> = ({ isAdmin }) => {
  const plex = usePlexPlaylists();
  const [subTab, setSubTab] = useState<PlexSubTab>('playlists');
  const { loadMixes, selectedUser } = plex;

  useEffect(() => {
    if (subTab === 'mixes' && selectedUser !== undefined) void loadMixes();
  }, [subTab, selectedUser, loadMixes]);

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-4">
        <TabStrip fill>
          <TapeDeckButton size="sm" active={subTab === 'playlists'} onClick={() => setSubTab('playlists')}>
            Playlists
          </TapeDeckButton>
          <TapeDeckButton size="sm" active={subTab === 'mixes'} onClick={() => setSubTab('mixes')}>
            Mixes
          </TapeDeckButton>
        </TabStrip>

        <div className="flex flex-wrap items-center gap-3">
          {plex.users.length > 1 && (
            <select
              id="plex-user"
              name="plex-user"
              value={plex.selectedUser ?? ''}
              onChange={(e) => plex.setSelectedUser(e.target.value)}
              aria-label="Plex user"
              className="bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-2.5 py-1.5 min-h-[36px] sm:min-h-0 text-[13px] sm:text-sm text-white font-mono focus:outline-none focus:border-[#e5a00d]"
            >
              {plex.users.map((u) => (
                <option key={u.username} value={u.username}>
                  {u.username}
                  {u.is_self ? ' (you)' : ''}
                </option>
              ))}
            </select>
          )}
          {subTab === 'playlists' && (
            <>
              <TactileSwitch checked={plex.includeIgnored} onChange={plex.setIncludeIgnored} label="Show ignored" />
              <TapeDeckButton
                size="sm"
                aria-label="Refresh Plex playlists"
                onClick={() => void plex.refresh()}
                disabled={plex.isLoading}
                icon={<RefreshCw className={`h-3.5 w-3.5 ${plex.isLoading ? 'animate-spin' : ''}`} />}
              />
            </>
          )}
        </div>
      </div>

      {plex.error && (
        <div
          role="alert"
          className="flex items-start justify-between gap-3 p-3 bg-[#1a0f0f] border border-[#ef4444]/40 rounded-[3px] text-xs text-[#ef4444] font-mono"
        >
          <span>{plex.error}</span>
          <button type="button" onClick={plex.clearError} aria-label="Dismiss error" className="shrink-0 inline-flex h-9 w-9 -my-2 -mr-2 items-center justify-center">
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {subTab === 'playlists' && (
        <>
          {plex.isLoading && plex.playlists.length === 0 && (
            <div className="py-16">
              <CassetteLoader size="md" />
            </div>
          )}

          {!plex.isLoading && plex.playlists.length === 0 && !plex.error && (
            <div className="text-center py-16 text-neutral-500 font-mono text-sm">No Plex playlists found.</div>
          )}

          {plex.playlists.length > 0 && (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {plex.playlists.map((pl) => (
                <MachinedCard
                  key={pl.rating_key}
                  interactive
                  className={`p-4 flex flex-col gap-3 ${pl.ignored ? 'opacity-60' : ''}`}
                  role="button"
                  tabIndex={0}
                  onClick={() => void plex.selectPlaylist(pl)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      void plex.selectPlaylist(pl);
                    }
                  }}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <PlexKindBadge kind={pl.kind} />
                      <PlexOwnerBadge owner={pl.owner} />
                    </div>
                    {/* Stop propagation so toggling does not open the modal */}
                    <div onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                      <TactileSwitch
                        checked={pl.ignored}
                        onChange={() => void plex.toggleIgnored(pl)}
                        title={pl.ignored ? 'Ignored: click to include' : 'Click to ignore'}
                        label="Ignore"
                      />
                    </div>
                  </div>
                  <h4 className="font-bold text-sm sm:text-base text-white truncate" title={pl.title}>
                    {pl.title}
                  </h4>
                  <div className="flex items-center gap-3 text-xs text-neutral-400 font-mono">
                    <span>{pl.track_count} tracks</span>
                    {pl.updated_at && (
                      <>
                        <span>&middot;</span>
                        <span>{new Date(pl.updated_at).toLocaleDateString()}</span>
                      </>
                    )}
                  </div>
                </MachinedCard>
              ))}
            </div>
          )}
        </>
      )}

      {subTab === 'mixes' && (
        <PlexMixesPanel
          mixes={plex.mixes}
          snapshots={plex.snapshots}
          isLoading={plex.isMixesLoading}
          isMutating={plex.isMutating}
          onSave={plex.saveMix}
          onToggleRefresh={plex.toggleSnapshotRefresh}
          onRemove={plex.removeSnapshot}
        />
      )}

      <PlexPlaylistDetailModal
        playlist={plex.selected}
        items={plex.items}
        isItemsLoading={plex.isItemsLoading}
        isMutating={plex.isMutating}
        isAdmin={isAdmin}
        users={plex.users}
        selectedUser={plex.selectedUser}
        copyResults={plex.copyResults}
        onClose={() => void plex.selectPlaylist(null)}
        onRename={plex.renamePlaylist}
        onDelete={plex.deletePlaylist}
        onRemoveItem={plex.removeItem}
        onMoveItem={plex.moveItem}
        onCopy={plex.copyToUsers}
        onAdopt={plex.adopt}
        onSetOwner={plex.setOwner}
      />
    </div>
  );
};
