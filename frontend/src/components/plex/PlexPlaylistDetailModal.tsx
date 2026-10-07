import React, { useState } from 'react';
import { ArrowUp, ArrowDown, Trash2, Pencil, Copy, DownloadCloud, Loader2, Check, X } from 'lucide-react';
import type {
  AdoptedPlexPlaylist,
  PlexCopyResult,
  PlexPlaylistItem,
  PlexPlaylistSummary,
  PlexUserOption,
} from '@/types/models';
import type { MoveDirection } from '@/hooks/usePlexPlaylists';
import { ObsidianModal, TapeDeckButton, TactileSwitch, CassetteLoader } from '@/components/ui';
import { PlexKindBadge, PlexOwnerBadge } from './PlexBadges';

export interface PlexPlaylistDetailModalProps {
  playlist: PlexPlaylistSummary | null;
  items: PlexPlaylistItem[];
  isItemsLoading: boolean;
  isMutating: boolean;
  isAdmin: boolean;
  users: PlexUserOption[];
  selectedUser: string | undefined;
  copyResults: PlexCopyResult[] | null;
  onClose: () => void;
  onRename: (playlist: PlexPlaylistSummary, title: string) => Promise<boolean>;
  onDelete: (playlist: PlexPlaylistSummary) => Promise<boolean>;
  onRemoveItem: (item: PlexPlaylistItem) => Promise<void>;
  onMoveItem: (item: PlexPlaylistItem, direction: MoveDirection) => Promise<void>;
  onCopy: (playlist: PlexPlaylistSummary, targets: string[]) => Promise<PlexCopyResult[]>;
  onAdopt: (playlist: PlexPlaylistSummary) => Promise<AdoptedPlexPlaylist | null>;
  onSetOwner: (playlist: PlexPlaylistSummary, owner: 'user' | 'trackseerr') => Promise<void>;
}

const SMART_HINT = 'Smart playlists are read-only. Copy it to a regular playlist to edit tracks.';

function formatDuration(ms: number): string {
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

const INPUT =
  'w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]';

interface BodyProps extends Omit<PlexPlaylistDetailModalProps, 'playlist' | 'onClose'> {
  playlist: PlexPlaylistSummary;
  onClose: () => void;
}

const DetailBody: React.FC<BodyProps> = ({
  playlist,
  items,
  isItemsLoading,
  isMutating,
  isAdmin,
  users,
  selectedUser,
  copyResults,
  onClose,
  onRename,
  onDelete,
  onRemoveItem,
  onMoveItem,
  onCopy,
  onAdopt,
  onSetOwner,
}) => {
  const isSmart = playlist.kind === 'smart';
  const [isRenaming, setIsRenaming] = useState<boolean>(false);
  const [draftTitle, setDraftTitle] = useState<string>(playlist.title);
  const [confirmDelete, setConfirmDelete] = useState<boolean>(false);
  const [copyTargets, setCopyTargets] = useState<string[]>([]);
  const [adoptMessage, setAdoptMessage] = useState<string | null>(null);

  const copyCandidates = users.filter((u) => u.username.toLowerCase() !== (selectedUser ?? '').toLowerCase());
  const canProtect = !isSmart && playlist.owner !== 'plexamp';

  const submitRename = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!draftTitle.trim() || draftTitle.trim() === playlist.title) {
      setIsRenaming(false);
      return;
    }
    if (await onRename(playlist, draftTitle)) setIsRenaming(false);
  };

  const toggleTarget = (username: string) =>
    setCopyTargets((prev) => (prev.includes(username) ? prev.filter((u) => u !== username) : [...prev, username]));

  const handleAdopt = async () => {
    const created = await onAdopt(playlist);
    setAdoptMessage(created ? 'Adopted into TrackSeerr.' : null);
  };

  return (
    <div className="space-y-5">
      {/* Header: badges, rename */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <PlexKindBadge kind={playlist.kind} />
          <PlexOwnerBadge owner={playlist.owner} />
          <span className="text-xs text-neutral-400 font-mono">{playlist.track_count} tracks</span>
        </div>

        {isRenaming ? (
          <form onSubmit={submitRename} className="flex items-center gap-2">
            <input
              id="plex-playlist-title"
              name="playlist-title"
              type="text"
              autoFocus
              value={draftTitle}
              onChange={(e) => setDraftTitle(e.target.value)}
              className={INPUT}
              aria-label="Playlist title"
            />
            <TapeDeckButton
              type="submit"
              size="sm"
              variant="amber"
              disabled={isMutating}
              aria-label="Save title"
              icon={<Check className="h-4 w-4" />}
            />
            <TapeDeckButton
              type="button"
              size="sm"
              aria-label="Cancel rename"
              onClick={() => {
                setIsRenaming(false);
                setDraftTitle(playlist.title);
              }}
              icon={<X className="h-4 w-4" />}
            />
          </form>
        ) : (
          <div className="flex items-center justify-between gap-2">
            <h4 className="font-bold text-sm sm:text-base text-white truncate" title={playlist.title}>
              {playlist.title}
            </h4>
            <TapeDeckButton
              size="sm"
              onClick={() => setIsRenaming(true)}
              icon={<Pencil className="h-3.5 w-3.5" />}
            >
              Rename
            </TapeDeckButton>
          </div>
        )}
      </div>

      {/* Protection toggle */}
      {canProtect && (
        <div className="flex items-center justify-between gap-3 p-3 bg-[#161616] border border-[#222222] rounded-[3px]">
          <div className="text-xs text-neutral-300">
            <p className="font-bold uppercase tracking-wide">Protect from sync</p>
            <p className="text-neutral-500 mt-0.5">
              {playlist.owner === 'user'
                ? 'Owned by you. TrackSeerr sync will never overwrite it.'
                : 'Managed by TrackSeerr. Sync may overwrite it.'}
            </p>
          </div>
          <TactileSwitch
            checked={playlist.owner === 'user'}
            onChange={(on) => void onSetOwner(playlist, on ? 'user' : 'trackseerr')}
            title="Protect from sync"
            ariaLabel={`Protect ${playlist.title} from sync`}
          />
        </div>
      )}

      {/* Tracks */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-[10px] uppercase tracking-wider text-neutral-400 font-mono">Tracks</span>
        </div>
        {isSmart && (
          <p className="text-[11px] text-neutral-500 font-mono" role="note">
            {SMART_HINT}
          </p>
        )}
        {isItemsLoading ? (
          <div className="flex justify-center py-8">
            <CassetteLoader size="sm" />
          </div>
        ) : items.length === 0 ? (
          <div className="text-center py-6 text-neutral-500 font-mono text-xs">No tracks.</div>
        ) : (
          <ul className="divide-y divide-[#1f1f1f] border border-[#222222] rounded-[3px] bg-[#0f0f0f]">
            {items.map((item, idx) => (
              <li key={item.playlist_item_id} className="flex items-center gap-2 px-3 py-2">
                <span className="w-6 text-right text-[10px] font-mono text-neutral-600">{idx + 1}</span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm text-white truncate">{item.title}</p>
                  <p className="text-xs text-neutral-400 truncate">
                    {item.artist}
                    {item.album ? ` · ${item.album}` : ''}
                  </p>
                </div>
                <span className="hidden sm:inline text-[10px] font-mono text-neutral-500">
                  {formatDuration(item.duration_ms)}
                </span>
                <div className="flex items-center gap-1">
                  <TapeDeckButton
                    size="sm"
                    disabled={isSmart || idx === 0}
                    title={isSmart ? SMART_HINT : 'Move up'}
                    aria-label="Move track up"
                    onClick={() => void onMoveItem(item, 'up')}
                    icon={<ArrowUp className="h-3.5 w-3.5" />}
                  />
                  <TapeDeckButton
                    size="sm"
                    disabled={isSmart || idx === items.length - 1}
                    title={isSmart ? SMART_HINT : 'Move down'}
                    aria-label="Move track down"
                    onClick={() => void onMoveItem(item, 'down')}
                    icon={<ArrowDown className="h-3.5 w-3.5" />}
                  />
                  <TapeDeckButton
                    size="sm"
                    variant="danger"
                    disabled={isSmart}
                    title={isSmart ? SMART_HINT : 'Remove from playlist'}
                    aria-label="Remove track"
                    onClick={() => void onRemoveItem(item)}
                    icon={<Trash2 className="h-3.5 w-3.5" />}
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Adopt */}
      {!playlist.trackseerr_playlist_id && (
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
      {adoptMessage && playlist.trackseerr_playlist_id && (
        <p className="text-xs text-[#22c55e] font-mono">{adoptMessage}</p>
      )}

      {/* Copy to users (admin) */}
      {isAdmin && (
        <div className="space-y-2 pt-3 border-t border-[#1f1f1f]">
          <span className="text-[10px] uppercase tracking-wider text-neutral-400 font-mono">Copy to users</span>
          {copyCandidates.length === 0 ? (
            <p className="text-[11px] text-neutral-500 font-mono">No other Plex users available.</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {copyCandidates.map((u) => {
                const on = copyTargets.includes(u.username);
                return (
                  <button
                    key={u.username}
                    type="button"
                    aria-pressed={on}
                    onClick={() => toggleTarget(u.username)}
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
            disabled={isMutating || copyTargets.length === 0}
            onClick={() => void onCopy(playlist, copyTargets)}
            icon={
              isMutating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Copy className="h-3.5 w-3.5" />
            }
          >
            Copy
          </TapeDeckButton>
          {copyResults && copyResults.length > 0 && (
            <ul className="space-y-1" aria-label="Copy results">
              {copyResults.map((r) => (
                <li
                  key={r.username}
                  className={`text-xs font-mono ${
                    !r.success
                      ? 'text-[#ef4444]'
                      : r.omitted_tracks > 0
                        ? 'text-[var(--accent-amber)]'
                        : 'text-[#22c55e]'
                  }`}
                >
                  {r.username}:{' '}
                  {r.success
                    ? `Copied ${r.copied_tracks} ${r.copied_tracks === 1 ? 'track' : 'tracks'}`
                    : r.error || 'failed'}
                  {r.success && r.omitted_tracks > 0 && (
                    <span className="block" role="alert">
                      {r.omitted_tracks} {r.omitted_tracks === 1 ? 'track' : 'tracks'} not available in{' '}
                      {r.username}&apos;s library
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Delete with in-modal confirmation */}
      <div className="pt-3 border-t border-[#1f1f1f]">
        {confirmDelete ? (
          <div className="p-3 bg-[#1a0f0f] border border-[#ef4444]/40 rounded-[3px] space-y-3">
            <p className="text-xs text-neutral-200">
              Delete &ldquo;{playlist.title}&rdquo; from Plex? This cannot be undone.
            </p>
            <div className="flex items-center gap-2">
              <TapeDeckButton
                size="sm"
                variant="danger"
                disabled={isMutating}
                onClick={async () => {
                  if (await onDelete(playlist)) onClose();
                }}
                icon={
                  isMutating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />
                }
              >
                Confirm Delete
              </TapeDeckButton>
              <TapeDeckButton size="sm" onClick={() => setConfirmDelete(false)}>
                Cancel
              </TapeDeckButton>
            </div>
          </div>
        ) : (
          <TapeDeckButton
            size="sm"
            variant="danger"
            onClick={() => setConfirmDelete(true)}
            icon={<Trash2 className="h-3.5 w-3.5" />}
          >
            Delete Playlist
          </TapeDeckButton>
        )}
      </div>
    </div>
  );
};

export const PlexPlaylistDetailModal: React.FC<PlexPlaylistDetailModalProps> = ({ playlist, onClose, ...rest }) => (
  <ObsidianModal
    isOpen={playlist !== null}
    onClose={onClose}
    title={playlist?.title ?? 'Playlist'}
    subtitle={playlist ? `Plex profile: ${playlist.plex_user}` : undefined}
  >
    {playlist && <DetailBody key={playlist.rating_key} playlist={playlist} onClose={onClose} {...rest} />}
  </ObsidianModal>
);
