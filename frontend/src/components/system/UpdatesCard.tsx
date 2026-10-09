import React, { useId } from 'react';
import { Loader2, RotateCw, ExternalLink, AlertTriangle, CheckCircle2 } from 'lucide-react';
import { MachinedCard, TapeDeckButton, TactileSwitch } from '@/components/ui';
import type { UseUpdateCheckReturn } from '@/hooks/useUpdateCheck';
import { formatTimestamp } from './formatters';

export interface UpdatesCardProps {
  updateCheck: UseUpdateCheckReturn;
}

export const UpdatesCard: React.FC<UpdatesCardProps> = ({ updateCheck }) => {
  const switchId = useId();
  const { update, isLoading, isChecking, isSaving, error, checkNow, setEnabled } = updateCheck;

  const currentVersion = update?.current_version ?? '-';
  const latestVersion = update?.latest_version;
  const updateAvailable = Boolean(update?.update_available);
  const releaseUrl = update?.release_url;
  const lastChecked = update?.checked_at ? formatTimestamp(update.checked_at) : 'Never';
  const isEnabled = update ? update.enabled : true;

  return (
    <MachinedCard className="p-3 sm:p-6 max-w-2xl space-y-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h4 className="text-sm font-bold uppercase font-mono text-white">Software Updates</h4>
          <p className="text-xs text-neutral-400 font-mono mt-0.5">
            Check for official TrackSeerr releases on GitHub.
          </p>
        </div>
        <TapeDeckButton
          size="sm"
          onClick={() => void checkNow()}
          disabled={isChecking || isLoading}
          icon={isChecking ? <Loader2 className="h-3.5 w-3.5 animate-spin text-[#e5a00d]" /> : <RotateCw className="h-3.5 w-3.5" />}
          title="Run update check task now"
          aria-label="Check for updates now"
        >
          Check now
        </TapeDeckButton>
      </div>

      {error && (
        <div role="alert" className="p-3 bg-red-950/40 border border-red-800/50 rounded-[4px] text-xs text-red-300 font-mono flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="min-w-0 break-words">{error}</span>
        </div>
      )}

      <div className="divide-y divide-[#1f1f1f] text-xs font-mono">
        <div className="py-2.5 flex justify-between gap-3 items-center">
          <span className="text-neutral-400">Current Version:</span>
          <span className="text-white">
            {currentVersion}
            {update?.current_commit && (
              <span className="text-neutral-400 font-mono text-[11px] ml-1.5">
                ({update.current_commit})
              </span>
            )}
          </span>
        </div>

        <div className="py-2.5 flex justify-between gap-3 items-center">
          <span className="text-neutral-400">Latest Release:</span>
          <div className="flex items-center gap-2">
            {latestVersion ? (
              <>
                <span className={updateAvailable ? 'text-[#e5a00d] font-bold' : 'text-neutral-200'}>
                  {latestVersion}
                </span>
                {updateAvailable && (
                  <span className="px-1.5 py-0.5 rounded-[3px] bg-[var(--accent-amber)] text-[10px] font-mono font-bold text-black uppercase">
                    Update available
                  </span>
                )}
                {!updateAvailable && !update?.error && (
                  <span className="flex items-center gap-1 text-green-400 text-[11px]">
                    <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" /> Up to date
                  </span>
                )}
              </>
            ) : (
              <span className="text-neutral-500">None detected</span>
            )}
          </div>
        </div>

        {releaseUrl && (
          <div className="py-2.5 flex justify-between gap-3 items-center">
            <span className="text-neutral-400">Release Notes:</span>
            <a
              href={releaseUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-[var(--accent-amber)] hover:underline text-xs"
            >
              View on GitHub <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
            </a>
          </div>
        )}

        <div className="py-2.5 flex justify-between gap-3 items-center">
          <span className="text-neutral-400">Last Checked:</span>
          <span className="text-neutral-300">{lastChecked}</span>
        </div>

        <div className="py-3 flex justify-between gap-3 items-center">
          <span className="text-neutral-400">Automatic Checks:</span>
          <TactileSwitch
            id={switchId}
            name="update_check_enabled"
            checked={isEnabled}
            disabled={isSaving}
            onChange={(checked) => void setEnabled(checked)}
            label={isEnabled ? 'Enabled (every 12h)' : 'Disabled'}
          />
        </div>
      </div>
    </MachinedCard>
  );
};
