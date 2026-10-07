import React from 'react';
import { Loader2, AlertTriangle } from 'lucide-react';
import { TabStrip, TapeDeckButton, MachinedCard } from '@/components/ui';
import type { LibraryManagerMode } from '@/types/models';
import type { UseLibraryManagerReturn } from '@/hooks/useLibraryManager';
import { MANAGER_LABEL } from './InactiveGate';

export interface LibraryManagerSwitchProps {
  manager: UseLibraryManagerReturn;
  /** Effective mode (server value, or the media-settings fallback when the endpoint is unavailable). */
  mode: LibraryManagerMode;
  /** Opens the shared confirmation (rendered by the parent) for the chosen mode. */
  onRequestSwitch: (mode: LibraryManagerMode) => void;
}

export const LibraryManagerSwitch: React.FC<LibraryManagerSwitchProps> = ({
  manager,
  mode,
  onRequestSwitch,
}) => {
  const { state, isLoading, isSwitching, loadError } = manager;
  const blocked = state ? !state.can_switch : false;
  const modes: LibraryManagerMode[] = ['native', 'lidarr'];

  return (
    <MachinedCard className="p-3 sm:p-5 max-w-2xl space-y-4">
      <div>
        <h4 className="text-sm font-bold uppercase font-mono text-white">Library manager</h4>
        <p className="text-xs text-neutral-400 font-mono mt-0.5">
          Exactly one system manages the library. The inactive side&apos;s settings are preserved.
        </p>
      </div>

      <div className="flex flex-col sm:flex-row sm:items-center gap-3">
        <TabStrip fill role="group" aria-label="Library manager">
          {modes.map((m) => (
            <TapeDeckButton
              key={m}
              size="md"
              active={mode === m}
              disabled={isSwitching || isLoading}
              aria-pressed={mode === m}
              className="flex-1 sm:flex-none sm:min-w-[120px]"
              onClick={() => {
                if (m !== mode) onRequestSwitch(m);
              }}
            >
              {MANAGER_LABEL[m]}
            </TapeDeckButton>
          ))}
        </TabStrip>
        {isLoading && <Loader2 className="h-4 w-4 animate-spin text-[#e5a00d]" />}
      </div>

      {loadError && (
        <p className="flex items-start gap-2 text-xs font-mono text-red-300">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>Library manager status unavailable: {loadError}</span>
        </p>
      )}

      {blocked && state?.blocking_reason && (
        <p className="flex items-start gap-2 text-xs font-mono text-amber-300">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>Switching is blocked: {state.blocking_reason}</span>
        </p>
      )}

      {state && !state.lidarr_configured && !state.native_configured && (
        <p className="text-[11px] font-mono text-neutral-400">
          Set up download clients + indexers, or connect Lidarr, to start fulfilling requests.
        </p>
      )}

      {state && !state.lidarr_configured && state.native_configured && mode === 'native' && (
        <p className="text-[11px] font-mono text-neutral-500">
          Lidarr is not configured yet. Set it up under Lidarr before switching.
        </p>
      )}

    </MachinedCard>
  );
};
