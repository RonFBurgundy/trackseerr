import React, { forwardRef } from 'react';
import { ArrowRight } from 'lucide-react';
import { useNow } from '@/hooks/useNow';
import type { SystemActivityRecent, SystemActivityRunning } from '@/types/models';
import { parseServerTimestamp, relativeTime } from '@/components/issues/issueFormat';
import { TaskStatusBadge } from '@/components/system/TaskStatusBadge';
import { formatElapsed } from '@/components/system/taskFormat';

export interface BrandActivityPopoverProps {
  id: string;
  running: SystemActivityRunning[];
  recent: SystemActivityRecent[];
  /** Fixed placement computed by the anchor: a full-width sheet on phones, a 320px panel on desktop. */
  style: React.CSSProperties;
  isSheet: boolean;
  onOpenTasks: () => void;
}

function progressText(item: SystemActivityRunning): string | null {
  const p = item.progress;
  if (!p) return null;
  if (p.total != null && p.total > 0) return `${p.current ?? 0}/${p.total}${p.message ? ` · ${p.message}` : ''}`;
  return p.message ?? null;
}

/** Running tasks (live elapsed) and the five most recent finished runs, with a link to Settings > System > Tasks. */
export const BrandActivityPopover = forwardRef<HTMLDivElement, BrandActivityPopoverProps>(
  ({ id, running, recent, style, isSheet, onOpenTasks }, ref) => {
    const now = useNow(1000, running.length > 0);
    return (
      <div
        ref={ref}
        id={id}
        role="dialog"
        aria-label="Background task activity"
        tabIndex={-1}
        style={style}
        className={`fixed z-50 bg-[#121212] border-[#2a2a2a] shadow-2xl outline-none overflow-y-auto ${
          isSheet ? 'border-y max-h-[70dvh]' : 'border rounded-[4px] w-80 max-h-[70dvh]'
        }`}
      >
        <section className="p-3" aria-label="Running tasks">
          <h3 className="text-[10px] font-mono font-bold uppercase tracking-wider text-neutral-500">Running</h3>
          {running.length === 0 ? (
            <p className="mt-1 text-xs font-mono text-neutral-500">Nothing running.</p>
          ) : (
            <ul className="mt-1 space-y-2">
              {running.map((item) => {
                const started = parseServerTimestamp(item.started_at);
                const detail = progressText(item);
                return (
                  <li key={item.task_id} className="min-w-0">
                    <div className="flex items-center justify-between gap-2">
                      <span className="min-w-0 truncate text-xs font-mono font-bold text-white">{item.name}</span>
                      <span className="shrink-0 text-[11px] font-mono tabular-nums text-[#e5a00d]">
                        {started !== null ? formatElapsed(now - started) : '—'}
                      </span>
                    </div>
                    {detail && <p className="truncate text-[11px] font-mono text-neutral-400">{detail}</p>}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
        <section className="p-3 border-t border-[#1f1f1f]" aria-label="Recent runs">
          <h3 className="text-[10px] font-mono font-bold uppercase tracking-wider text-neutral-500">Recent</h3>
          {recent.length === 0 ? (
            <p className="mt-1 text-xs font-mono text-neutral-500">No runs yet.</p>
          ) : (
            <ul className="mt-1 space-y-1.5">
              {recent.map((item, i) => (
                <li key={`${item.task_id}-${item.finished_at ?? i}`} className="flex items-center justify-between gap-2 min-w-0">
                  <span className="min-w-0 truncate text-xs font-mono text-neutral-200">{item.name}</span>
                  <span className="flex shrink-0 items-center gap-2">
                    <TaskStatusBadge status={item.status} />
                    <span className="text-[11px] font-mono text-neutral-500">{relativeTime(item.finished_at, now)}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
        <div className="p-3 border-t border-[#1f1f1f]">
          <button
            type="button"
            onClick={onOpenTasks}
            className="inline-flex items-center gap-1.5 min-h-[36px] text-xs font-mono font-bold uppercase tracking-wider text-[#e5a00d] hover:text-white focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--accent-amber)]"
          >
            Open Tasks
            <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </div>
      </div>
    );
  }
);
BrandActivityPopover.displayName = 'BrandActivityPopover';
