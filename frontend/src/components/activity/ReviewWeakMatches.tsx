import React, { useState } from 'react';
import { EyeOff, RefreshCw } from 'lucide-react';
import type { OverflowMenuItem } from '@/components/ui';
import { ItemCard, type FlatListColumn } from '@/components/lists';
import type { LibraryHealthFinding } from '@/types/libraryHealth';
import { causeLabel, fileName, weakMatchInfo } from './reviewCauses';

export interface ReviewWeakMatchesProps {
  findings: readonly LibraryHealthFinding[];
  busy: boolean;
  onRematch: (finding: LibraryHealthFinding) => void;
  onDismiss: (path: string, scope: 'file' | 'folder') => Promise<void>;
}

export const ReviewWeakMatches: React.FC<ReviewWeakMatchesProps> = React.memo(({ findings, busy, onRematch, onDismiss }) => {
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
    <section aria-label={causeLabel('weak_tag_match')} className="space-y-2">
      <h5 className="text-[11px] font-bold uppercase tracking-widest font-mono text-[var(--text-secondary)]">
        {causeLabel('weak_tag_match')}
        <span className="ml-2 font-normal text-[var(--text-muted)]">{findings.length}</span>
      </h5>
      <div className="rounded-[4px] border border-[#222222] overflow-hidden">
        {findings.map((f) => {
          const info = weakMatchInfo(f);
          const titleText = info.title ?? fileName(f.path);

          const titleCol: FlatListColumn<LibraryHealthFinding> = {
            key: 'title',
            label: 'Title',
            render: () => titleText,
          };

          const subCols: FlatListColumn<LibraryHealthFinding>[] = info.sourceName
            ? [
                {
                  key: 'source',
                  label: 'Source',
                  render: () => info.sourceName,
                },
              ]
            : [];

          const metaCols: FlatListColumn<LibraryHealthFinding>[] = info.strength
            ? [
                {
                  key: 'strength',
                  label: 'Strength',
                  render: () => `(${info.strength})`,
                },
              ]
            : [];

          const menu: OverflowMenuItem[] = [
            {
              key: 'rematch',
              label: 'Re-match',
              icon: <RefreshCw className="h-3.5 w-3.5" />,
              highlight: true,
              disabled: busy,
              onSelect: () => onRematch(f),
            },
            {
              key: 'dismiss',
              label: 'Dismiss',
              icon: <EyeOff className="h-3.5 w-3.5" />,
              disabled: busy,
              onSelect: () => void onDismiss(f.path, 'file'),
            },
          ];

          return (
            <ItemCard
              key={f.id}
              item={f}
              title={titleCol}
              sub={subCols}
              meta={metaCols}
              details={<span className="break-all">{f.path}</span>}
              expanded={expandedIds.has(f.id)}
              onToggleExpand={() => toggleExpand(f.id)}
              menu={menu}
              menuLabel={`Actions for ${titleText}`}
            />
          );
        })}
      </div>
    </section>
  );
});
ReviewWeakMatches.displayName = 'ReviewWeakMatches';

