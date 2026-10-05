import React from 'react';
import { PowerOff, ExternalLink, ArrowRightLeft } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';
import type { LibraryManagerMode } from '@/types/models';

export const MANAGER_LABEL: Record<LibraryManagerMode, string> = {
  native: 'TrackSeerr',
  lidarr: 'Lidarr',
};

/** Who is in charge, phrased for the inactive-side banner. */
const MANAGER_STATEMENT: Record<LibraryManagerMode, string> = {
  native: 'TrackSeerr is managing your library.',
  lidarr: 'Lidarr is managing your library.',
};

export interface ManagedExternally {
  /** Lidarr URL from settings; null/empty when not configured. */
  url?: string | null;
  /** Fallback when no URL is set: jump to the Lidarr settings tab. */
  onOpenLidarrSettings: () => void;
}

export interface InactiveGateProps {
  /** True when this group is the active library manager's side. */
  active: boolean;
  /** The manager currently in charge. */
  activeManager: LibraryManagerMode;
  /** Opens the switch confirmation for the given mode. */
  onRequestSwitch: (mode: LibraryManagerMode) => void;
  /** Dead-page rule: page has no content of its own while Lidarr manages the library. */
  managedExternally?: ManagedExternally;
  children: React.ReactNode;
}

export interface InactiveBannerProps {
  activeManager: LibraryManagerMode;
  onRequestSwitch: (mode: LibraryManagerMode) => void;
  managedExternally?: ManagedExternally;
  /** Extra one-line hint under the banner text. */
  note?: string;
}

export const InactiveBanner: React.FC<InactiveBannerProps> = ({
  activeManager,
  onRequestSwitch,
  managedExternally,
  note,
}) => {
  const target: LibraryManagerMode = activeManager === 'lidarr' ? 'native' : 'lidarr';
  return (
    <div
      role="status"
      className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-[#121212] border border-[#2a2a2a] rounded-[4px] px-3 py-2 sm:px-4 sm:py-3"
    >
      <div className="flex items-start gap-2 text-xs font-mono text-neutral-300">
        <PowerOff className="h-4 w-4 shrink-0 text-neutral-500 mt-px" />
        <div className="space-y-1">
          <p>Inactive &mdash; {MANAGER_STATEMENT[activeManager]}</p>
          {note && <p className="text-neutral-500">{note}</p>}
          {managedExternally && (
            <p className="text-neutral-400">
              Managed by Lidarr.{' '}
              {managedExternally.url ? (
                <a
                  href={managedExternally.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-[#e5a00d] hover:underline"
                >
                  Open Lidarr <ExternalLink className="h-3 w-3" />
                </a>
              ) : (
                <button
                  type="button"
                  onClick={managedExternally.onOpenLidarrSettings}
                  className="text-[#e5a00d] hover:underline"
                >
                  Set the Lidarr URL in Lidarr settings
                </button>
              )}
            </p>
          )}
        </div>
      </div>
      <TapeDeckButton
        size="sm"
        onClick={() => onRequestSwitch(target)}
        icon={<ArrowRightLeft className="h-3.5 w-3.5" />}
      >
        Switch to {MANAGER_LABEL[target]}
      </TapeDeckButton>
    </div>
  );
};

/** Greys out and disables a settings group on the inactive side of the library-manager interlock, keeping values visible. */
export const InactiveGate: React.FC<InactiveGateProps> = ({
  active,
  activeManager,
  onRequestSwitch,
  managedExternally,
  children,
}) => {
  if (active) return <>{children}</>;
  return (
    <div className="space-y-4">
      <InactiveBanner
        activeManager={activeManager}
        onRequestSwitch={onRequestSwitch}
        managedExternally={managedExternally}
      />
      <fieldset disabled aria-disabled="true" className="m-0 min-w-0 border-0 p-0 opacity-50 grayscale">
        {children}
      </fieldset>
    </div>
  );
};
