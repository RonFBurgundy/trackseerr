import React, { useState, useEffect, useMemo } from 'react';
import {
  RefreshCw,
  Plus,
  Play,
  Trash2,
  FileText,
  Link as LinkIcon,
  Loader2,
  Headphones,
  AlertCircle,
  History,
  Sparkles,
} from 'lucide-react';
import type { Playlist, User } from '@/types/models';
import type { ImportPlaylistPayload } from '@/services/playlistService';
import type { MediaServerType } from '@/types/mediaServer';
import {
  TabStrip,
  ActionBar,
  TapeDeckButton,
  MachinedCard,
  ObsidianModal,
  TactileSwitch,
  ConfirmDangerButton,
  MonitorModeSelect,
  CassetteLoader,
  ToastBanner,
} from '@/components/ui';
import { PageFrame } from '@/components/layout';
import { LIST_MONITOR_MODES, type ListMonitorMode } from '@/types/importLists';

/** Album and artist modes add to the library without a quota, so the server only accepts them from admins. */
const NON_ADMIN_MONITOR_MODES: ReadonlyArray<ListMonitorMode> = ['track', 'none'];
/** Without the auto-request permission a playlist can only list its missing tracks. */
const LIST_ONLY_MODES: ReadonlyArray<ListMonitorMode> = ['none'];
import { PlexPlaylistsSection } from '@/components/plex';
import { ListeningPlaylistModal } from '@/components/listening';
import {
  MissingTracksModal,
  MatchOverridesModal,
  SmartCollectionModal,
  SmartCollectionCardActions,
} from '@/components/playlists';
import { useMissingTracks } from '@/hooks/useMissingTracks';
import { useToast } from '@/hooks/useToast';
import { consumePendingImport, buildBookmarkletCode } from '@/services/bookmarkletImport';
import {
  AUTO_REQUEST_DENIED_REASON,
  LISTENING_PROVIDER_LABELS,
  isListeningService,
} from '@/types/listening';
import { isSmartCollection } from '@/types/smartCollections';
import { TailoredMixesSection } from '@/components/mixes';
import { NoMediaServerNote } from '@/components/mediaServer';

export interface PlaylistsViewProps {
  playlists: Playlist[];
  users: User[];
  currentUserId?: string;
  onSync: () => Promise<void>;
  onToggleTarget: (playlistId: number | string, userIds: string[]) => Promise<void>;
  onImport: (payload: ImportPlaylistPayload) => Promise<void>;
  onToggleActive?: (playlistId: number | string, active: boolean) => Promise<void>;
  onSetMonitorMode?: (playlist: Playlist, mode: ListMonitorMode) => Promise<void>;
  /** Admin or holder of the auto-request permission: may pick track mode and turn on auto-request. */
  canAutoRequest?: boolean;
  onSetAutoRequest?: (playlist: Playlist, autoRequest: boolean) => Promise<void>;
  /** Reload the playlists after one was created or synced. */
  onPlaylistsChanged?: () => Promise<void>;
  onDelete?: (playlistId: number | string) => Promise<void>;
  isLoading?: boolean;
  isAdmin?: boolean;
  /** False when no media server is connected: playlist push, Plex playlists and mixes are hidden. */
  hasMediaServer?: boolean;
  /** Which server receives playlists; Plex-only sections (Plex playlists) show for `plex` alone. */
  serverType?: MediaServerType;
  /** Name used in generic copy ("Plex", "Subsonic server", "Jellyfin server"). */
  serverLabel?: string;
  /** False when playlists can only go to one account (Subsonic): per-user sync targets are hidden. */
  canTargetUsers?: boolean;
  /** False when the server has no mixes (Subsonic): the Mixes section is hidden. */
  mixesEnabled?: boolean;
}

const SyncPlaylistsPanel: React.FC<PlaylistsViewProps> = ({
  playlists,
  users,
  currentUserId,
  onSync,
  onToggleTarget,
  onImport,
  onToggleActive,
  onSetMonitorMode,
  canAutoRequest = false,
  onSetAutoRequest,
  onPlaylistsChanged,
  onDelete,
  isLoading = false,
  isAdmin = false,
  hasMediaServer = true,
  serverLabel = 'Plex',
  canTargetUsers = true,
}) => {
  const { toast, showToast } = useToast();
  const [isSmartModalOpen, setIsSmartModalOpen] = useState<boolean>(false);
  const [editingCollectionId, setEditingCollectionId] = useState<string | undefined>(undefined);
  const [isSyncing, setIsSyncing] = useState<boolean>(false);
  const [isImportModalOpen, setIsImportModalOpen] = useState<boolean>(false);
  const [importTab, setImportTab] = useState<'link' | 'paste' | 'listening' | 'helper'>('link');
  const [isListeningModalOpen, setIsListeningModalOpen] = useState<boolean>(false);
  const [playlistName, setPlaylistName] = useState<string>('');
  const [playlistUrl, setPlaylistUrl] = useState<string>('');
  const [pastedTracks, setPastedTracks] = useState<string>('');
  const [isSubmittingImport, setIsSubmittingImport] = useState<boolean>(false);
  const [importError, setImportError] = useState<string | null>(null);
  const bookmarkletCode = buildBookmarkletCode();

  const missingHook = useMissingTracks({ isAdmin });
  const [selectedMissingPlaylistId, setSelectedMissingPlaylistId] = useState<string | null>(null);
  const [selectedMissingPlaylistName, setSelectedMissingPlaylistName] = useState<string>('');
  const [isOverridesModalOpen, setIsOverridesModalOpen] = useState<boolean>(false);

  const selectedMissingTracks = useMemo(
    () => (selectedMissingPlaylistId ? missingHook.missingTracks.filter((t) => t.playlist_id === selectedMissingPlaylistId) : []),
    [missingHook.missingTracks, selectedMissingPlaylistId]
  );

  useEffect(() => {
    const pending = consumePendingImport();
    if (pending) {
      setImportTab('link');
      setIsImportModalOpen(true);
      if (pending.url) {
        setPlaylistUrl(pending.url);
        setImportError(null);
      } else if (pending.error) {
        setPlaylistUrl('');
        setImportError(pending.error);
      }
    }
  }, []);

  // Non-admins may only target themselves (the server rejects other users with 403).
  const targetUsers = isAdmin ? users : users.filter((u) => u.id === currentUserId);

  const handleSyncClick = async () => {
    setIsSyncing(true);
    try {
      await onSync();
    } finally {
      setIsSyncing(false);
    }
  };

  const handleTargetClick = async (playlist: Playlist, userIdStr: string) => {
    const existing = playlist.targets;
    const updated = existing.includes(userIdStr)
      ? existing.filter((id) => id !== userIdStr)
      : [...existing, userIdStr];
    await onToggleTarget(playlist.id, updated);
  };

  const handleImportSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (importTab === 'paste' && !playlistName.trim()) return;

    setIsSubmittingImport(true);
    try {
      if (importTab === 'link') {
        await onImport({ source: 'link', url: playlistUrl.trim() });
      } else {
        const tracks = pastedTracks
          .split('\n')
          .map((l) => l.trim())
          .filter(Boolean);
        await onImport({ source: 'tracks', name: playlistName.trim(), tracks });
      }
      setIsImportModalOpen(false);
      setPlaylistName('');
      setPlaylistUrl('');
      setPastedTracks('');
    } finally {
      setIsSubmittingImport(false);
    }
  };

  return (
    <PageFrame
      bodyClassName="space-y-6"
      actions={
        <div className="flex items-center justify-between gap-2 min-w-0">
          <div className="tape-transport-bay p-1.5 flex items-center gap-1.5 shrink-0">
            {isAdmin && (
              <TapeDeckButton
                size="sm"
                variant="amber"
                onClick={handleSyncClick}
                disabled={isSyncing}
                aria-label={isSyncing ? 'Syncing playlists' : 'Sync playlists'}
                title="Sync playlists"
                collapseLabel
                icon={
                  isSyncing ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <RefreshCw className="h-3.5 w-3.5" />
                  )
                }
              >
                {isSyncing ? 'Syncing...' : 'Sync Playlists'}
              </TapeDeckButton>
            )}

            {isAdmin && (
              <TapeDeckButton
                size="sm"
                onClick={() => {
                  setEditingCollectionId(undefined);
                  setIsSmartModalOpen(true);
                }}
                aria-label="Create smart collection"
                title="Create smart collection"
                collapseLabel
                icon={<Sparkles className="h-3.5 w-3.5 text-[#e5a00d]" />}
              >
                Smart collection
              </TapeDeckButton>
            )}

            <TapeDeckButton
              size="sm"
              onClick={() => setIsImportModalOpen(true)}
              aria-label="Add playlist"
              title="Add playlist"
              collapseLabel
              icon={<Plus className="h-3.5 w-3.5" />}
            >
              Add Playlist
            </TapeDeckButton>

            {isAdmin && (
              <TapeDeckButton
                size="sm"
                onClick={() => {
                  void missingHook.loadOverrides();
                  setIsOverridesModalOpen(true);
                }}
                aria-label="Match Memory overrides"
                title="Match Memory overrides"
                collapseLabel
                icon={<History className="h-3.5 w-3.5 text-[#e5a00d]" />}
              >
                Match Memory
              </TapeDeckButton>
            )}
          </div>

          <div className="text-xs text-neutral-400 font-mono truncate">{playlists.length} Configured Playlists</div>
        </div>
      }
    >
      {toast && <ToastBanner message={toast.message} tone={toast.tone} />}

      {/* Loading state */}
      {isLoading && (
        <div className="py-16">
          <CassetteLoader size="md" />
        </div>
      )}

      {/* Playlists Grid */}
      {!isLoading && playlists.length === 0 && (
        <div className="text-center py-16 text-neutral-500 font-mono text-sm">
          No playlists configured yet. Click 'Add Playlist' to begin.
        </div>
      )}

      {!isLoading && playlists.length > 0 && (
        <div aria-label="Playlists" className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 content-start">
          {playlists.map((pl) => (
            <MachinedCard key={pl.id} className="p-3 sm:p-4 flex flex-col justify-between gap-4">
              <div>
                <div className="flex items-start justify-between gap-2 mb-2">
                  <span className="px-2 py-0.5 rounded-[2px] bg-[#1a1a1a] border border-[#2a2a2a] text-[10px] font-mono uppercase text-neutral-300">
                    {isSmartCollection(pl)
                      ? 'Smart collection'
                      : isListeningService(pl.service)
                      ? LISTENING_PROVIDER_LABELS[pl.service]
                      : pl.service}
                  </span>
                  {onToggleActive && (
                    <div className="flex items-center gap-2">
                      {isSmartCollection(pl) && (
                        <span className="text-[10px] font-mono text-neutral-400">
                          {pl.enabled ? 'Auto-update' : 'One-time'}
                        </span>
                      )}
                      <TactileSwitch
                        checked={pl.enabled}
                        onChange={(val) => onToggleActive(pl.id, val)}
                        ariaLabel={
                          isSmartCollection(pl)
                            ? `Auto-update ${pl.name}`
                            : `Playlist ${pl.name} active`
                        }
                        title={isSmartCollection(pl) ? `Auto-update ${pl.name}` : undefined}
                      />
                    </div>
                  )}
                </div>

                <h4 className="font-bold text-sm sm:text-base text-white truncate" title={pl.name}>
                  {pl.name}
                </h4>

                <div className="flex items-center gap-3 text-xs text-neutral-400 font-mono mt-2">
                  <span>{pl.sync_status}</span>
                </div>

                {pl.last_synced_at && (
                  <p className="text-[10px] text-neutral-500 font-mono mt-1">
                    Last sync: {new Date(pl.last_synced_at).toLocaleString()}
                  </p>
                )}
              </div>

              {isListeningService(pl.service) ? (
                <div className="space-y-1.5 pt-3 border-t border-[#1f1f1f]">
                  {canAutoRequest && onSetAutoRequest ? (
                    <TactileSwitch
                      id={`pl-auto-${pl.id}`}
                      name={`auto-request-${pl.id}`}
                      label="Auto-request missing tracks"
                      checked={pl.auto_request}
                      onChange={(val) => void onSetAutoRequest(pl, val)}
                    />
                  ) : (
                    <p className="text-[11px] font-mono text-neutral-500">
                      {pl.auto_request ? 'Auto-request is on, but you no longer have permission.' : AUTO_REQUEST_DENIED_REASON}
                    </p>
                  )}
                </div>
              ) : isSmartCollection(pl) ? null : (
                onSetMonitorMode && (
                  <div className="space-y-1.5 pt-3 border-t border-[#1f1f1f]">
                    <label
                      htmlFor={`pl-mode-${pl.id}`}
                      className="text-[10px] uppercase tracking-wider text-neutral-400 font-mono"
                    >
                      Monitor mode
                    </label>
                    <MonitorModeSelect
                      id={`pl-mode-${pl.id}`}
                      compact
                      allowedModes={isAdmin ? LIST_MONITOR_MODES : canAutoRequest ? NON_ADMIN_MONITOR_MODES : LIST_ONLY_MODES}
                      value={pl.monitor_mode}
                      onChange={(m) => void onSetMonitorMode(pl, m)}
                    />
                    {!isAdmin && !canAutoRequest && (
                      <p className="text-[11px] font-mono text-neutral-500">{AUTO_REQUEST_DENIED_REASON}</p>
                    )}
                  </div>
                )
              )}

              {/* Target Users Assignment: only meaningful when a media server receives the playlist */}
              {hasMediaServer && !canTargetUsers && (
                <p className="pt-3 border-t border-[#1f1f1f] text-[11px] font-mono text-neutral-500">
                  Pushed to the configured {serverLabel} account.
                </p>
              )}
              {hasMediaServer && canTargetUsers && (
                <div className="space-y-2 pt-3 border-t border-[#1f1f1f]">
                  <span className="text-[10px] uppercase tracking-wider text-neutral-400 font-mono">
                    Sync Targets
                  </span>
                  <div className="flex flex-wrap gap-1.5">
                    {targetUsers.map((u) => {
                      const uIdStr = String(u.id);
                      const isTarget = pl.targets.includes(uIdStr);
                      const canEdit = isAdmin || u.id === currentUserId;

                      return (
                        <button
                          key={u.id}
                          type="button"
                          disabled={!canEdit}
                          onClick={() => handleTargetClick(pl, uIdStr)}
                          className={`target-pill px-2.5 py-1 rounded-[3px] text-xs font-mono transition-all ${
                            isTarget
                              ? 'bg-[#e5a00d]/20 border border-[#e5a00d] text-[#e5a00d]'
                              : 'bg-[#121212] border border-[#222222] text-neutral-500 hover:text-neutral-300'
                          } ${!canEdit ? 'cursor-default opacity-80' : 'cursor-pointer'}`}
                        >
                          {u.username}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* Actions */}
              <div className="flex items-center justify-between pt-2 border-t border-[#1f1f1f]">
                {isSmartCollection(pl) ? (
                  isAdmin ? (
                    <SmartCollectionCardActions
                      playlist={pl}
                      onEdit={(id) => {
                        setEditingCollectionId(id);
                        setIsSmartModalOpen(true);
                      }}
                      onPlaylistsChanged={onPlaylistsChanged ?? onSync}
                      onToast={showToast}
                    />
                  ) : (
                    <span />
                  )
                ) : isAdmin && (missingHook.missingCountByPlaylist[pl.id] || 0) > 0 ? (
                  <TapeDeckButton
                    size="sm"
                    variant="amber"
                    onClick={() => {
                      setSelectedMissingPlaylistId(pl.id);
                      setSelectedMissingPlaylistName(pl.name);
                    }}
                    icon={<AlertCircle className="h-3.5 w-3.5" />}
                    title={`View ${missingHook.missingCountByPlaylist[pl.id]} missing track${missingHook.missingCountByPlaylist[pl.id] === 1 ? '' : 's'}`}
                  >
                    Missing ({missingHook.missingCountByPlaylist[pl.id]})
                  </TapeDeckButton>
                ) : (
                  <span />
                )}
                {isAdmin && onDelete && (
                  <ConfirmDangerButton
                    onConfirm={() => onDelete(pl.id)}
                    icon={<Trash2 className="h-3 w-3" />}
                    ariaLabel={`Delete playlist ${pl.name}`}
                    confirmLabel="Delete"
                  />
                )}
              </div>
            </MachinedCard>
          ))}
        </div>
      )}

      {/* Add / Import Playlist Modal */}
      <ObsidianModal
        isOpen={isImportModalOpen}
        onClose={() => setIsImportModalOpen(false)}
        title="Add Playlist"
        subtitle="Import music tracks from streaming playlists or text"
      >
        <div className="space-y-4">
          <TabStrip fill>
            <TapeDeckButton
              size="sm"
              active={importTab === 'link'}
              onClick={() => setImportTab('link')}
              icon={<LinkIcon className="h-3.5 w-3.5" />}
            >
              By Link
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              active={importTab === 'paste'}
              onClick={() => setImportTab('paste')}
              icon={<FileText className="h-3.5 w-3.5" />}
            >
              Paste Tracks
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              active={importTab === 'listening'}
              onClick={() => setImportTab('listening')}
              icon={<Headphones className="h-3.5 w-3.5" />}
            >
              My Listening
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              active={importTab === 'helper'}
              onClick={() => setImportTab('helper')}
              icon={<Play className="h-3.5 w-3.5" />}
            >
              1-Click Helper
            </TapeDeckButton>
          </TabStrip>

          <form onSubmit={handleImportSubmit} className="space-y-4">
            {importTab === 'paste' && (
              <div>
              <label
                htmlFor="import-playlist-name"
                className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1"
              >
                Playlist Name
              </label>
              <input
                id="import-playlist-name"
                name="playlist-name"
                type="text"
                required
                value={playlistName}
                onChange={(e) => setPlaylistName(e.target.value)}
                placeholder="e.g. Synthwave Night Drive"
                className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]"
              />
              </div>
            )}

            {importTab === 'link' && (
              <div className="space-y-2">
                {importError && (
                  <div
                    role="alert"
                    className="p-2.5 bg-red-950/40 border border-red-800/50 rounded-[3px] text-xs font-mono text-red-300"
                  >
                    {importError}
                  </div>
                )}
                <div>
                  <label
                    htmlFor="import-playlist-url"
                    className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1"
                  >
                    Playlist URL (Spotify or Deezer)
                  </label>
                  <input
                    id="import-playlist-url"
                    name="playlist-url"
                    type="url"
                    required
                    value={playlistUrl}
                    onChange={(e) => {
                      setPlaylistUrl(e.target.value);
                      if (importError) setImportError(null);
                    }}
                    placeholder="https://open.spotify.com/playlist/... or https://www.deezer.com/playlist/..."
                    className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white focus:outline-none focus:border-[#e5a00d]"
                  />
                  <p className="text-[11px] text-neutral-500 font-mono mt-1">
                    Keyless Spotify: No Spotify API Key Needed!
                  </p>
                </div>
              </div>
            )}

            {importTab === 'paste' && (
              <div>
                <label
                  htmlFor="import-playlist-tracks"
                  className="block text-xs uppercase font-mono tracking-wider text-neutral-300 mb-1"
                >
                  Tracks (one per line: Artist - Title)
                </label>
                <textarea
                  id="import-playlist-tracks"
                  name="playlist-tracks"
                  rows={6}
                  required
                  value={pastedTracks}
                  onChange={(e) => setPastedTracks(e.target.value)}
                  placeholder={`Kavinsky - Nightcall\nGunship - Tech Noir\nCarpenter Brut - Turbo Killer`}
                  className="w-full bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-[#e5a00d]"
                />
              </div>
            )}

            {importTab === 'listening' && (
              <div className="p-3 bg-[#161616] border border-[#222222] rounded-[3px] space-y-3 text-xs text-neutral-300 font-mono">
                <p>
                  Build a playlist from your own Last.fm or ListenBrainz account: loved tracks, top tracks or
                  generated playlists such as Weekly Jams. Missing tracks are listed only unless you opt in to
                  requesting them.
                </p>
                <TapeDeckButton
                  size="sm"
                  variant="amber"
                  onClick={() => {
                    setIsImportModalOpen(false);
                    setIsListeningModalOpen(true);
                  }}
                  icon={<Headphones className="h-3.5 w-3.5" />}
                >
                  Choose From Listening
                </TapeDeckButton>
              </div>
            )}

            {importTab === 'helper' && (
              <div className="p-3 bg-[#161616] border border-[#222222] rounded-[3px] space-y-3 text-xs text-neutral-300 font-mono">
                <div>
                  <p className="font-bold text-[#e5a00d]">1-Click Browser Bookmarklet</p>
                  <p className="mt-1 text-neutral-400">
                    Drag this helper to your bookmarks toolbar to instantly export any playlist
                    from Spotify Web Player or Deezer directly into TrackSeerr with 1 click.
                  </p>
                </div>
                <div className="pt-1 pb-1">
                  <a
                    href={bookmarkletCode}
                    draggable
                    onClick={(e) => e.preventDefault()}
                    className="inline-flex items-center gap-2 px-3 py-2 rounded-[3px] bg-[#1f1f1f] border border-[#383838] border-b-[#111111] text-xs font-semibold uppercase tracking-wider text-white hover:text-[#e5a00d] cursor-grab active:cursor-grabbing shadow-[0_2px_0_#050505] transition-colors"
                    title="Drag this button to your browser bookmarks toolbar"
                  >
                    <Play className="h-3.5 w-3.5 text-[#e5a00d]" />
                    Send to TrackSeerr
                  </a>
                </div>
                <div className="space-y-1">
                  <p className="text-[10px] text-neutral-500 uppercase tracking-wider font-semibold">
                    Or copy bookmarklet URL:
                  </p>
                  <div className="p-2 bg-[#0d0d0d] border border-[#2a2a2a] rounded-[3px] text-[11px] select-all break-all text-neutral-300">
                    {bookmarkletCode}
                  </div>
                </div>
              </div>
            )}

            {importTab !== 'helper' && importTab !== 'listening' && (
              <ActionBar align="end" className="pt-2">
                <TapeDeckButton
                  type="submit"
                  variant="amber"
                  size="md"
                  disabled={isSubmittingImport}
                  icon={
                    isSubmittingImport ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Plus className="h-4 w-4" />
                    )
                  }
                >
                  {isSubmittingImport ? 'Importing...' : 'Add Playlist'}
                </TapeDeckButton>
              </ActionBar>
            )}
          </form>
        </div>
      </ObsidianModal>

      <ListeningPlaylistModal
        isOpen={isListeningModalOpen}
        onClose={() => setIsListeningModalOpen(false)}
        canAutoRequest={canAutoRequest}
        onCreated={onPlaylistsChanged ?? onSync}
      />

      <SmartCollectionModal
        open={isSmartModalOpen}
        onClose={() => {
          setIsSmartModalOpen(false);
          setEditingCollectionId(undefined);
        }}
        onSaved={onPlaylistsChanged ?? onSync}
        collectionId={editingCollectionId}
        users={users}
        currentUserId={currentUserId}
        canTargetUsers={canTargetUsers}
        serverLabel={serverLabel}
      />

      <MissingTracksModal
        isOpen={selectedMissingPlaylistId !== null}
        onClose={() => setSelectedMissingPlaylistId(null)}
        playlistName={selectedMissingPlaylistName}
        tracks={selectedMissingTracks}
        onSearch={missingHook.searchTracks}
        onMatch={missingHook.matchTrack}
        onOpenOverrides={() => {
          void missingHook.loadOverrides();
          setIsOverridesModalOpen(true);
        }}
        overridesCount={missingHook.overrides.length}
      />

      <MatchOverridesModal
        isOpen={isOverridesModalOpen}
        onClose={() => setIsOverridesModalOpen(false)}
        overrides={missingHook.overrides}
        loading={missingHook.loadingOverrides}
        onDelete={missingHook.deleteOverride}
        onReload={missingHook.loadOverrides}
      />
    </PageFrame>
  );
};

type PlaylistsSection = 'sync' | 'plex' | 'mixes';

export const PlaylistsView: React.FC<PlaylistsViewProps> = (props) => {
  const [requestedSection, setSection] = useState<PlaylistsSection>('sync');
  const hasMediaServer = props.hasMediaServer ?? true;
  // Plex playlists and mixes only exist with a media server; never leave a hidden section selected.
  const serverType = props.serverType ?? 'plex';
  const showPlexSection = serverType === 'plex';
  const showMixes = props.mixesEnabled ?? true;
  const visible = (s: PlaylistsSection): boolean => s === 'sync' || (s === 'plex' ? showPlexSection : showMixes);
  const section: PlaylistsSection = hasMediaServer && visible(requestedSection) ? requestedSection : 'sync';

  return (
    <PageFrame
      scroll={section !== 'sync'}
      bodyClassName="space-y-6"
      nav={
      hasMediaServer ? (
        <TabStrip fill>
          <TapeDeckButton size="sm" active={section === 'sync'} onClick={() => setSection('sync')}>
            Sync
          </TapeDeckButton>
          {showPlexSection && (
            <TapeDeckButton size="sm" active={section === 'plex'} onClick={() => setSection('plex')}>
              Plex
            </TapeDeckButton>
          )}
          {showMixes && (
            <TapeDeckButton size="sm" active={section === 'mixes'} onClick={() => setSection('mixes')}>
              Mixes
            </TapeDeckButton>
          )}
        </TabStrip>
      ) : (
        <NoMediaServerNote />
      )
      }
    >
      {section === 'sync' && <SyncPlaylistsPanel {...props} hasMediaServer={hasMediaServer} />}
      {section === 'plex' && <PlexPlaylistsSection isAdmin={props.isAdmin ?? false} />}
      {section === 'mixes' && <TailoredMixesSection isAdmin={props.isAdmin ?? false} />}
    </PageFrame>
  );
};
