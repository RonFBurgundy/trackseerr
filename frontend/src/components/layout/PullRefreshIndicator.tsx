import React from 'react';
import { ArrowDown, Loader2 } from 'lucide-react';
import { PULL_THRESHOLD_PX } from '@/hooks/usePullToRefresh';
import type { PullState } from '@/hooks/usePullToRefresh';

/** Machined amber pull key that rides down from the top edge of its (relative) parent while pulling. */
export const PullRefreshIndicator: React.FC<PullState> = ({ distance, phase }) => {
  if (phase === 'idle') return null;
  const progress = Math.min(1, distance / PULL_THRESHOLD_PX);
  const armed = phase === 'armed' || phase === 'refreshing';
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute left-1/2 top-0 z-20"
      style={{ transform: `translate(-50%, ${distance - 36}px)`, opacity: Math.max(0.25, progress) }}
    >
      <div
        className={`flex h-8 w-8 items-center justify-center rounded-[3px] border bg-[#121212] shadow-[0_2px_0_#050505] ${
          armed ? 'border-[var(--accent-amber)] text-[var(--accent-amber)]' : 'border-[#2a2a2a] text-[var(--text-secondary)]'
        }`}
      >
        {phase === 'refreshing' ? (
          <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" />
        ) : (
          <ArrowDown className="h-4 w-4 transition-transform" style={{ transform: `rotate(${armed ? 180 : progress * 120}deg)` }} />
        )}
      </div>
      <span className="sr-only" role="status">
        {phase === 'refreshing' ? 'Refreshing' : armed ? 'Release to refresh' : 'Pull to refresh'}
      </span>
    </div>
  );
};
