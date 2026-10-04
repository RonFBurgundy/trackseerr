import React from 'react';
import { Loader2, AlertTriangle, ExternalLink } from 'lucide-react';
import { MachinedCard } from '@/components/ui';
import { RequestPortalCard } from '@/components/deployment';
import { useSystemOverview } from '@/hooks/useSystemOverview';
import type { LibraryManagerMode, LidarrHealthItem } from '@/types/models';

export interface SystemStatusPanelProps {
  isCore: boolean;
  libraryMode: LibraryManagerMode;
}

const HEALTH_STYLE: Record<LidarrHealthItem['type'], string> = {
  ok: 'text-green-400 border-green-800/40 bg-green-950/30',
  notice: 'text-sky-300 border-sky-800/40 bg-sky-950/30',
  warning: 'text-amber-300 border-amber-800/40 bg-amber-950/30',
  error: 'text-red-300 border-red-800/50 bg-red-950/40',
};

export const SystemStatusPanel: React.FC<SystemStatusPanelProps> = ({ isCore, libraryMode }) => {
  const { status, statusError, lidarrHealth, healthError, isLoading } = useSystemOverview(libraryMode === 'lidarr');

  return (
    <div className="space-y-6">
      {isCore && <RequestPortalCard />}

      <MachinedCard className="p-6 max-w-2xl space-y-4">
        <h4 className="text-sm font-bold uppercase font-mono text-white">System Diagnostics</h4>
        {isLoading && !status && !statusError && (
          <div className="flex items-center gap-2 text-xs font-mono text-neutral-400">
            <Loader2 className="h-4 w-4 animate-spin text-[#e5a00d]" /> Reading system status...
          </div>
        )}
        {statusError && (
          <p className="flex items-start gap-2 text-xs font-mono text-red-300">
            <AlertTriangle className="h-4 w-4 shrink-0" /> {statusError}
          </p>
        )}
        {status && (
          <div className="divide-y divide-[#1f1f1f] text-xs font-mono">
            <div className="py-2.5 flex justify-between gap-3">
              <span className="text-neutral-400">TrackSeerr Version:</span>
              <span className="text-white">{status.version || '1.0.0'}</span>
            </div>
            <div className="py-2.5 flex justify-between gap-3">
              <span className="text-neutral-400">Database Engine:</span>
              <span className="text-green-400">{status.database_status || 'SQLite OK'}</span>
            </div>
            <div className="py-2.5 flex justify-between gap-3">
              <span className="text-neutral-400">Plex Server Connection:</span>
              <span className={status.plex_connected ? 'text-green-400' : 'text-neutral-400'}>
                {status.plex_connected ? 'Connected' : 'Configured'}
              </span>
            </div>
            <div className="py-2.5 flex justify-between gap-3">
              <span className="text-neutral-400">Lidarr Connection:</span>
              <span className={status.lidarr_connected ? 'text-green-400' : 'text-neutral-500'}>
                {status.lidarr_connected ? 'Connected' : 'Standalone Mode'}
              </span>
            </div>
          </div>
        )}
      </MachinedCard>

      {libraryMode === 'lidarr' && (
        <MachinedCard className="p-6 max-w-2xl space-y-4">
          <div className="flex items-center justify-between gap-3">
            <h4 className="text-sm font-bold uppercase font-mono text-white">Lidarr Health</h4>
            {lidarrHealth && (
              <span
                className={`px-2 py-0.5 rounded-[2px] border text-[10px] font-mono uppercase ${
                  lidarrHealth.reachable
                    ? 'text-green-400 border-green-800/40 bg-green-950/30'
                    : 'text-red-300 border-red-800/50 bg-red-950/40'
                }`}
              >
                {lidarrHealth.reachable ? `Reachable${lidarrHealth.version ? ` v${lidarrHealth.version}` : ''}` : 'Unreachable'}
              </span>
            )}
          </div>

          {isLoading && !lidarrHealth && !healthError && (
            <div className="flex items-center gap-2 text-xs font-mono text-neutral-400">
              <Loader2 className="h-4 w-4 animate-spin text-[#e5a00d]" /> Querying Lidarr...
            </div>
          )}
          {healthError && (
            <p className="flex items-start gap-2 text-xs font-mono text-red-300">
              <AlertTriangle className="h-4 w-4 shrink-0" /> {healthError}
            </p>
          )}
          {lidarrHealth && lidarrHealth.health.length === 0 && (
            <p className="text-xs font-mono text-neutral-400">
              {lidarrHealth.reachable === false
                ? 'Lidarr could not be reached, so no health report is available.'
                : 'No health issues reported by Lidarr.'}
            </p>
          )}
          {lidarrHealth && lidarrHealth.health.length > 0 && (
            <ul className="space-y-2">
              {lidarrHealth.health.map((h, i) => (
                <li
                  key={`${h.source}-${i}`}
                  className={`border rounded-[3px] px-3 py-2 text-xs font-mono ${HEALTH_STYLE[h.type]}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="uppercase text-[10px] font-bold">
                      {h.type} &middot; {h.source}
                    </span>
                    {h.wiki_url && (
                      <a
                        href={h.wiki_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-[10px] hover:underline"
                      >
                        Wiki <ExternalLink className="h-3 w-3" />
                      </a>
                    )}
                  </div>
                  <p className="mt-1 break-words text-neutral-200">{h.message}</p>
                </li>
              ))}
            </ul>
          )}
        </MachinedCard>
      )}
    </div>
  );
};
