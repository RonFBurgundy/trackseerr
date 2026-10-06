import React from 'react';
import { RotateCcw } from 'lucide-react';
import { MachinedCard, TapeDeckButton } from '@/components/ui';
import type { LibraryHealthFinding } from '@/types/libraryHealth';
import { failedInfo } from './seedCleanupInfo';

export interface ReviewCleanupFailedProps {
  findings: readonly LibraryHealthFinding[];
  busy: boolean;
  onRetry: (findingId: string) => Promise<void>;
}

export const ReviewCleanupFailed: React.FC<ReviewCleanupFailedProps> = React.memo(({ findings, busy, onRetry }) => (
  <section aria-label="Cleanup failed" className="space-y-2">
    <h5 className="text-[11px] font-bold uppercase tracking-widest font-mono text-[var(--text-secondary)]">
      Cleanup failed
      <span className="ml-2 font-normal text-[var(--text-muted)]">{findings.length}</span>
    </h5>
    {findings.map((f) => {
      const info = failedInfo(f);
      return (
        <MachinedCard key={f.id} className="p-2.5 flex flex-col sm:flex-row sm:items-center gap-2">
          <div className="min-w-0 flex-1">
            <p className="truncate text-xs font-mono text-[var(--text-primary)]" title={info.title}>
              {info.title}
            </p>
            {info.error && <p className="text-[11px] font-mono text-[var(--status-error)] break-words">{info.error}</p>}
            {info.attempts !== null && (
              <p className="text-[11px] font-mono text-[var(--text-muted)]">
                {info.attempts} {info.attempts === 1 ? 'attempt' : 'attempts'}
              </p>
            )}
          </div>
          <TapeDeckButton
            size="sm"
            variant="amber"
            disabled={busy}
            onClick={() => void onRetry(f.id)}
            icon={<RotateCcw className="h-3.5 w-3.5" />}
            aria-label={`Retry cleanup for ${info.title}`}
          >
            Retry
          </TapeDeckButton>
        </MachinedCard>
      );
    })}
  </section>
));
ReviewCleanupFailed.displayName = 'ReviewCleanupFailed';
