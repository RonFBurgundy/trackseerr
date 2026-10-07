import React from 'react';
import { Loader2, Play, Square, RotateCw, AlertTriangle } from 'lucide-react';
import { TapeDeckButton, ActionBar, MachinedCard } from '@/components/ui';
import { PageActionsPortal } from '@/components/layout';
import { useScheduledTasks } from '@/hooks/useScheduledTasks';
import { formatTimestamp } from './formatters';

export interface SystemTasksPanelProps {
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

export const SystemTasksPanel: React.FC<SystemTasksPanelProps> = ({ onToast }) => {
  const { tasks, isLoading, error, runningIds, cancellingIds, refresh, run, cancel } = useScheduledTasks(onToast);

  return (
    <div className="space-y-4">
      <PageActionsPortal>
        <div className="flex items-center justify-end gap-2">
          <TapeDeckButton
            size="sm"
            onClick={() => void refresh()}
            disabled={isLoading}
            collapseLabel
            aria-label="Refresh tasks"
            title="Refresh tasks"
            icon={isLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCw className="h-3.5 w-3.5" />}
          >
            Refresh
          </TapeDeckButton>
        </div>
      </PageActionsPortal>
      <div>
        <div className="min-w-0">
          <h4 className="text-sm font-bold uppercase font-mono text-white">Scheduled Tasks &amp; Background Workers</h4>
          <p className="text-xs text-neutral-400 font-mono mt-0.5">
            Monitor recurring automation timers, intervals, and trigger on-demand sweeps
          </p>
        </div>
      </div>

      {error && (
        <div className="p-3 bg-red-950/40 border border-red-800/50 rounded-[4px] text-xs text-red-300 font-mono flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <MachinedCard className="overflow-hidden p-0 border-[#222222]">
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse max-md:block">
            <thead className="max-md:hidden">
              <tr className="border-b border-[#222222] bg-[#121212] text-[11px] font-mono uppercase tracking-wider text-neutral-400">
                <th className="py-2 px-3 md:py-3 md:px-4">Task</th>
                <th className="py-2 px-3 md:py-3 md:px-4">Interval</th>
                <th className="py-2 px-3 md:py-3 md:px-4">Status</th>
                <th className="py-2 px-3 md:py-3 md:px-4">Last Run</th>
                <th className="py-2 px-3 md:py-3 md:px-4 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1c1c1c] text-xs font-mono max-md:block">
              {tasks.map((task) => {
                const isRunning = task.status === 'running' || runningIds.has(task.id);
                const isCancelling = cancellingIds.has(task.id);
                return (
                  <tr key={task.id} className="hover:bg-[#141414] transition-colors max-md:flex max-md:flex-col max-md:gap-2.5 max-md:p-4">
                    <td className="py-2 px-3 md:py-3.5 md:px-4 min-w-[200px] max-md:min-w-0 max-md:p-0">
                      <span className="font-bold text-sm text-white block">{task.name}</span>
                      <span className="text-[11px] text-neutral-400 block mt-0.5">{task.description}</span>
                    </td>
                    <td data-label="Interval" className="py-2 px-3 md:py-3.5 md:px-4 whitespace-nowrap max-md:flex max-md:items-center max-md:justify-between max-md:p-0 before:content-[attr(data-label)] before:text-[10px] before:uppercase before:tracking-wider before:text-neutral-500 md:before:hidden">
                      <span className="px-2 py-0.5 rounded-[2px] bg-[#181818] text-[10px] text-neutral-300 border border-[#282828]">
                        {task.interval}
                      </span>
                    </td>
                    <td data-label="Status" className="py-2 px-3 md:py-3.5 md:px-4 whitespace-nowrap max-md:flex max-md:items-center max-md:justify-between max-md:p-0 before:content-[attr(data-label)] before:text-[10px] before:uppercase before:tracking-wider before:text-neutral-500 md:before:hidden">
                      {task.status === 'running' ? (
                        <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[2px] bg-[#e5a00d]/10 text-[#e5a00d] border border-[#e5a00d]/30 font-bold text-[10px] uppercase">
                          <Loader2 className="h-3 w-3 animate-spin" />
                          Running
                        </span>
                      ) : task.status === 'failed' ? (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-[2px] bg-red-950/40 text-red-400 border border-red-800/40 font-bold text-[10px] uppercase">
                          Failed
                        </span>
                      ) : task.status === 'paused' ? (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-[2px] bg-yellow-950/40 text-yellow-400 border border-yellow-800/40 font-bold text-[10px] uppercase">
                          Paused
                        </span>
                      ) : (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-[2px] bg-neutral-800/80 text-neutral-400 border border-neutral-700 font-bold text-[10px] uppercase">
                          Idle
                        </span>
                      )}
                    </td>
                    <td data-label="Last run" className="py-2 px-3 md:py-3.5 md:px-4 whitespace-nowrap text-neutral-400 max-md:flex max-md:items-center max-md:justify-between max-md:p-0 before:content-[attr(data-label)] before:text-[10px] before:uppercase before:tracking-wider before:text-neutral-500 md:before:hidden">
                      {task.last_run_at ? formatTimestamp(task.last_run_at) : 'Never'}
                    </td>
                    <td className="py-2 px-3 md:py-3.5 md:px-4 whitespace-nowrap text-right max-md:p-0">
                      <ActionBar align="end">
                        <TapeDeckButton
                          size="sm"
                          variant="amber"
                          disabled={isRunning || isCancelling}
                          onClick={() => void run(task.id)}
                          icon={
                            runningIds.has(task.id) ? (
                              <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : (
                              <Play className="h-3.5 w-3.5" />
                            )
                          }
                        >
                          Run Now
                        </TapeDeckButton>
                        {task.can_cancel && task.status === 'running' && (
                          <TapeDeckButton
                            size="sm"
                            variant="danger"
                            disabled={isCancelling}
                            onClick={() => void cancel(task.id)}
                            icon={
                              isCancelling ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                              ) : (
                                <Square className="h-3.5 w-3.5" />
                              )
                            }
                          >
                            Cancel
                          </TapeDeckButton>
                        )}
                      </ActionBar>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {isLoading && tasks.length === 0 && (
          <div className="flex items-center justify-center gap-2 py-12 text-neutral-400 text-xs font-mono">
            <Loader2 className="h-4 w-4 animate-spin text-[#e5a00d]" /> Loading scheduled tasks...
          </div>
        )}
        {!isLoading && tasks.length === 0 && (
          <div className="text-center py-12 text-neutral-500 font-mono text-sm">
            {error ? 'Scheduled tasks could not be loaded.' : 'No scheduled background tasks registered.'}
          </div>
        )}
      </MachinedCard>
    </div>
  );
};
