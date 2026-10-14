import React, { useState } from 'react';
import { Trash2 } from 'lucide-react';
import { ConfirmDialog, type OverflowMenuItem } from '@/components/ui';
import { ItemCard, formatBytes, type FlatListColumn } from '@/components/lists';
import type { LibraryHealthFinding } from '@/types/libraryHealth';
import { formatSeedTime, orphanInfo } from './seedCleanupInfo';

export interface ReviewOrphansProps {
  findings: readonly LibraryHealthFinding[];
  busy: boolean;
  onRemove: (findingId: string, deleteFiles: boolean) => Promise<void>;
}

interface Pending {
  id: string;
  name: string;
  deleteFiles: boolean;
}

export const ReviewOrphans: React.FC<ReviewOrphansProps> = React.memo(({ findings, busy, onRemove }) => {
  const [pending, setPending] = useState<Pending | null>(null);
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

  const confirm = (): void => {
    if (!pending) return;
    const { id, deleteFiles } = pending;
    setPending(null);
    void onRemove(id, deleteFiles);
  };

  return (
    <section aria-label="Orphaned torrents" className="space-y-2">
      <h5 className="text-[11px] font-bold uppercase tracking-widest font-mono text-[var(--text-secondary)]">
        Orphaned torrents
        <span className="ml-2 font-normal text-[var(--text-muted)]">{findings.length}</span>
      </h5>
      <p className="text-[11px] font-mono text-[var(--text-muted)]">
        Torrents in your client that TrackSeerr did not grab or no longer tracks.
      </p>
      <div className="rounded-[4px] border border-[#222222] overflow-hidden">
        {findings.map((f) => {
          const info = orphanInfo(f);

          const titleCol: FlatListColumn<LibraryHealthFinding> = {
            key: 'title',
            label: 'Title',
            render: () => info.name,
          };

          const metaCols: FlatListColumn<LibraryHealthFinding>[] = [
            {
              key: 'meta',
              label: 'Stats',
              render: () => (
                <>
                  {info.size === null ? '-' : formatBytes(info.size)} &middot; ratio {info.ratio === null ? '-' : info.ratio.toFixed(2)} &middot; seeded{' '}
                  {formatSeedTime(info.seedingSeconds)}
                </>
              ),
            },
          ];

          const menu: OverflowMenuItem[] = [
            {
              key: 'remove',
              label: 'Remove torrent…',
              icon: <Trash2 className="h-3.5 w-3.5" />,
              disabled: busy,
              onSelect: () => setPending({ id: f.id, name: info.name, deleteFiles: false }),
            },
            {
              key: 'remove-files',
              label: 'Remove torrent + files…',
              icon: <Trash2 className="h-3.5 w-3.5" />,
              highlight: true,
              disabled: busy,
              onSelect: () => setPending({ id: f.id, name: info.name, deleteFiles: true }),
            },
          ];

          return (
            <ItemCard
              key={f.id}
              item={f}
              title={titleCol}
              meta={metaCols}
              details={<span className="break-all">{info.path}</span>}
              expanded={expandedIds.has(f.id)}
              onToggleExpand={() => toggleExpand(f.id)}
              menu={menu}
              menuLabel={`Actions for ${info.name}`}
            />
          );
        })}
      </div>
      <ConfirmDialog
        isOpen={pending !== null}
        title={pending?.deleteFiles ? 'Remove torrent and files' : 'Remove torrent'}
        confirmLabel={pending?.deleteFiles ? 'Remove torrent + files' : 'Remove torrent'}
        onConfirm={confirm}
        onCancel={() => setPending(null)}
        busy={busy}
      >
        <p className="text-xs font-mono text-[var(--text-secondary)]">
          {pending?.deleteFiles
            ? `This removes '${pending.name}' from your torrent client and permanently deletes its files from the download folder.`
            : `This removes '${pending?.name ?? ''}' from your torrent client. Its files stay on disk.`}
        </p>
      </ConfirmDialog>
    </section>
  );
});
ReviewOrphans.displayName = 'ReviewOrphans';
