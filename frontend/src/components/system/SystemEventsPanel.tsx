import React, { useState } from 'react';
import { RefreshCw, Trash2, Loader2, Search, AlertTriangle, ChevronLeft, ChevronRight } from 'lucide-react';
import { TapeDeckButton, TapeTransportBay, MachinedCard } from '@/components/ui';
import { useSystemEvents } from '@/hooks/useSystemEvents';

const EVENT_TYPES = [
  'scan_started',
  'scan_completed',
  'download_started',
  'download_failed',
  'item_available',
  'backlog_sweep',
  'rss_synced',
  'sync_completed',
  'library_manager_changed',
];

const SeverityBadge: React.FC<{ severity: string }> = ({ severity }) => {
  const s = (severity || 'info').toLowerCase();
  if (s === 'error') {
    return (
      <span className="px-1.5 py-0.5 rounded-[2px] bg-red-950/80 border border-red-800 text-[10px] font-mono uppercase text-red-300">
        ERROR
      </span>
    );
  }
  if (s === 'warn' || s === 'warning') {
    return (
      <span className="px-1.5 py-0.5 rounded-[2px] bg-amber-950/80 border border-amber-800 text-[10px] font-mono uppercase text-amber-300">
        WARN
      </span>
    );
  }
  return (
    <span className="px-1.5 py-0.5 rounded-[2px] bg-neutral-900 border border-neutral-700 text-[10px] font-mono uppercase text-neutral-300">
      INFO
    </span>
  );
};

const EventTypeBadge: React.FC<{ eventType: string }> = ({ eventType }) => {
  let colorClasses = 'bg-[#1a1a1a] border-[#2a2a2a] text-[#e5a00d]';
  if (eventType.includes('completed') || eventType.includes('available')) {
    colorClasses = 'bg-emerald-950/50 border-emerald-800/60 text-emerald-400';
  } else if (eventType.includes('failed')) {
    colorClasses = 'bg-red-950/50 border-red-800/60 text-red-400';
  } else if (eventType.includes('sync')) {
    colorClasses = 'bg-sky-950/50 border-sky-800/60 text-sky-400';
  } else if (eventType.includes('sweep')) {
    colorClasses = 'bg-purple-950/50 border-purple-800/60 text-purple-400';
  }
  return (
    <span className={`px-2 py-0.5 rounded-[2px] border text-[10px] font-mono uppercase whitespace-nowrap ${colorClasses}`}>
      {eventType}
    </span>
  );
};

export const SystemEventsPanel: React.FC = () => {
  const ev = useSystemEvents();
  const [confirmingClear, setConfirmingClear] = useState<boolean>(false);

  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h4 className="text-sm font-bold uppercase font-mono text-white">System Events</h4>
          <p className="text-xs text-neutral-400 font-mono mt-0.5">Recorded lifecycle events, newest first</p>
        </div>
        <TapeTransportBay className="flex items-center gap-2 self-start sm:self-auto">
          <TapeDeckButton
            size="sm"
            onClick={() => void ev.refresh()}
            disabled={ev.isLoading}
            icon={<RefreshCw className={`h-3.5 w-3.5 ${ev.isLoading ? 'animate-spin' : ''}`} />}
          >
            Refresh
          </TapeDeckButton>
          {confirmingClear ? (
            <>
              <TapeDeckButton
                size="sm"
                variant="danger"
                disabled={ev.isClearing}
                onClick={() => {
                  setConfirmingClear(false);
                  void ev.clear();
                }}
                icon={ev.isClearing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
              >
                Confirm Clear
              </TapeDeckButton>
              <TapeDeckButton size="sm" onClick={() => setConfirmingClear(false)}>
                Keep
              </TapeDeckButton>
            </>
          ) : (
            <TapeDeckButton
              size="sm"
              variant="danger"
              onClick={() => setConfirmingClear(true)}
              disabled={ev.isClearing || ev.total === 0}
              icon={<Trash2 className="h-3.5 w-3.5" />}
            >
              Clear Events
            </TapeDeckButton>
          )}
        </TapeTransportBay>
      </div>

      <div className="flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3 p-3 bg-[#101010] border border-[#222222] rounded-[4px]">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            ev.submitSearch();
          }}
          className="flex items-center gap-2 flex-1 md:max-w-md"
        >
          <div className="relative flex-1">
            <Search className="h-3.5 w-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-neutral-500" />
            <input
              type="text"
              placeholder="Filter event message or source..."
              value={ev.searchInput}
              onChange={(e) => ev.setSearchInput(e.target.value)}
              className="w-full pl-9 pr-3 py-1.5 bg-[#171717] border border-[#262626] rounded-[3px] text-xs font-mono text-neutral-200 placeholder-neutral-500 focus:outline-none focus:border-[#e5a00d]"
            />
          </div>
          <TapeDeckButton size="sm" type="submit">
            Search
          </TapeDeckButton>
        </form>

        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs font-mono text-neutral-400">
            <span>Severity:</span>
            <select
              value={ev.severity}
              onChange={(e) => ev.setSeverity(e.target.value)}
              className="bg-[#171717] border border-[#262626] text-neutral-200 text-xs font-mono rounded-[3px] px-2 py-1 focus:outline-none focus:border-[#e5a00d]"
            >
              <option value="all">All</option>
              <option value="info">Info</option>
              <option value="warn">Warning</option>
              <option value="error">Error</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-xs font-mono text-neutral-400">
            <span>Type:</span>
            <select
              value={ev.eventType}
              onChange={(e) => ev.setEventType(e.target.value)}
              className="bg-[#171717] border border-[#262626] text-neutral-200 text-xs font-mono rounded-[3px] px-2 py-1 focus:outline-none focus:border-[#e5a00d]"
            >
              <option value="all">All Types</option>
              {EVENT_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      {ev.error && (
        <div className="p-3 bg-red-950/40 border border-red-800/50 rounded-[4px] text-xs text-red-300 font-mono flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>{ev.error}</span>
        </div>
      )}

      <MachinedCard className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs font-mono">
            <thead>
              <tr className="bg-[#141414] border-b border-[#222222] text-neutral-400 uppercase tracking-wider text-[11px]">
                <th className="py-2.5 px-3 whitespace-nowrap">Timestamp</th>
                <th className="py-2.5 px-3">Type</th>
                <th className="py-2.5 px-3">Severity</th>
                <th className="py-2.5 px-3">Source</th>
                <th className="py-2.5 px-3">Message</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1e1e1e]">
              {ev.isLoading && ev.events.length === 0 && (
                <tr>
                  <td colSpan={5} className="py-12 text-center text-neutral-500">
                    <Loader2 className="h-5 w-5 text-[#e5a00d] animate-spin inline-block mr-2" />
                    Loading system events...
                  </td>
                </tr>
              )}
              {!ev.isLoading && ev.events.length === 0 && (
                <tr>
                  <td colSpan={5} className="py-12 text-center text-neutral-500">
                    {ev.error
                      ? 'System events could not be loaded.'
                      : 'No system lifecycle events recorded matching current filters.'}
                  </td>
                </tr>
              )}
              {ev.events.map((e) => (
                <tr key={e.id} className="hover:bg-[#151515] transition-colors">
                  <td className="py-2 px-3 text-neutral-400 whitespace-nowrap text-[11px]">{e.created_at}</td>
                  <td className="py-2 px-3">
                    <EventTypeBadge eventType={e.event_type} />
                  </td>
                  <td className="py-2 px-3">
                    <SeverityBadge severity={e.severity} />
                  </td>
                  <td className="py-2 px-3 text-neutral-300 font-bold text-[11px] whitespace-nowrap">{e.source}</td>
                  <td className="py-2 px-3 text-neutral-200 break-words max-w-xl">
                    {e.message}
                    {e.details && Object.keys(e.details).length > 0 && (
                      <div className="mt-1 text-[10px] text-neutral-400 bg-[#0d0d0d] p-1.5 rounded border border-[#1f1f1f] overflow-x-auto">
                        {JSON.stringify(e.details)}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="flex flex-col sm:flex-row items-center justify-between gap-2 p-3 bg-[#121212] border-t border-[#222222] text-xs font-mono">
          <span className="text-neutral-400">
            Page {ev.page} of {ev.totalPages} ({ev.total} total events)
          </span>
          <div className="flex items-center gap-2">
            <TapeDeckButton
              size="sm"
              disabled={ev.page <= 1 || ev.isLoading}
              onClick={() => ev.setPage((p) => Math.max(1, p - 1))}
              icon={<ChevronLeft className="h-3 w-3" />}
            >
              Previous
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              disabled={ev.page >= ev.totalPages || ev.isLoading}
              onClick={() => ev.setPage((p) => p + 1)}
              icon={<ChevronRight className="h-3 w-3" />}
            >
              Next
            </TapeDeckButton>
          </div>
        </div>
      </MachinedCard>
    </div>
  );
};
