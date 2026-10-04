import React from 'react';

export interface SourceBadgeProps {
  /** Server-reported `mode` (`native` | `lidarr`); renders nothing until known. */
  mode: string | null;
}

export const SourceBadge: React.FC<SourceBadgeProps> = ({ mode }) => {
  if (mode !== 'native' && mode !== 'lidarr') return null;
  return (
    <span className="inline-flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-wider text-neutral-400">
      Source:
      <span className="px-1.5 py-0.5 rounded-[2px] border border-[#2a2a2a] bg-[#0d0d0d] text-[#e5a00d] font-bold">
        {mode === 'lidarr' ? 'Lidarr' : 'TrackSeerr'}
      </span>
    </span>
  );
};
