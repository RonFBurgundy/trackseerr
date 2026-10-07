import React from 'react';
import { Loader2, Check } from 'lucide-react';
import { TapeDeckButton, ActionBar } from '@/components/ui';
import type { LibraryManagerMode, LibraryManagerState } from '@/types/models';
import { MODE_LABEL } from '@/hooks/useLibraryModeSwitch';

const CONSEQUENCE: Record<LibraryManagerMode, string> = {
  native:
    'TrackSeerr will search, download, import and rename on its own. Requests stop going to Lidarr. Lidarr settings are kept.',
  lidarr:
    'Requests will be sent to Lidarr, and TrackSeerr acts as its front-end. Native downloads, imports and the scanner pause. Media Management settings are kept.',
};

export interface ModeSwitchConfirmProps {
  /** Effective current mode. */
  mode: LibraryManagerMode;
  pendingMode: LibraryManagerMode;
  state: LibraryManagerState | null;
  isSwitching: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

/** The one confirmation panel for the library-manager switch, rendered wherever the switch is offered. */
export const ModeSwitchConfirm: React.FC<ModeSwitchConfirmProps> = ({
  mode,
  pendingMode,
  state,
  isSwitching,
  onCancel,
  onConfirm,
}) => {
  const blocked = state ? !state.can_switch : false;
  return (
    <div
      className="border border-[#e5a00d]/40 bg-[#101010] rounded-[4px] p-4 space-y-3 max-w-2xl"
      role="alertdialog"
      aria-label="Confirm library manager switch"
    >
      <p className="text-xs font-mono text-white">
        Switch library manager from {MODE_LABEL[mode]} to {MODE_LABEL[pendingMode]}?
      </p>
      <p className="text-[11px] font-mono text-neutral-400">{CONSEQUENCE[pendingMode]}</p>
      {blocked && state?.blocking_reason && (
        <p className="text-[11px] font-mono text-amber-300">Currently blocked: {state.blocking_reason}</p>
      )}
      <ActionBar align="end" stackOnMobile className="max-sm:[&>*:last-child]:order-first">
        <TapeDeckButton size="sm" onClick={onCancel} disabled={isSwitching}>
          Cancel
        </TapeDeckButton>
        <TapeDeckButton
          size="sm"
          variant="amber"
          onClick={onConfirm}
          disabled={isSwitching || blocked}
          icon={isSwitching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
        >
          Switch to {MODE_LABEL[pendingMode]}
        </TapeDeckButton>
      </ActionBar>
    </div>
  );
};
