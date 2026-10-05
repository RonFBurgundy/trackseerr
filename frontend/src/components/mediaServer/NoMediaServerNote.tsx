import React from 'react';
import { Info } from 'lucide-react';

/** Shown where playlist push would appear when no media server is connected. */
export const NoMediaServerNote: React.FC = () => (
  <div
    role="note"
    className="flex items-start gap-3 rounded-[4px] border border-[var(--border-default)] bg-[var(--bg-card)] p-3 text-xs font-mono text-neutral-400"
  >
    <Info className="mt-0.5 h-4 w-4 shrink-0 text-[#e5a00d]" aria-hidden="true" />
    <p>
      No media server connected. Trackseerr is managing your library; connect Plex, Subsonic or
      Jellyfin in Settings later.
    </p>
  </div>
);
