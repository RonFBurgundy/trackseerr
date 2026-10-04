import React, { useState } from 'react';
import {
  RefreshCw,
  Play,
  X,
  RotateCcw,
  DownloadCloud,
  AlertTriangle,
  Loader2,
} from 'lucide-react';
import type { UseQueueReturn } from '@/hooks/useQueue';
import {
  TapeTransportBay,
  TapeDeckButton,
  MachinedCard,
  ConfirmDangerButton,
} from '@/components/ui';

export interface ActivityViewProps {
  queueHook: UseQueueReturn;
}

/** Download queue. Events and logs live under Settings > System; phase 3 rebuilds this into Queue/History/Blocklist. */
export const ActivityView: React.FC<ActivityViewProps> = ({
  queueHook,
}) => {
  const {
    queueItems,
    backlogStatus,
    isLoading: isQueueLoading,
    error: queueError,
    cancelItem,
    retryItem,
    triggerBacklogSearch,
    refresh: refreshQueue,
  } = queueHook;

  const [busyId, setBusyId] = useState<string | null>(null);
  const [isTriggeringBacklog, setIsTriggeringBacklog] = useState<boolean>(false);

  const handleCancel = async (id: string) => {
    setBusyId(id);
    try {
      await cancelItem(id);
    } finally {
      setBusyId(null);
    }
  };

  const handleRetry = async (id: string) => {
    setBusyId(id);
    try {
      await retryItem(id);
    } finally {
      setBusyId(null);
    }
  };

  const handleBacklogClick = async () => {
    setIsTriggeringBacklog(true);
    try {
      await triggerBacklogSearch();
    } finally {
      setIsTriggeringBacklog(false);
    }
  };

  const formatBytes = (bytes?: number) => {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-4">
        <h3 className="text-sm font-bold uppercase font-mono text-white">Download Queue{queueItems.length > 0 && ` (${queueItems.length})`}</h3>
        <TapeTransportBay className="flex items-center gap-2">
          <TapeDeckButton
            size="sm"
            onClick={refreshQueue}
            icon={<RefreshCw className="h-3.5 w-3.5" />}
          >
            Refresh Queue
          </TapeDeckButton>
          <TapeDeckButton
            size="sm"
            variant="amber"
            onClick={handleBacklogClick}
            disabled={isTriggeringBacklog || backlogStatus?.is_running}
            icon={
              isTriggeringBacklog ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="h-3.5 w-3.5" />
              )
            }
          >
            {backlogStatus?.is_running ? 'Backlog Running...' : 'Trigger Backlog Search'}
          </TapeDeckButton>
        </TapeTransportBay>
      </div>

    <div className="space-y-6">
      {/* Backlog Worker Status Banner */}
      {backlogStatus && (
        <div className="p-3 bg-[#121212] border border-[#222222] rounded-[4px] flex items-center justify-between text-xs font-mono">
          <div className="flex items-center gap-2">
            <DownloadCloud className="h-4 w-4 text-[#e5a00d]" />
            <span className="text-neutral-300">
              Backlog Worker: {backlogStatus.total_missing} missing releases tracked, {backlogStatus.in_progress} actively searching
            </span>
          </div>
          {backlogStatus.last_run && (
            <span className="text-neutral-500 text-[10px]">
              Last run: {new Date(backlogStatus.last_run).toLocaleTimeString()}
            </span>
          )}
        </div>
      )}

      {/* Loading state */}
      {isQueueLoading && (
        <div className="flex flex-col items-center justify-center py-16 gap-3">
          <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
          <span className="text-xs uppercase tracking-widest text-neutral-400 font-mono">
            Interrogating Download Clients...
          </span>
        </div>
      )}

      {/* Error state */}
      {queueError && !isQueueLoading && (
        <div className="p-4 bg-red-950/40 border border-red-800/50 rounded-[4px] text-xs text-red-300 font-mono flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>{queueError}</span>
        </div>
      )}

      {/* Empty state */}
      {!isQueueLoading && queueItems.length === 0 && !queueError && (
        <div className="text-center py-16 text-neutral-500 font-mono text-sm">
          Download queue is currently empty.
        </div>
      )}

      {/* Active Queue cards */}
      {!isQueueLoading && queueItems.length > 0 && (
        <div className="space-y-3">
          {queueItems.map((item) => {
            const isBusy = busyId === item.id;
            const progressVal = item.progress !== undefined ? Math.round(item.progress) : 0;

            return (
              <MachinedCard key={item.id} className="p-4 space-y-3">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="px-2 py-0.5 rounded-[2px] bg-[#1a1a1a] border border-[#2a2a2a] text-[10px] font-mono uppercase text-[#e5a00d]">
                        {item.protocol || item.download_client || 'Direct'}
                      </span>
                      <span className="text-xs font-mono text-neutral-400 uppercase">
                        {item.status}
                      </span>
                    </div>

                    <h4 className="font-bold text-sm text-white truncate" title={item.title}>
                      {item.title}
                    </h4>

                    {item.artist && (
                      <p className="text-xs text-neutral-400 truncate mt-0.5">
                        {item.artist} {item.album ? `— ${item.album}` : ''}
                      </p>
                    )}

                    {item.error_message && (
                      <p className="text-xs text-red-400 font-mono mt-1">
                        Error: {item.error_message}
                      </p>
                    )}
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    <TapeDeckButton
                      size="sm"
                      variant="amber"
                      disabled={isBusy}
                      onClick={() => handleRetry(item.id)}
                      icon={<RotateCcw className="h-3 w-3" />}
                      aria-label="Retry download"
                    />
                    <ConfirmDangerButton
                      disabled={isBusy}
                      onConfirm={() => void handleCancel(item.id)}
                      icon={<X className="h-3 w-3" />}
                      ariaLabel="Cancel download"
                      confirmLabel="Confirm Remove"
                    />
                  </div>
                </div>

                {/* Progress bar */}
                <div className="space-y-1">
                  <div className="flex justify-between text-[11px] font-mono text-neutral-400">
                    <span>{progressVal}%</span>
                    <span>
                      {item.size ? formatBytes(item.size) : ''}{' '}
                      {item.timeleft ? `· ETA: ${item.timeleft}` : ''}
                    </span>
                  </div>
                  <div className="w-full h-1.5 bg-[#0d0d0d] rounded-full overflow-hidden border border-[#1f1f1f]">
                    <div
                      className="h-full bg-[#e5a00d] transition-all duration-300"
                      style={{ width: `${progressVal}%` }}
                    />
                  </div>
                </div>
              </MachinedCard>
            );
          })}
        </div>
      )}
    </div>
    </div>
  );
};
