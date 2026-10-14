import React, { useState } from 'react';
import { RotateCcw } from 'lucide-react';
import type { OverflowMenuItem } from '@/components/ui';
import { ItemCard, type FlatListColumn } from '@/components/lists';
import type { LibraryHealthFinding } from '@/types/libraryHealth';
import { failedInfo } from './seedCleanupInfo';

export interface ReviewCleanupFailedProps {
  findings: readonly LibraryHealthFinding[];
  busy: boolean;
  onRetry: (findingId: string) => Promise<void>;
}

export const ReviewCleanupFailed: React.FC<ReviewCleanupFailedProps> = React.memo(({ findings, busy, onRetry }) => {
  const [expandedIds, setExpandedIds] = useState<ReadonlySet<string>>(() => new Set());

  const toggleExpand = (id: string): void => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  return (
    <section aria-label="Cleanup failed" className="space-y-2">
      <h5 className="text-[11px] font-bold uppercase tracking-widest font-mono text-[var(--text-secondary)]">
        Cleanup failed
        <span className="ml-2 font-normal text-[var(--text-muted)]">{findings.length}</span>
      </h5>
      <div className="rounded-[4px] border border-[#222222] overflow-hidden">
        {findings.map((f) => {
          const info = failedInfo(f);

          const titleCol: FlatListColumn<LibraryHealthFinding> = {
            key: 'title',
            label: 'Title',
            render: () => info.title,
          };

          const metaCols: FlatListColumn<LibraryHealthFinding>[] =
            info.attempts !== null
              ? [
                  {
                    key: 'attempts',
                    label: 'Attempts',
                    render: () => `${info.attempts} ${info.attempts === 1 ? 'attempt' : 'attempts'}`,
                  },
                ]
              : [];

          const menu: OverflowMenuItem[] = [
            {
              key: 'retry',
              label: 'Retry',
              icon: <RotateCcw className="h-3.5 w-3.5" />,
              highlight: true,
              disabled: busy,
              onSelect: () => void onRetry(f.id),
            },
          ];

          return (
            <ItemCard
              key={f.id}
              item={f}
              title={titleCol}
              meta={metaCols}
              details={
                info.error ? (
                  <span className="text-[var(--status-error)] break-words">{info.error}</span>
                ) : null
              }
              expanded={expandedIds.has(f.id)}
              onToggleExpand={() => toggleExpand(f.id)}
              attention={{ label: 'Retry', onSelect: () => void onRetry(f.id) }}
              menu={menu}
              menuLabel={`Actions for ${info.title}`}
            />
          );
        })}
      </div>
    </section>
  );
});
ReviewCleanupFailed.displayName = 'ReviewCleanupFailed';

