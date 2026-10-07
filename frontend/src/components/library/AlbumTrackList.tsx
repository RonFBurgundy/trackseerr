import React, { useState } from 'react';
import { Loader2 } from 'lucide-react';
import type { TrackItem } from '@/types/models';
import { TactileSwitch } from '@/components/ui';
import { ItemHistoryModal } from './ItemHistoryModal';
import { formatTrackDuration, getQualityBadge, trackSeconds } from './trackFormat';

export interface AlbumTrackListProps {
  tracks: TrackItem[];
  loading: boolean;
  error: string | null;
  isAdmin: boolean;
  /** False in Lidarr mode: Lidarr has no per-track monitoring (the route is 409). */
  canMonitorTracks?: boolean;
  onToggleMonitored: (trackId: number | string, currentMonitored: boolean) => void;
  /** Extra per-row control rendered after the quality badge (e.g. a report key). */
  renderRowAction?: (track: TrackItem) => React.ReactNode;
  /** Tailwind max-height class that makes the list scroll internally. */
  scrollClass?: string;
  /** Default true: a track's title is a text link that opens that track's history. */
  trackHistory?: boolean;
}

/** One album's tracks (from the paged endpoint) with quality badge and monitoring switch. */
export const AlbumTrackList: React.FC<AlbumTrackListProps> = ({
  tracks,
  loading,
  error,
  isAdmin,
  canMonitorTracks = true,
  onToggleMonitored,
  renderRowAction,
  scrollClass = '',
  trackHistory = true,
}) => {
  const [historyTrack, setHistoryTrack] = useState<TrackItem | null>(null);
  if (loading) {
    return (
      <div className="flex justify-center py-6">
        <Loader2 className="h-6 w-6 text-[#e5a00d] animate-spin" />
      </div>
    );
  }
  if (error) {
    return (
      <p role="alert" className="text-xs text-red-300 font-mono py-4 text-center">
        {error}
      </p>
    );
  }
  if (tracks.length === 0) {
    return <p className="text-xs text-neutral-500 font-mono py-4 text-center">No tracks registered for this album.</p>;
  }
  return (
    <>
    <div
      className={`divide-y divide-[#181818] border border-[#1f1f1f] rounded-[3px] bg-[#0d0d0d] ${
        scrollClass ? `${scrollClass} overflow-y-auto` : 'overflow-hidden'
      }`}
    >
      {tracks.map((t) => {
        const badge = getQualityBadge(t);
        return (
          <div key={t.id} className="p-2.5 flex items-center justify-between gap-3 hover:bg-[#141414] transition-colors">
            <div className="flex items-center gap-3 min-w-0 flex-1">
              <span className="font-mono text-xs text-neutral-500 w-6 text-right flex-shrink-0">
                {t.track_number || 1}
              </span>
              {trackHistory ? (
                <button
                  type="button"
                  onClick={() => setHistoryTrack(t)}
                  className="text-sm text-neutral-200 min-w-0 flex-1 truncate text-left hover:text-white hover:underline"
                  title={`${t.title} \u2014 view history`}
                >
                  {t.title}
                </button>
              ) : (
                <span className="text-sm text-neutral-200 min-w-0 flex-1 truncate" title={t.title ?? undefined}>
                  {t.title}
                </span>
              )}
            </div>
            <div className="flex items-center gap-3 flex-shrink-0 font-mono text-xs">
              <span className="text-neutral-500 hidden sm:inline">{formatTrackDuration(trackSeconds(t))}</span>
              <span className={`px-2 py-0.5 rounded-[2px] text-[10px] font-bold ${badge.className}`}>{badge.label}</span>
              {renderRowAction?.(t)}
              {isAdmin && canMonitorTracks && t.monitored !== null && (
                <div className="flex items-center gap-1.5 pl-1">
                  <span
                    className={`text-[9px] uppercase tracking-wider hidden sm:inline ${
                      t.monitored ? 'text-[#e5a00d]' : 'text-neutral-500'
                    }`}
                  >
                    {t.monitored ? 'Monitored' : 'Unmonitored'}
                  </span>
                  <TactileSwitch
                    checked={t.monitored === true}
                    onChange={() => onToggleMonitored(t.id, t.monitored === true)}
                    ariaLabel={`Monitor ${t.title}`}
                  />
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
    {historyTrack && (
      <ItemHistoryModal isOpen onClose={() => setHistoryTrack(null)} entity="track" entityId={String(historyTrack.id)} title={historyTrack.title ?? ''} />
    )}
    </>
  );
};
