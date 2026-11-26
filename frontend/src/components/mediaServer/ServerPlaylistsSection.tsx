import React, { useId } from 'react';
import { RefreshCw, X } from 'lucide-react';
import type { User } from '@/types/models';
import type { MediaServerKind } from '@/types/mediaServer';
import { useServerPlaylists } from '@/hooks/useServerPlaylists';
import { TapeDeckButton, MachinedCard, CassetteLoader } from '@/components/ui';
import { ServerPlaylistDetailModal } from './ServerPlaylistDetailModal';

export interface ServerPlaylistsSectionProps {
  serverId: string;
  serverName: string;
  kind: MediaServerKind;
  users: User[];
}

const ADOPTED_BADGE =
  'px-2 py-0.5 rounded-[2px] border text-[10px] font-mono uppercase tracking-wide bg-[#22c55e]/10 border-[#22c55e]/40 text-[#22c55e]';

export const ServerPlaylistsSection: React.FC<ServerPlaylistsSectionProps> = ({
  serverId,
  serverName,
  kind,
  users,
}) => {
  const state = useServerPlaylists(serverId, kind);
  const userSelectId = useId();

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-4">
        <h3 className="text-sm sm:text-base font-bold text-white uppercase tracking-wider font-mono truncate">
          {serverName}
        </h3>

        <div className="flex flex-wrap items-center gap-3">
          {kind === 'jellyfin' && (
            <select
              id={userSelectId}
              name="server-user"
              value={state.selectedUser ?? ''}
              onChange={(e) => state.setSelectedUser(e.target.value)}
              aria-label={`${serverName} user`}
              className="bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-2.5 py-1.5 min-h-[36px] sm:min-h-0 text-[13px] sm:text-sm text-white font-mono focus:outline-none focus:border-[#e5a00d]"
            >
              <option value="">Default account</option>
              {users.map((u) => (
                <option key={u.id} value={u.username}>
                  {u.username}
                </option>
              ))}
            </select>
          )}
          <TapeDeckButton
            size="sm"
            aria-label={`Refresh ${serverName} playlists`}
            onClick={() => void state.refresh()}
            disabled={state.isLoading}
            icon={<RefreshCw className={`h-3.5 w-3.5 ${state.isLoading ? 'animate-spin' : ''}`} />}
          />
        </div>
      </div>

      {state.error && (
        <div
          role="alert"
          className="flex items-start justify-between gap-3 p-3 bg-[#1a0f0f] border border-[#ef4444]/40 rounded-[3px] text-xs text-[#ef4444] font-mono"
        >
          <span>{state.error}</span>
          <button
            type="button"
            onClick={state.clearError}
            aria-label="Dismiss error"
            className="shrink-0 inline-flex h-9 w-9 -my-2 -mr-2 items-center justify-center"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {state.isLoading && state.playlists.length === 0 && (
        <div className="py-16">
          <CassetteLoader size="md" />
        </div>
      )}

      {!state.isLoading && state.playlists.length === 0 && !state.error && (
        <div className="text-center py-16 text-neutral-500 font-mono text-sm">
          No {serverName} playlists found.
        </div>
      )}

      {state.playlists.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {state.playlists.map((pl) => (
            <MachinedCard
              key={pl.id}
              interactive
              className="p-4 flex flex-col gap-3"
              role="button"
              tabIndex={0}
              onClick={() => void state.selectPlaylist(pl)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  void state.selectPlaylist(pl);
                }
              }}
            >
              {pl.adopted_playlist_id && (
                <div className="flex items-center gap-1.5">
                  <span className={ADOPTED_BADGE}>Adopted</span>
                </div>
              )}
              <h4 className="font-bold text-sm sm:text-base text-white truncate" title={pl.title}>
                {pl.title}
              </h4>
              <div className="flex items-center gap-3 text-xs text-neutral-400 font-mono">
                <span>{pl.track_count} tracks</span>
                {pl.owner && (
                  <>
                    <span>&middot;</span>
                    <span className="truncate">{pl.owner}</span>
                  </>
                )}
              </div>
            </MachinedCard>
          ))}
        </div>
      )}

      <ServerPlaylistDetailModal
        playlist={state.selected}
        serverName={serverName}
        selectedUser={kind === 'jellyfin' ? state.selectedUser : undefined}
        items={state.items}
        isItemsLoading={state.isItemsLoading}
        isMutating={state.isMutating}
        onClose={() => void state.selectPlaylist(null)}
        onAdopt={state.adopt}
      />
    </div>
  );
};
