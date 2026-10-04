import React from 'react';
import { Loader2, AlertTriangle, Check } from 'lucide-react';
import { TapeTransportBay, TapeDeckButton, MachinedCard } from '@/components/ui';
import type { LibraryManagerMode } from '@/types/models';
import type { UseLibraryManagerReturn } from '@/hooks/useLibraryManager';
import { MANAGER_LABEL } from './InactiveGate';

export interface LibraryManagerSwitchProps {
  manager: UseLibraryManagerReturn;
  /** Effective mode (server value, or the media-settings fallback when the endpoint is unavailable). */
  mode: LibraryManagerMode;
  pendingMode: LibraryManagerMode | null;
  onPendingChange: (mode: LibraryManagerMode | null) => void;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

const CONSEQUENCE: Record<LibraryManagerMode, string> = {
  native:
    'TrackSeerr will search, download, import and rename on its own. Requests stop going to Lidarr. Lidarr settings are kept.',
  lidarr:
    'Requests will be sent to Lidarr, and TrackSeerr acts as its front-end. Native downloads, imports and the scanner pause. Media Management settings are kept.',
};

export const LibraryManagerSwitch: React.FC<LibraryManagerSwitchProps> = ({
  manager,
  mode,
  pendingMode,
  onPendingChange,
  onToast,
}) => {
  const { state, isLoading, isSwitching, loadError } = manager;
  const blocked = state ? !state.can_switch : false;
  const modes: LibraryManagerMode[] = ['native', 'lidarr'];

  const confirm = async () => {
    if (!pendingMode) return;
    const target = pendingMode;
    const res = await manager.switchTo(target);
    if (res.ok) {
      onToast(`Library manager is now ${MANAGER_LABEL[target]}`);
      onPendingChange(null);
    } else {
      onToast(res.message, 'error');
      // Keep the confirm panel closed; the server message is the explanation (409 in-flight / 422 unconfigured).
      onPendingChange(null);
      void manager.refresh();
    }
  };

  return (
    <MachinedCard className="p-5 max-w-2xl space-y-4">
      <div>
        <h4 className="text-sm font-bold uppercase font-mono text-white">Library manager</h4>
        <p className="text-xs text-neutral-400 font-mono mt-0.5">
          Exactly one system manages the library. The inactive side&apos;s settings are preserved.
        </p>
      </div>

      <div className="flex flex-col sm:flex-row sm:items-center gap-3">
        <TapeTransportBay className="flex items-center gap-1.5 w-full sm:w-auto" role="group" aria-label="Library manager">
          {modes.map((m) => (
            <TapeDeckButton
              key={m}
              size="md"
              active={mode === m}
              disabled={isSwitching || isLoading}
              aria-pressed={mode === m}
              className="flex-1 sm:flex-none sm:min-w-[120px]"
              onClick={() => {
                if (m !== mode) onPendingChange(m);
              }}
            >
              {MANAGER_LABEL[m]}
            </TapeDeckButton>
          ))}
        </TapeTransportBay>
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

      {pendingMode && (
        <div className="border border-[#e5a00d]/40 bg-[#101010] rounded-[4px] p-4 space-y-3" role="alertdialog" aria-label="Confirm library manager switch">
          <p className="text-xs font-mono text-white">
            Switch library manager from {MANAGER_LABEL[mode]} to {MANAGER_LABEL[pendingMode]}?
          </p>
          <p className="text-[11px] font-mono text-neutral-400">{CONSEQUENCE[pendingMode]}</p>
          {blocked && state?.blocking_reason && (
            <p className="text-[11px] font-mono text-amber-300">Currently blocked: {state.blocking_reason}</p>
          )}
          <div className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2">
            <TapeDeckButton size="sm" onClick={() => onPendingChange(null)} disabled={isSwitching}>
              Cancel
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              variant="amber"
              onClick={() => void confirm()}
              disabled={isSwitching || blocked}
              icon={isSwitching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
            >
              Switch to {MANAGER_LABEL[pendingMode]}
            </TapeDeckButton>
          </div>
        </div>
      )}
    </MachinedCard>
  );
};
