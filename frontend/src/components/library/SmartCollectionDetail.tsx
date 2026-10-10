import React, { useEffect, useState, useCallback } from 'react';
import { RefreshCw, Pencil, Trash2, Loader2, Music } from 'lucide-react';
import type { Schema } from '@/types/apiSchema';
import type { User } from '@/types/models';
import {
  getSmartCollection,
  previewSmartCollection,
  syncSmartCollection,
} from '@/services/smartCollectionService';
import { togglePlaylistActive, deletePlaylist } from '@/services/playlistService';
import { errorMessage } from '@/services/apiClient';
import {
  MachinedCard,
  TapeDeckButton,
  TactileSwitch,
  ConfirmDangerButton,
  CassetteLoader,
} from '@/components/ui';
import { DetailHeaderBar, PageFrame } from '@/components/layout';
import { SmartCollectionModal } from '@/components/playlists/SmartCollectionModal';
import { rulesToLibraryFilters, libraryFilterChips } from '@/lib/libraryFilters';
import { useTags } from '@/hooks/useTags';

export interface SmartCollectionDetailProps {
  collectionId: string;
  isAdmin: boolean;
  onBack: () => void;
  onDeleted?: (id: string, name: string) => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
  users?: User[];
  currentUserId?: string;
  canTargetUsers?: boolean;
  serverLabel?: string;
}

export const SmartCollectionDetail: React.FC<SmartCollectionDetailProps> = ({
  collectionId,
  isAdmin,
  onBack,
  onDeleted,
  onToast,
  users = [],
  currentUserId,
  canTargetUsers = false,
  serverLabel = 'Plex',
}) => {
  const [record, setRecord] = useState<Schema<'SmartCollectionRecord'> | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [matches, setMatches] = useState<Schema<'SmartPreviewTrack'>[]>([]);
  const [matchesLoading, setMatchesLoading] = useState<boolean>(false);
  const [isSyncing, setIsSyncing] = useState<boolean>(false);
  const [isTogglingMode, setIsTogglingMode] = useState<boolean>(false);
  const [editModalOpen, setEditModalOpen] = useState<boolean>(false);

  const { tags } = useTags(true);

  const loadRecord = useCallback(async () => {
    try {
      setLoading(true);
      const data = await getSmartCollection(collectionId);
      setRecord(data);
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to load smart collection'), 'error');
    } finally {
      setLoading(false);
    }
  }, [collectionId, onToast]);

  useEffect(() => {
    void loadRecord();
  }, [loadRecord]);

  useEffect(() => {
    if (!record?.rules) return;
    const controller = new AbortController();
    setMatchesLoading(true);
    previewSmartCollection(record.rules, controller.signal)
      .then((res) => {
        setMatches(res.tracks || []);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          onToast(errorMessage(err, 'Failed to load preview matches'), 'error');
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setMatchesLoading(false);
        }
      });

    return () => {
      controller.abort();
    };
  }, [record?.rules, onToast]);

  const handleToggleMode = async (enabled: boolean) => {
    if (!record) return;
    setIsTogglingMode(true);
    try {
      await togglePlaylistActive(record.id, enabled);
      setRecord({ ...record, enabled });
      onToast(`Mode updated to ${enabled ? 'Auto-update' : 'One-time'}`);
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to change mode'), 'error');
    } finally {
      setIsTogglingMode(false);
    }
  };

  const handleSync = async () => {
    if (!record) return;
    setIsSyncing(true);
    try {
      const res = await syncSmartCollection(record.id);
      onToast(`Synced "${res.name}": ${res.matched_count} of ${res.track_count} tracks matched`);
      void loadRecord();
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to sync smart collection'), 'error');
    } finally {
      setIsSyncing(false);
    }
  };

  const handleDelete = async () => {
    if (!record) return;
    try {
      await deletePlaylist(record.id);
      onToast(`Deleted "${record.name}"`);
      onDeleted?.(record.id, record.name);
      onBack();
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to delete smart collection'), 'error');
    }
  };

  const chips = record?.rules
    ? libraryFilterChips(rulesToLibraryFilters(record.rules).filters, tags)
    : [];

  const actions = (
    <div className="flex items-center gap-1.5">
      {isAdmin && (
        <>
          <TapeDeckButton
            size="sm"
            onClick={() => void handleSync()}
            disabled={isSyncing}
            icon={
              isSyncing ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <RefreshCw className="h-3.5 w-3.5" />
              )
            }
            collapseLabel="sm"
            aria-label="Sync smart collection"
            title="Sync smart collection"
          >
            {isSyncing ? 'Syncing...' : 'Sync now'}
          </TapeDeckButton>
          <TapeDeckButton
            size="sm"
            onClick={() => setEditModalOpen(true)}
            icon={<Pencil className="h-3.5 w-3.5" />}
            collapseLabel="sm"
            aria-label="Edit smart collection"
            title="Edit smart collection"
          >
            Edit
          </TapeDeckButton>
          <ConfirmDangerButton
            size="sm"
            onConfirm={() => void handleDelete()}
            icon={<Trash2 className="h-3.5 w-3.5" />}
            ariaLabel="Delete smart collection"
            idleLabel="Delete"
            collapseLabel
            confirmLabel="Confirm Delete"
          />
        </>
      )}
    </div>
  );

  return (
    <PageFrame
      nav={
        <DetailHeaderBar
          parentLabel="Collections"
          title={record?.name}
          onBack={onBack}
          actions={actions}
        />
      }
    >
      <div className="space-y-6">
        {loading ? (
          <div className="py-20">
            <CassetteLoader size="md" />
          </div>
        ) : !record ? (
          <div className="text-center py-12 text-neutral-500 font-mono text-sm border border-dashed border-[#222] rounded-[4px]">
            Smart collection not found.
          </div>
        ) : (
          <>
            <MachinedCard className="p-3 sm:p-6">
              <div className="space-y-4">
                <div className="flex flex-col sm:flex-row items-start justify-between gap-4">
                  <div className="space-y-1.5 flex-1 min-w-0">
                    <span className="text-[10px] font-mono uppercase tracking-widest text-[#e5a00d]">
                      Smart Collection Drilldown
                    </span>
                    <h2 className="text-2xl sm:text-3xl font-black text-white font-mono tracking-tight truncate">
                      {record.name}
                    </h2>
                    {record.description && (
                      <p className="text-xs text-neutral-300 font-mono">{record.description}</p>
                    )}
                  </div>

                  {isAdmin && (
                    <div className="flex-shrink-0 pt-1">
                      <TactileSwitch
                        checked={record.enabled}
                        onChange={(v) => void handleToggleMode(v)}
                        disabled={isTogglingMode}
                        label={record.enabled ? 'Auto-update' : 'One-time'}
                        ariaLabel={`Mode switch: ${record.enabled ? 'Auto-update' : 'One-time'}`}
                      />
                    </div>
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-3 pt-2 border-t border-[#1f1f1f] text-xs font-mono">
                  <span className="px-2.5 py-0.5 rounded-[2px] font-bold bg-[#e5a00d]/10 text-[#e5a00d] border border-[#e5a00d]/30">
                    {record.track_count ?? 0} Tracks
                  </span>
                  <span className="text-neutral-400">
                    Status: <span className="text-neutral-200">{record.sync_status || 'idle'}</span>
                  </span>
                  {record.last_synced_at ? (
                    <span className="text-neutral-400">
                      Last sync: {new Date(record.last_synced_at).toLocaleString()}
                    </span>
                  ) : (
                    <span className="text-neutral-500">Never synced</span>
                  )}
                </div>
              </div>
            </MachinedCard>

            <div className="space-y-2">
              <h3 className="text-sm font-mono uppercase tracking-wider text-neutral-400">
                Filter Rules ({chips.length})
              </h3>
              {chips.length > 0 ? (
                <div
                  role="region"
                  aria-label="Smart collection filter rules"
                  className="flex flex-wrap items-center gap-1.5 pb-1 text-xs"
                >
                  {chips.map((chip) => (
                    <span
                      key={chip.key}
                      className="inline-flex items-center gap-1 rounded-[3px] border border-[#2a2a2a] bg-[#181818] px-2.5 py-1 text-xs font-mono text-[var(--text-secondary)]"
                    >
                      {chip.label}
                    </span>
                  ))}
                </div>
              ) : (
                <div className="text-xs font-mono text-neutral-500">
                  No filter rules specified (matches all library tracks up to limit).
                </div>
              )}
            </div>

            <div className="space-y-3">
              <h3 className="text-sm font-mono uppercase tracking-wider text-neutral-400">
                Current Matches ({matches.length})
              </h3>
              {matchesLoading ? (
                <div className="py-12">
                  <CassetteLoader size="sm" />
                </div>
              ) : matches.length > 0 ? (
                <div className="space-y-1">
                  {matches.map((track, idx) => (
                    <div
                      key={`${track.artist}-${track.album}-${track.title}-${idx}`}
                      className="flex items-center gap-3 p-2.5 rounded-[3px] bg-[#121212] border border-[#1f1f1f] text-xs font-mono"
                    >
                      <Music className="h-4 w-4 text-neutral-500 shrink-0" />
                      <div className="min-w-0 flex-1 truncate">
                        <span className="font-bold text-white">{track.artist}</span>
                        <span className="text-neutral-400"> &mdash; {track.title}</span>
                        <span className="text-neutral-500"> &middot; {track.album}</span>
                        {track.year && <span className="text-neutral-500"> &middot; {track.year}</span>}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-center py-12 text-neutral-500 font-mono text-sm border border-dashed border-[#222] rounded-[4px]">
                  No matching tracks found in the library.
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {isAdmin && (
        <SmartCollectionModal
          open={editModalOpen}
          onClose={() => setEditModalOpen(false)}
          collectionId={collectionId}
          onSaved={() => {
            void loadRecord();
          }}
          users={users}
          currentUserId={currentUserId}
          canTargetUsers={canTargetUsers}
          serverLabel={serverLabel}
        />
      )}
    </PageFrame>
  );
};
