import React from 'react';
import { HardDrive, RefreshCw } from 'lucide-react';
import type { LidarrStatus, ScanStatus } from '@/types/models';
import { MachinedCard, TapeDeckButton } from '@/components/ui';

export interface LibraryScanBannerProps {
  scanStatus: ScanStatus | null;
  onCancel: () => void;
}

/** Live telemetry for a native library scan. */
export const LibraryScanBanner: React.FC<LibraryScanBannerProps> = ({ scanStatus, onCancel }) => {
  const processed = scanStatus?.processed_files || 0;
  const found = scanStatus?.total_files_found || 0;
  const pct = Math.round((processed / Math.max(1, found || 1)) * 100);
  return (
    <MachinedCard className="p-4 border-[#e5a00d]/50 bg-[#161616]/90 shadow-lg space-y-2.5">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <RefreshCw className="h-5 w-5 text-[#e5a00d] animate-spin flex-shrink-0" />
          <div>
            <h4 className="text-sm font-bold text-white font-mono flex items-center gap-2">
              <span>Library Scan In Progress</span>
              <span className="text-xs text-[#e5a00d] font-normal">
                {processed} / {found} files ({pct}%)
              </span>
            </h4>
            <p className="text-xs text-neutral-300 font-mono mt-0.5">
              {scanStatus?.artists_created || 0} artists &bull; {scanStatus?.albums_created || 0} albums &bull;{' '}
              {scanStatus?.files_indexed || 0} files indexed
            </p>
          </div>
        </div>
        <TapeDeckButton size="sm" variant="danger" onClick={onCancel} className="self-start sm:self-center flex-shrink-0">
          Cancel Scan
        </TapeDeckButton>
      </div>
      {scanStatus?.current_file && (
        <p className="truncate font-mono text-[10px] text-neutral-400">{scanStatus.current_file}</p>
      )}
    </MachinedCard>
  );
};

export interface LidarrMigrationBannerProps {
  status: LidarrStatus;
}

export const LidarrMigrationBanner: React.FC<LidarrMigrationBannerProps> = ({ status }) => (
  <div className="p-3 bg-[#151515] border border-blue-800/60 rounded-[4px] flex items-center gap-3 text-xs font-mono">
    <HardDrive className="h-4 w-4 text-blue-400 animate-spin" />
    <span className="text-blue-300">
      Lidarr Migration Active: {status.migrated_artists} of {status.total_artists} artists migrated ({status.progress}%)
    </span>
  </div>
);
