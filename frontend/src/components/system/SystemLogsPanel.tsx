import React, { useEffect, useId, useRef, useState } from 'react';
import { Download, Trash2, Loader2, Search, AlertTriangle } from 'lucide-react';
import { TapeDeckButton, ActionBar, TabStrip } from '@/components/ui';
import { PageFrame } from '@/components/layout';
import { useSystemLogs } from '@/hooks/useSystemLogs';

const LEVELS = ['all', 'info', 'warning', 'error', 'debug'] as const;

export const SystemLogsPanel: React.FC = () => {
  const logsHook = useSystemLogs();
  const { logs, filteredLogs } = logsHook;
  const [autoScroll, setAutoScroll] = useState<boolean>(true);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const searchId = useId();
  const autoScrollId = useId();

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [filteredLogs, autoScroll]);

  return (
    <PageFrame
      bodyRef={containerRef}
      ariaLabel="Application log entries"
      bodyClassName="space-y-1 p-1.5 font-mono text-[11px] leading-relaxed select-text bg-[#0a0a0a] border border-[#222222] rounded-[4px]"
      nav={
      <div className="space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h4 className="text-sm font-bold uppercase font-mono text-white">Application Logs</h4>
          <p className="text-xs text-neutral-400 font-mono mt-0.5">Live stream from the server process</p>
        </div>
        <ActionBar bay align="end" className="p-1.5 sm:w-auto">
          <TapeDeckButton
            size="sm"
            onClick={() => void logsHook.download()}
            disabled={logsHook.isDownloading}
            icon={
              logsHook.isDownloading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />
            }
          >
            Download Log File
          </TapeDeckButton>
          <TapeDeckButton
            size="sm"
            variant="danger"
            onClick={() => void logsHook.clear()}
            disabled={logsHook.isClearing || logs.length === 0}
            icon={logsHook.isClearing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
          >
            Clear Logs
          </TapeDeckButton>
        </ActionBar>
      </div>

      {logsHook.actionError && (
        <div className="p-3 bg-red-950/40 border border-red-800/50 rounded-[4px] text-xs text-red-300 font-mono flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>{logsHook.actionError}</span>
        </div>
      )}

      <div className="flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 p-3 bg-[#101010] border border-[#222222] rounded-[4px]">
        <div className="flex items-center gap-2 min-w-0">
          <span className="hidden sm:inline text-xs font-mono text-neutral-400">Level:</span>
          <TabStrip fill aria-label="Log level" className="min-w-0 flex-1">
            {LEVELS.map((lvl) => (
              <TapeDeckButton
                key={lvl}
                size="sm"
                active={logsHook.levelFilter === lvl}
                aria-pressed={logsHook.levelFilter === lvl}
                onClick={() => logsHook.setLevelFilter(lvl)}
              >
                {lvl}
              </TapeDeckButton>
            ))}
          </TabStrip>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <div className="relative flex-1 md:flex-none">
            <Search className="h-3 w-3 absolute left-2.5 top-1/2 -translate-y-1/2 text-neutral-500" />
            <input
              id={searchId}
              name="log-filter"
              type="text"
              aria-label="Filter log stream"
              placeholder="Filter log stream..."
              value={logsHook.searchTerm}
              onChange={(e) => logsHook.setSearchTerm(e.target.value)}
              className="pl-8 pr-3 py-1 w-full md:w-48 bg-[#171717] border border-[#262626] rounded-[3px] text-xs font-mono text-neutral-200 placeholder-neutral-500 focus:outline-none focus:border-[#e5a00d]"
            />
          </div>

          <label htmlFor={autoScrollId} className="flex items-center gap-1.5 min-h-[36px] sm:min-h-0 text-xs font-mono text-neutral-400 cursor-pointer select-none">
            <input
              id={autoScrollId}
              name="log-auto-scroll"
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
              className="accent-[#e5a00d] rounded"
            />
            <span>Auto-scroll</span>
          </label>

          <div className="flex items-center gap-1.5 px-2 py-0.5 rounded-[2px] bg-[#141414] border border-[#242424] text-[10px] font-mono">
            <span className={`w-2 h-2 rounded-full ${logsHook.isConnected ? 'bg-emerald-500 animate-pulse' : 'bg-neutral-600'}`} />
            <span className={logsHook.isConnected ? 'text-emerald-400' : 'text-neutral-500'}>
              {logsHook.isConnected ? 'LIVE STREAM' : 'DISCONNECTED'}
            </span>
          </div>
        </div>
      </div>

      </div>
      }
    >
          {filteredLogs.length === 0 && (
            <div className="py-20 text-center text-neutral-500">
              {logs.length === 0
                ? logsHook.isConnected
                  ? 'Connected. No log entries yet; waiting for new activity.'
                  : 'Connecting to the live log stream. If this persists, the stream is unavailable.'
                : 'No log entries match the active filter.'}
            </div>
          )}

          {filteredLogs.map((log, idx) => {
            const lvl = (log.level || '').toUpperCase();
            let lvlColor = 'text-sky-400';
            if (lvl === 'ERROR') lvlColor = 'text-red-400 font-bold';
            else if (lvl === 'WARN' || lvl === 'WARNING') lvlColor = 'text-amber-400 font-bold';
            else if (lvl === 'DEBUG') lvlColor = 'text-neutral-500';

            return (
              <div key={log.id || idx} className="hover:bg-[#121212] px-1 py-0.5 rounded flex flex-wrap sm:flex-nowrap items-start gap-x-2">
                <span className="text-neutral-500 whitespace-nowrap shrink-0">{log.timestamp}</span>
                <span className={`w-14 text-center shrink-0 uppercase ${lvlColor}`}>[{lvl}]</span>
                <span className="text-neutral-400 whitespace-nowrap shrink-0">{log.name}:</span>
                <span className="text-neutral-200 break-all flex-1 min-w-[50%]">{log.message}</span>
              </div>
            );
          })}
    </PageFrame>
  );
};
