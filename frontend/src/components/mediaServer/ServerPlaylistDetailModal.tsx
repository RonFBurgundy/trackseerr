import React, { useState } from 'react';
import { DownloadCloud } from 'lucide-react';
import type { Schema } from '@/types/apiSchema';
import type { ServerPlaylistSummary, ServerPlaylistTrack } from '@/types/mediaServer';
import { ObsidianModal, TapeDeckButton, CassetteLoader } from '@/components/ui';

export interface ServerPlaylistDetailModalProps {
  playlist: ServerPlaylistSummary | null;
  serverName: string;
  selectedUser?: string;
  items: ServerPlaylistTrack[];
  isItemsLoading: boolean;
  isMutating: boolean;
  onClose: () => void;
  onAdopt: (playlist: ServerPlaylistSummary) => Promise<Schema<'PlaylistRecord'> | null>;
}

const ADOPTED_BADGE =
  'px-2 py-0.5 rounded-[2px] border text-[10px] font-mono uppercase tracking-wide bg-[#22c55e]/10 border-[#22c55e]/40 text-[#22c55e]';

interface BodyProps extends Omit<ServerPlaylistDetailModalProps, 'playlist' | 'onClose' | 'serverName' | 'selectedUser'> {
  playlist: ServerPlaylistSummary;
}

const DetailBody: React.FC<BodyProps> = ({
  playlist,
  items,
  isItemsLoading,
  isMutating,
  onAdopt,
}) => {
  const [adoptMessage, setAdoptMessage] = useState<string | null>(null);

  const handleAdopt = async () => {
    const created = await onAdopt(playlist);
    setAdoptMessage(created ? 'Adopted into TrackSeerr.' : null);
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
    {playlist && <DetailBody key={playlist.id} playlist={playlist} {...rest} />}
  </ObsidianModal>
);
