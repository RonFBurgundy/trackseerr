import React from 'react';
import { Loader2 } from 'lucide-react';
import { MachinedCard } from './MachinedCard';

export interface StartupScreenProps {
  step: string | null;
}

/** Full-viewport "TrackSeerr is starting" screen shown while the server finishes booting. */
export const StartupScreen: React.FC<StartupScreenProps> = ({ step }) => (
  <div
    className="h-full w-full overflow-y-auto flex bg-[#0a0a0a] px-4"
    role="status"
    aria-live="polite"
  >
    <MachinedCard className="m-auto w-full max-w-sm p-3 sm:p-6 flex flex-col items-center gap-4 text-center">
      <Loader2 className="w-8 h-8 text-[#e5a00d] animate-spin" aria-hidden="true" />
      <h1 className="text-sm font-semibold uppercase tracking-[0.05em] text-white">
        TrackSeerr is starting&hellip;
      </h1>
      <div className="w-full bg-[#0d0d0d] border border-[#1f1f1f] rounded-[3px] px-3 py-2 shadow-[inset_0_2px_4px_rgba(0,0,0,0.8)]">
        <p className="text-xs text-[#a3a3a3] font-mono break-words">{step ?? 'Waiting for the server'}</p>
      </div>
      <p className="text-xs text-[#666666]">This page will load automatically.</p>
    </MachinedCard>
  </div>
);
