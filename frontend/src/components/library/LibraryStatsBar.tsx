import React from 'react';
import type { LibraryStats } from '@/types/models';

export interface LibraryStatsBarProps {
  stats: LibraryStats;
}

const StatCell: React.FC<{ label: string; value: React.ReactNode; accent?: boolean }> = ({ label, value, accent }) => (
  <div className="bg-[#121212] border border-[#222222] p-2 sm:p-3 rounded-[4px] min-w-0">
    <span className="block truncate text-[9px] sm:text-[10px] text-neutral-500 uppercase tracking-wider sm:tracking-widest font-mono">
      {label}
    </span>
    <p className={`text-sm sm:text-xl font-mono font-bold mt-0.5 sm:mt-1 ${accent ? 'text-[#e5a00d]' : 'text-white'}`}>{value}</p>
  </div>
);

export const LibraryStatsBar: React.FC<LibraryStatsBarProps> = ({ stats }) => (
  <div className="grid grid-cols-4 gap-2 sm:gap-3">
    <StatCell label="Artists" value={stats.artist_count} />
    <StatCell label="Albums" value={stats.album_count} />
    <StatCell label="Tracks" value={stats.track_count} />
    <StatCell label="Monitored" value={stats.monitored_artist_count ?? '-'} accent />
  </div>
);
