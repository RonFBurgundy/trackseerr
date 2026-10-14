import React, { useMemo, useState } from 'react';
import { AlertCircle, Check } from 'lucide-react';
import { ObsidianModal, PageLoader, TapeDeckButton } from '@/components/ui';
import { usePlaylistTracks } from '@/hooks/usePlaylistTracks';
import { createTrackAlias } from '@/services/trackFixService';
import { errorMessage } from '@/services/apiClient';
import type { Schema } from '@/types/apiSchema';
import type { DiscoveryItem } from '@/types/models';
import { FindTrackPanel } from './FindTrackPanel';

export interface PlaylistTracksModalProps {
  isOpen: boolean;
  onClose: () => void;
  playlistId: string | null;
  playlistName: string;
  /** Admins only: jump to the match-missing flow for this playlist. */
  onOpenMissing?: () => void;
  /** Admins see "Mark unmatchable" on missing rows. */
  isAdmin: boolean;
  /** Request a catalog song that is not in the library yet (from the Fix panel). */
  onRequest: (item: DiscoveryItem) => Promise<void>;
}

type TrackFilter = 'all' | 'missing';

export const PlaylistTracksModal: React.FC<PlaylistTracksModalProps> = ({
  isOpen,
  onClose,
  playlistId,
  playlistName,
  onOpenMissing,
  isAdmin,
  onRequest,
}) => {
  const { data, loading, error, reload } = usePlaylistTracks(isOpen ? playlistId : null);
  const [filter, setFilter] = useState<TrackFilter>('all');
  const [fixing, setFixing] = useState<Schema<'PlaylistTrackItem'> | null>(null);
  const [confirmIgnore, setConfirmIgnore] = useState<number | null>(null);
  const [ignoring, setIgnoring] = useState<boolean>(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const close = (): void => {
    setFixing(null);
    setConfirmIgnore(null);
    setActionError(null);
    onClose();
  };

  const markUnmatchable = async (track: Schema<'PlaylistTrackItem'>): Promise<void> => {
    setIgnoring(true);
    setActionError(null);
    try {
      await createTrackAlias({ kind: 'ignore', source_title: track.title, source_artist: track.artist });
      setConfirmIgnore(null);
      await reload();
    } catch (err: unknown) {
      setActionError(errorMessage(err, 'Failed to mark the track unmatchable'));
    } finally {
      setIgnoring(false);
    }
  };

  const visible = useMemo(() => {
    const tracks = data?.tracks ?? [];
    return filter === 'missing' ? tracks.filter((t) => t.status === 'missing') : tracks;
  }, [data, filter]);

  let summary = '';
  if (data) {
    const count = `${data.total} track${data.total === 1 ? '' : 's'}`;
    summary = data.synced ? `${count} \u00b7 ${data.missing} missing` : `${count} \u00b7 not synced yet`;
  }

  const footer =
    onOpenMissing && data && data.missing > 0 ? (
      <TapeDeckButton size="sm" variant="amber" onClick={onOpenMissing} icon={<AlertCircle className="h-3.5 w-3.5" />}>
        Fix missing
      </TapeDeckButton>
    ) : undefined;

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={close}
      title={playlistName}
      subtitle={summary || undefined}
      footer={footer}
    >
      {fixing && playlistId !== null ? (
        <FindTrackPanel
          playlistId={playlistId}
          playlistName={playlistName}
          track={{ title: fixing.title, artist: fixing.artist }}
          onBack={() => setFixing(null)}
          onRequest={onRequest}
          onDone={() => {
            setFixing(null);
            void reload();
          }}
        />
      ) : (
      <div className="flex flex-col gap-3 min-h-0">
        {actionError && (
          <p role="alert" className="text-xs font-mono text-[var(--status-error)]">
            {actionError}
          </p>
        )}
        <div className="tape-transport-bay p-1.5 flex items-center gap-1.5 self-start">
          <TapeDeckButton size="sm" active={filter === 'all'} onClick={() => setFilter('all')}>
            All
          </TapeDeckButton>
          <TapeDeckButton size="sm" active={filter === 'missing'} onClick={() => setFilter('missing')}>
            Missing
          </TapeDeckButton>
        </div>

        {loading && (
          <PageLoader size="sm" />
        )}

        {!loading && error && (
          <div className="flex flex-col items-start gap-2 py-4">
            <p className="text-xs text-[#ef4444] font-mono">{error}</p>
            <TapeDeckButton size="sm" onClick={() => void reload()}>
              Retry
            </TapeDeckButton>
          </div>
        )}

        {!loading && !error && data && visible.length === 0 && (
          <p className="py-6 text-center text-xs text-neutral-500 font-mono">
            {filter === 'missing' ? 'No missing tracks.' : 'No tracks in this playlist.'}
          </p>
        )}

        {!loading && !error && visible.length > 0 && (
          <ul className="flex flex-col gap-1">
            {visible.map((t, idx) => (
              <li
                key={`${t.position}-${idx}`}
                className={`flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-[3px] border px-2.5 py-2 ${
                  t.status === 'missing'
                    ? 'border-[#e5a00d]/40 bg-[#e5a00d]/10'
                    : 'border-[#222222] bg-[#141414]'
                } ${t.status === 'ignored' ? 'opacity-60' : ''}`}
              >
                <span className="w-8 shrink-0 text-right text-[11px] font-mono text-neutral-500">{t.position}</span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm text-white truncate" title={t.title}>
                    {t.title}
                  </p>
                  <p className="text-xs text-neutral-400 truncate">
                    {[t.artist, t.album].filter(Boolean).join(' \u00b7 ')}
                  </p>
                </div>
                {t.status === 'missing' && (
                  <span className="shrink-0 rounded-[3px] border border-[#e5a00d]/50 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-[#e5a00d]">
                    Missing
                  </span>
                )}
                {t.status === 'ignored' && (
                  <span className="shrink-0 rounded-[3px] border border-[#2a2a2a] px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-neutral-500">
                    Ignored
                  </span>
                )}
                {t.suggestion === 'approved' && (
                  <span className="shrink-0 text-[10px] uppercase tracking-wider text-[var(--status-success)] font-mono">
                    Fixed - applies next sync
                  </span>
                )}
                {t.suggestion === 'pending' && (
                  <span className="shrink-0 text-[10px] uppercase tracking-wider text-neutral-400 font-mono">
                    Suggested - awaiting admin
                  </span>
                )}
                {t.status === 'missing' && !t.suggestion && (
                  <TapeDeckButton size="sm" variant="amber" onClick={() => setFixing(t)}>
                    Fix
                  </TapeDeckButton>
                )}
                {isAdmin && t.status === 'missing' && (
                  confirmIgnore === t.position ? (
                    <span className="flex items-center gap-1.5 shrink-0">
                      <TapeDeckButton size="sm" variant="danger" disabled={ignoring} onClick={() => void markUnmatchable(t)}>
                        Confirm
                      </TapeDeckButton>
                      <TapeDeckButton size="sm" disabled={ignoring} onClick={() => setConfirmIgnore(null)}>
                        Cancel
                      </TapeDeckButton>
                    </span>
                  ) : (
                    <TapeDeckButton size="sm" onClick={() => setConfirmIgnore(t.position)}>
                      Mark unmatchable
                    </TapeDeckButton>
                  )
                )}
                {t.status === 'matched' && (
                  <Check className="h-3.5 w-3.5 shrink-0 text-neutral-500" aria-label="Matched" />
                )}
                {t.status === 'pending' && (
                  <span className="shrink-0 text-[10px] uppercase tracking-wider text-neutral-500 font-mono">
                    Pending
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
      )}
    </ObsidianModal>
  );
};
