import React from 'react';
import { Loader2, Eraser } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';
import { formatDateTime } from '@/components/lists';
import type { SeedCleanupStatus } from '@/types/seedCleanup';

export interface SeedCleanupStripProps {
  status: SeedCleanupStatus | null;
  running: boolean;
  onRun: () => void;
}

export const SeedCleanupStrip: React.FC<SeedCleanupStripProps> = React.memo(({ status, running, onRun }) => {
  const run = status?.last_run ?? null;
  return (
    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 rounded-[4px] border border-[var(--border-default)] bg-[var(--bg-surface)] px-3 py-2">
      <div className="min-w-0">
        <h5 className="text-[11px] font-bold uppercase tracking-widest font-mono text-[var(--text-secondary)]">Seed cleanup</h5>
        {run ? (
          <>
            <p className="text-[11px] font-mono text-[var(--text-muted)]">
              Last run {formatDateTime(run.finished_at ?? run.started_at)}
              {run.finished_at === null ? ' (in progress)' : ''}
            </p>
            <p className="text-[11px] font-mono text-[var(--text-secondary)]">
              {run.stats.evaluated} evaluated · {run.stats.removed} removed · {run.stats.deleted_files} files deleted ·{' '}
              {run.stats.orphans} orphans · {run.stats.failures} failures
            </p>
            {run.error && (
              <p role="alert" className="text-[11px] font-mono text-[var(--status-error)]">
                Last run failed: {run.error}
              </p>
            )}
          </>
        ) : (
          <p className="text-[11px] font-mono text-[var(--text-muted)]">Never run</p>
        )}
      </div>
      <TapeDeckButton
        variant="amber"
        size="sm"
        disabled={running}
        onClick={onRun}
        icon={running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Eraser className="h-3.5 w-3.5" />}
      >
        {running ? 'Running' : 'Run seed cleanup'}
      </TapeDeckButton>
    </div>
  );
});
SeedCleanupStrip.displayName = 'SeedCleanupStrip';
