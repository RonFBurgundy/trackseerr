import React from 'react';
import { EyeOff, RefreshCw } from 'lucide-react';
import { MachinedCard, TapeDeckButton } from '@/components/ui';
import type { LibraryHealthFinding } from '@/types/libraryHealth';
import { causeLabel, fileName, weakMatchInfo } from './reviewCauses';

export interface ReviewWeakMatchesProps {
  findings: readonly LibraryHealthFinding[];
  busy: boolean;
  onRematch: (finding: LibraryHealthFinding) => void;
  onDismiss: (path: string, scope: 'file' | 'folder') => Promise<void>;
}

export const ReviewWeakMatches: React.FC<ReviewWeakMatchesProps> = React.memo(({ findings, busy, onRematch, onDismiss }) => (
  <section aria-label={causeLabel('weak_tag_match')} className="space-y-2">
    <h5 className="text-[11px] font-bold uppercase tracking-widest font-mono text-[var(--text-secondary)]">
      {causeLabel('weak_tag_match')}
      <span className="ml-2 font-normal text-[var(--text-muted)]">{findings.length}</span>
    </h5>
    {findings.map((f) => {
      const info = weakMatchInfo(f);
      const title = info.title ?? fileName(f.path);
      return (
        <MachinedCard key={f.id} className="p-2.5 flex flex-col sm:flex-row sm:items-center gap-2">
          <div className="min-w-0 flex-1">
            <p className="truncate text-xs font-mono text-[var(--text-primary)]">
              {title}
              {info.sourceName && <span className="text-[var(--text-secondary)]"> · {info.sourceName}</span>}
              {info.strength && <span className="text-[var(--text-muted)]"> ({info.strength})</span>}
            </p>
            <p className="truncate text-[11px] font-mono text-[var(--text-muted)]" title={f.path}>
              {f.path}
            </p>
          </div>
          <div className="flex gap-1.5">
            <TapeDeckButton size="sm" variant="amber" disabled={busy} onClick={() => onRematch(f)} icon={<RefreshCw className="h-3.5 w-3.5" />}>
              Re-match
            </TapeDeckButton>
            <TapeDeckButton
              size="sm"
              disabled={busy}
              onClick={() => void onDismiss(f.path, 'file')}
              icon={<EyeOff className="h-3.5 w-3.5" />}
              aria-label={`Dismiss file ${f.path}`}
            >
              Dismiss
            </TapeDeckButton>
          </div>
        </MachinedCard>
      );
    })}
  </section>
));
ReviewWeakMatches.displayName = 'ReviewWeakMatches';
