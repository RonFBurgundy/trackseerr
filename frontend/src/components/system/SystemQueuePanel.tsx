import React from 'react';
import { Loader2, AlertTriangle } from 'lucide-react';
import { MachinedCard, ScrollFill } from '@/components/ui';
import { useSystemQueue } from '@/hooks/useSystemQueue';
import type { SystemJob, SystemJobState } from '@/types/models';
import { formatDuration, formatTimestamp } from './formatters';

const STATE_STYLE: Record<SystemJobState, string> = {
  running: 'bg-[#e5a00d]/10 text-[#e5a00d] border-[#e5a00d]/30',
  queued: 'bg-neutral-800/80 text-neutral-300 border-neutral-700',
  completed: 'bg-emerald-950/40 text-emerald-400 border-emerald-800/40',
  failed: 'bg-red-950/40 text-red-400 border-red-800/40',
  cancelled: 'bg-yellow-950/40 text-yellow-400 border-yellow-800/40',
};

/** Flat list of running, then queued, then recent jobs; polls while mounted. */
export const SystemQueuePanel: React.FC = () => {
  const { queue, isLoading, error } = useSystemQueue();
  const rows: SystemJob[] = queue ? [...queue.running, ...queue.queued, ...queue.recent] : [];

  return (
    <div className="space-y-4">
      <div>
        <h4 className="text-sm font-bold uppercase font-mono text-white">Job Queue</h4>
        <p className="text-xs text-neutral-400 font-mono mt-0.5">
          Running and queued background jobs, plus the last 50 completed. Refreshes every few seconds.
        </p>
      </div>

      {error && (
        <div className="p-3 bg-red-950/40 border border-red-800/50 rounded-[4px] text-xs text-red-300 font-mono flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <MachinedCard className="overflow-hidden p-0">
        <ScrollFill ariaLabel="Background job queue" className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs font-mono">
            <thead>
              <tr className="border-b border-[#222222] bg-[#121212] text-[11px] uppercase tracking-wider text-neutral-400">
                <th className="py-2 px-3 md:py-3 md:px-4">Name</th>
                <th className="py-2 px-3 md:py-3 md:px-4">State</th>
                <th className="py-2 px-3 md:py-3 md:px-4">Started</th>
                <th className="py-2 px-3 md:py-3 md:px-4">Duration</th>
                <th className="py-2 px-3 md:py-3 md:px-4">Message</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1c1c1c]">
              {rows.map((job) => (
                <tr key={job.id} className="hover:bg-[#141414] transition-colors">
                  <td className="py-2 px-3 md:py-3 md:px-4 text-white font-bold min-w-[160px]">{job.name}</td>
                  <td className="py-2 px-3 md:py-3 md:px-4 whitespace-nowrap">
                    <span
                      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[2px] border text-[10px] font-bold uppercase ${STATE_STYLE[job.state]}`}
                    >
                      {job.state === 'running' && <Loader2 className="h-3 w-3 animate-spin" />}
                      {job.state}
                    </span>
                  </td>
                  <td className="py-2 px-3 md:py-3 md:px-4 whitespace-nowrap text-neutral-400">{formatTimestamp(job.started_at)}</td>
                  <td className="py-2 px-3 md:py-3 md:px-4 whitespace-nowrap text-neutral-400">{formatDuration(job.duration_ms)}</td>
                  <td className="py-2 px-3 md:py-3 md:px-4 text-neutral-300 break-words max-w-md">{job.message || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollFill>

        {isLoading && rows.length === 0 && (
          <div className="flex items-center justify-center gap-2 py-12 text-neutral-400 text-xs font-mono">
            <Loader2 className="h-4 w-4 animate-spin text-[#e5a00d]" /> Loading job queue...
          </div>
        )}
        {!isLoading && rows.length === 0 && !error && (
          <div className="text-center py-12 text-neutral-500 font-mono text-sm">
            No jobs running or queued, and nothing has run since the server started.
          </div>
        )}
        {!isLoading && rows.length === 0 && error && (
          <div className="text-center py-12 text-neutral-500 font-mono text-sm">
            Job queue unavailable. Retrying automatically.
          </div>
        )}
      </MachinedCard>
    </div>
  );
};
