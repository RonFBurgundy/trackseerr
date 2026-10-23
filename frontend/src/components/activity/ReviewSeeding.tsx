import React, { useState } from 'react';
import { ItemCard, formatDateTime, type FlatListColumn } from '@/components/lists';
import type { SeedingItem } from '@/types/seedCleanup';
import { SeedingLine } from './SeedingLine';

export interface ReviewSeedingProps {
  items: readonly SeedingItem[];
}

const itemName = (item: SeedingItem): string => (item.artist ? `${item.artist} – ${item.title}` : item.title);

const itemDetails = (item: SeedingItem): string =>
  [
    item.client,
    item.indexer,
    item.rule_source === 'indexer' ? 'indexer seed rule' : 'global seed limits',
    item.removed_at ? `removed ${formatDateTime(item.removed_at)}` : null,
  ]
    .filter((part): part is string => Boolean(part))
    .join(' · ');

export const ReviewSeeding: React.FC<ReviewSeedingProps> = React.memo(({ items }) => {
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
    <section aria-label="Still seeding" className="space-y-2">
      <h5 className="text-[11px] font-bold uppercase tracking-widest font-mono text-[var(--text-secondary)]">
        Still seeding
        <span className="ml-2 font-normal text-[var(--text-muted)]">{items.length}</span>
      </h5>
      <p className="text-[11px] font-mono text-[var(--text-muted)]">
        Removed from the queue but still seeding. Seed cleanup removes each one when its seed goal is met.
      </p>
      <div className="rounded-[4px] border border-[#222222] overflow-hidden">
        {items.map((item) => {
          const titleCol: FlatListColumn<SeedingItem> = {
            key: 'title',
            label: 'Title',
            render: () => itemName(item),
          };
          const metaCols: FlatListColumn<SeedingItem>[] = [
            {
              key: 'meta',
              label: 'Seeding',
              render: () => <SeedingLine seeding={item.seeding} />,
            },
          ];
          return (
            <ItemCard<SeedingItem>
              key={item.id}
              item={item}
              title={titleCol}
              meta={metaCols}
              details={<span className="break-all">{itemDetails(item)}</span>}
              expanded={expandedIds.has(String(item.id))}
              onToggleExpand={() => toggleExpand(String(item.id))}
              menuLabel={`Details for ${itemName(item)}`}
            />
          );
        })}
      </div>
    </section>
  );
});
ReviewSeeding.displayName = 'ReviewSeeding';
