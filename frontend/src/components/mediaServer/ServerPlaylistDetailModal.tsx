import React, { useState } from 'react';
import { Copy, DownloadCloud, Loader2 } from 'lucide-react';
import type { Schema } from '@/types/apiSchema';
import type { User } from '@/types/models';
import type { ServerPlaylistSummary, ServerPlaylistTrack } from '@/types/mediaServer';
import { ObsidianModal, TapeDeckButton, CassetteLoader } from '@/components/ui';

export interface ServerPlaylistDetailModalProps {
  playlist: ServerPlaylistSummary | null;
  serverName: string;
  selectedUser?: string;
  users: User[];
  items: ServerPlaylistTrack[];
  isItemsLoading: boolean;
  isMutating: boolean;
  onClose: () => void;
  onAdopt: (playlist: ServerPlaylistSummary) => Promise<Schema<'PlaylistRecord'> | null>;
  onSetTargets: (playlistId: string, userIds: string[]) => Promise<boolean>;
}

const ADOPTED_BADGE =
  'px-2 py-0.5 rounded-[2px] border text-[10px] font-mono uppercase tracking-wide bg-[#22c55e]/10 border-[#22c55e]/40 text-[#22c55e]';

interface BodyProps extends Omit<ServerPlaylistDetailModalProps, 'playlist' | 'onClose' | 'serverName'> {
  playlist: ServerPlaylistSummary;
}

const DetailBody: React.FC<BodyProps> = ({
  playlist,
  selectedUser,
  users,
  items,
  isItemsLoading,
  isMutating,
  onAdopt,
  onSetTargets,
}) => {
  const [adoptMessage, setAdoptMessage] = useState<string | null>(null);
  const [targetUserIds, setTargetUserIds] = useState<string[]>([]);
  const [targetsSavedMessage, setTargetsSavedMessage] = useState<string | null>(null);

  const sourceOwner = (playlist.owner || selectedUser || '').toLowerCase();
  const copyCandidates = sourceOwner
    ? users.filter((u) => u.username.toLowerCase() !== sourceOwner)
    : users;

  const toggleTarget = (userId: string) => {
    setTargetsSavedMessage(null);
    setTargetUserIds((prev) => (prev.includes(userId) ? prev.filter((id) => id !== userId) : [...prev, userId]));
  };

  const handleAdopt = async () => {
    const created = await onAdopt(playlist);
    if (created) {
      setAdoptMessage('Adopted into TrackSeerr.');
      setTargetUserIds(created.targets ?? []);
    } else {
      setAdoptMessage(null);
    }
  };

  const handleSaveTargets = async () => {
    if (!playlist.adopted_playlist_id) return;
    const saved = await onSetTargets(playlist.adopted_playlist_id, targetUserIds);
    setTargetsSavedMessage(saved ? 'Target users updated.' : null);
  };

  return (
    <div className="space-y-5">
      {/* Header: adopted badge, counts */}
      <div className="flex flex-wrap items-center gap-2">
        {playlist.adopted_playlist_id && <span className={ADOPTED_BADGE}>Adopted</span>}
        <span className="text-xs text-neutral-400 font-mono">{playlist.track_count} tracks</span>
        {playlist.owner && (
          <>
            <span className="text-xs text-neutral-600 font-mono">&middot;</span>
            <span className="text-xs text-neutral-400 font-mono">{playlist.owner}</span>
          </>
        )}
      </div>

      {/* Tracks (read-only) */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-[10px] uppercase tracking-wider text-neutral-400 font-mono">Tracks</span>
        </div>
        {isItemsLoading ? (
          <div className="flex justify-center py-8">
            <CassetteLoader size="sm" />
          </div>
        ) : items.length === 0 ? (
          <div className="text-center py-6 text-neutral-500 font-mono text-xs">No tracks.</div>
        ) : (
          <ul className="divide-y divide-[#1f1f1f] border border-[#222222] rounded-[3px] bg-[#0f0f0f]">
            {items.map((item, idx) => (
              <li key={`${idx}-${item.title}-${item.artist}`} className="flex items-center gap-2 px-3 py-2">
                <span className="w-6 text-right text-[10px] font-mono text-neutral-600">{idx + 1}</span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm text-white truncate">{item.title}</p>
                  <p className="text-xs text-neutral-400 truncate">
                    {item.artist}
                    {item.album ? ` \u00b7 ${item.album}` : ''}
                  </p>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Adopt */}
      {!playlist.adopted_playlist_id && (
        <div className="space-y-2 pt-3 border-t border-[#1f1f1f]">
          <TapeDeckButton
            size="sm"
            disabled={isMutating}
            onClick={() => void handleAdopt()}
            icon={<DownloadCloud className="h-3.5 w-3.5" />}
          >
            Adopt into TrackSeerr
          </TapeDeckButton>
          <p className="text-[11px] text-neutral-500 font-mono">
            Track this playlist in TrackSeerr as a source to sync to other users.
          </p>
        </div>
      )}
      {adoptMessage && playlist.adopted_playlist_id && (
        <p className="text-xs text-[#22c55e] font-mono">{adoptMessage}</p>
      )}

      {/* Copy to users (after adopt) */}
      {playlist.adopted_playlist_id && (
        <div className="space-y-2 pt-3 border-t border-[#1f1f1f]">
          <span className="text-[10px] uppercase tracking-wider text-neutral-400 font-mono">Copy to users</span>
          {copyCandidates.length === 0 ? (
            <p className="text-[11px] text-neutral-500 font-mono">No other users available.</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {copyCandidates.map((u) => {
                const on = targetUserIds.includes(u.id);
                return (
                  <button
                    key={u.id}
                    type="button"
                    aria-pressed={on}
                    onClick={() => toggleTarget(u.id)}
                    className={`px-2.5 py-2 min-h-[36px] sm:min-h-0 sm:py-1 rounded-[3px] text-xs font-mono transition-all ${
                      on
                        ? 'bg-[#e5a00d]/20 border border-[#e5a00d] text-[#e5a00d]'
                        : 'bg-[#121212] border border-[#222222] text-neutral-500 hover:text-neutral-300'
                    }`}
                  >
                    {u.username}
                  </button>
                );
              })}
            </div>
          )}
          <TapeDeckButton
            size="sm"
            variant="amber"
            disabled={isMutating || targetUserIds.length === 0}
            onClick={() => void handleSaveTargets()}
            icon={
              isMutating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Copy className="h-3.5 w-3.5" />
            }
          >
            Copy to users
          </TapeDeckButton>
          {targetsSavedMessage && (
            <p className="text-xs text-[#22c55e] font-mono">{targetsSavedMessage}</p>
          )}
        </div>
      )}
    </div>
  );
};

export const ServerPlaylistDetailModal: React.FC<ServerPlaylistDetailModalProps> = ({
  playlist,
  serverName,
  selectedUser,
  onClose,
  ...rest
}) => (
  <ObsidianModal
    isOpen={playlist !== null}
    onClose={onClose}
    title={playlist?.title ?? 'Playlist'}
    subtitle={playlist ? `${serverName}${selectedUser ? ` \u00b7 ${selectedUser}` : ''}` : undefined}
  >
    {playlist && <DetailBody key={playlist.id} playlist={playlist} selectedUser={selectedUser} {...rest} />}
  </ObsidianModal>
);
