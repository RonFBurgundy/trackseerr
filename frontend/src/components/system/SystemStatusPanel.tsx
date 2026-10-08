import React, { useState } from 'react';
import { Loader2, AlertTriangle, ExternalLink, ChevronDown, ChevronRight } from 'lucide-react';
import { MachinedCard, TapeDeckButton } from '@/components/ui';
import { RequestPortalCard } from '@/components/deployment';
import { useSystemOverview } from '@/hooks/useSystemOverview';
import { useMediaServer } from '@/hooks/useMediaServer';
import { useChangelog } from '@/hooks';
import { formatBytes } from '@/components/lists/formatters';
import type { LibraryManagerMode, LidarrHealthItem } from '@/types/models';
import { formatDuration } from './formatters';
import { ChangelogModal } from './ChangelogModal';

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

const StatusRow: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className="py-2.5 flex justify-between gap-3">
    <span className="text-neutral-400">{label}</span>
    {children}
  </div>
);

/** Worker heartbeats are free-form dicts on the wire; read one boolean key defensively. */
function flag(heartbeat: Record<string, unknown>, key: string): boolean {
  return heartbeat[key] === true;
}

const WorkerState: React.FC<{ running: boolean; runningLabel?: string; idleLabel?: string }> = ({
  running,
  runningLabel = 'Running',
  idleLabel = 'Stopped',
}) => (
  <span className={running ? 'text-green-400' : 'text-neutral-500'}>{running ? runningLabel : idleLabel}</span>
);

export const SystemStatusPanel: React.FC<SystemStatusPanelProps> = ({ isCore, libraryMode }) => {
  const { status, statusError, lidarrHealth, healthError, isLoading } = useSystemOverview(libraryMode === 'lidarr');
  const mediaServer = useMediaServer();
  const mediaConnected = mediaServer.isPlex ? Boolean(status?.plex.online) : Boolean(mediaServer.status?.connected);
  const clientsOnline = status?.download_clients.filter((c) => c.online).length ?? 0;
  const indexersOnline = status?.indexers.filter((i) => i.online).length ?? 0;
  const [showChangelogModal, setShowChangelogModal] = useState<boolean>(false);
  const [whatsNewExpanded, setWhatsNewExpanded] = useState<boolean>(false);
  const changelogHook = useChangelog();

  return (
    <div className="space-y-6">
      {isCore && <RequestPortalCard />}

      <MachinedCard className="p-3 sm:p-6 max-w-2xl space-y-4">
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
            <StatusRow label="TrackSeerr Version:">
              <span className="text-white">
                {changelogHook.version || status.environment.version}
                {changelogHook.commit ? (
                  <span className="text-neutral-400 font-mono text-[11px] ml-1.5">
                    ({changelogHook.commit})
                  </span>
                ) : null}
              </span>
            </StatusRow>
            <StatusRow label="Role:">
              <span className="text-white">{status.environment.role}</span>
            </StatusRow>
            <StatusRow label="Uptime:">
              <span className="text-white">{formatDuration(status.environment.uptime_seconds * 1000)}</span>
            </StatusRow>
            <StatusRow label="Runtime:">
              <span className="text-white text-right break-words">
                Python {status.environment.python_version} &middot; {status.environment.platform}
              </span>
            </StatusRow>
            <StatusRow label="Database:">
              <span className="text-green-400">
                SQLite {status.database.sqlite_version} &middot; {formatBytes(status.database.size_bytes)}
              </span>
            </StatusRow>
            <StatusRow label={mediaServer.isPlex ? 'Plex Server Connection:' : 'Media Server:'}>
              <span className={mediaConnected ? 'text-green-400' : 'text-neutral-400'}>
                {!mediaServer.hasMediaServer ? 'Not configured' : mediaConnected ? 'Connected' : 'Configured'}
              </span>
            </StatusRow>
            <StatusRow label="Download Clients:">
              <span className={clientsOnline > 0 ? 'text-green-400' : 'text-neutral-500'}>
                {status.download_clients.length === 0
                  ? 'None configured'
                  : `${clientsOnline} of ${status.download_clients.length} online`}
              </span>
            </StatusRow>
            <StatusRow label="Indexers:">
              <span className={indexersOnline > 0 ? 'text-green-400' : 'text-neutral-500'}>
                {status.indexers.length === 0
                  ? 'None configured'
                  : `${indexersOnline} of ${status.indexers.length} online`}
              </span>
            </StatusRow>
            <StatusRow label="Acquisition Worker:">
              <WorkerState running={flag(status.workers.acquisition_worker, 'running')} />
            </StatusRow>
            <StatusRow label="Lidarr Worker:">
              <WorkerState running={flag(status.workers.lidarr_worker, 'running')} />
            </StatusRow>
            <StatusRow label="Playlist Sync:">
              <WorkerState running={flag(status.workers.sync_coordinator, 'is_syncing')} idleLabel="Idle" runningLabel="Syncing" />
            </StatusRow>
            {status.storage.map((d) => (
              <StatusRow key={d.path} label={`${d.label}:`}>
                <span className="text-white text-right">
                  {formatBytes(d.used_bytes)} / {formatBytes(d.total_bytes)} ({Math.round(d.percent_used)}%)
                </span>
              </StatusRow>
            ))}
          </div>
        )}
      </MachinedCard>

      <MachinedCard className="p-3 sm:p-6 max-w-2xl space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h4 className="text-sm font-bold uppercase font-mono text-white">
              Version {changelogHook.version || status?.environment.version || '1.0.0'}
              {changelogHook.commit ? (
                <span className="text-neutral-400 font-normal ml-2 text-xs">
                  ({changelogHook.commit})
                </span>
              ) : null}
            </h4>
            {changelogHook.latestRelease?.date_note && (
              <p className="text-xs font-mono text-neutral-400 mt-0.5">
                {changelogHook.latestRelease.date_note}
              </p>
            )}
          </div>
          <TapeDeckButton size="sm" onClick={() => setShowChangelogModal(true)}>
            All releases
          </TapeDeckButton>
        </div>

        {changelogHook.latestRelease && changelogHook.latestRelease.sections && changelogHook.latestRelease.sections.length > 0 && (
          <div>
            <button
              type="button"
              onClick={() => setWhatsNewExpanded((v) => !v)}
              aria-expanded={whatsNewExpanded}
              aria-controls="system-whats-new-items"
              className="inline-flex items-center gap-1.5 min-h-[28px] text-[11px] font-mono font-bold uppercase tracking-wider text-neutral-400 hover:text-white focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--accent-amber)]"
            >
              {whatsNewExpanded ? (
                <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
              ) : (
                <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              What's new
            </button>
            <div id="system-whats-new-items">
              {whatsNewExpanded && (
                <div className="mt-3 pt-3 border-t border-[#1f1f1f] space-y-3">
                  {changelogHook.latestRelease.sections.map((section, sIdx) => (
                    <div key={sIdx} className="space-y-1">
                      <h5 className="text-[11px] font-mono font-bold uppercase tracking-wider text-[var(--accent-amber)]">
                        {section.title}
                      </h5>
                      <ul className="space-y-1 pl-1">
                        {section.items.map((item, iIdx) => (
                          <li
                            key={iIdx}
                            className="flex items-start gap-2 text-xs font-mono text-neutral-300 leading-relaxed"
                          >
                            <span className="text-neutral-500 select-none mt-0.5">&bull;</span>
                            <span>{item}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </MachinedCard>

      <ChangelogModal
        isOpen={showChangelogModal}
        onClose={() => setShowChangelogModal(false)}
        releases={changelogHook.releases}
        currentVersion={changelogHook.version || status?.environment.version}
        commit={changelogHook.commit}
      />

      {libraryMode === 'lidarr' && (
        <MachinedCard className="p-3 sm:p-6 max-w-2xl space-y-4">
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
