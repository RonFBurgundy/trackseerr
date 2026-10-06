import React from 'react';
import { HardDrive, RefreshCw, X } from 'lucide-react';
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
  const detail = `${scanStatus?.artists_created || 0} artists, ${scanStatus?.albums_created || 0} albums, ${
    scanStatus?.files_indexed || 0
  } files indexed${scanStatus?.current_file ? ` - ${scanStatus.current_file}` : ''}`;
  return (
    <MachinedCard
      role="status"
      title={detail}
      className="pl-3 pr-1 py-1 border-[#e5a00d]/50 bg-[#161616]/90 flex items-center gap-2 min-w-0"
    >
      <RefreshCw className="h-4 w-4 text-[#e5a00d] animate-spin shrink-0" />
      <div className="min-w-0 flex-1 truncate text-xs font-mono">
        <span className="font-bold text-white">Scanning</span>
        <span className="ml-2 text-[#e5a00d]">
          {processed} / {found} files ({pct}%)
        </span>
        <span className="ml-2 text-neutral-400 max-sm:hidden">{detail}</span>
      </div>
      <TapeDeckButton
        size="sm"
        variant="danger"
        onClick={onCancel}
        className="shrink-0"
        aria-label="Cancel scan"
        title="Cancel scan"
        icon={<X className="h-3.5 w-3.5" />}
        collapseLabel
      >
        Cancel
      </TapeDeckButton>
    </MachinedCard>
  );
};

export interface LidarrMigrationBannerProps {
  status: LidarrStatus;
}

export const LidarrMigrationBanner: React.FC<LidarrMigrationBannerProps> = ({ status }) => (
  <div className="px-3 py-2 bg-[#151515] border border-blue-800/60 rounded-[4px] flex items-center gap-3 text-xs font-mono min-w-0">
    <HardDrive className="h-4 w-4 text-blue-400 animate-spin" />
    <span className="text-blue-300 truncate" title={`Lidarr Migration Active: ${status.migrated_artists} of ${status.total_artists} artists migrated (${status.progress}%)`}>
      Lidarr Migration Active: {status.migrated_artists} of {status.total_artists} artists migrated ({status.progress}%)
    </span>
  </div>
);
