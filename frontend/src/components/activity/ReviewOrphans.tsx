import React, { useState } from 'react';
import { Trash2 } from 'lucide-react';
import { ConfirmDialog, MachinedCard, TapeDeckButton } from '@/components/ui';
import { formatBytes } from '@/components/lists';
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
      {findings.map((f) => {
        const info = orphanInfo(f);
        return (
          <MachinedCard key={f.id} className="p-2.5 flex flex-col lg:flex-row lg:items-center gap-2">
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-mono text-[var(--text-primary)]" title={info.name}>
                {info.name}
              </p>
              <p className="text-[11px] font-mono text-[var(--text-secondary)]">
                {info.size === null ? '-' : formatBytes(info.size)} · ratio {info.ratio === null ? '-' : info.ratio.toFixed(2)} · seeded{' '}
                {formatSeedTime(info.seedingSeconds)}
              </p>
              <p className="truncate text-[11px] font-mono text-[var(--text-muted)]" title={info.path}>
                {info.path}
              </p>
            </div>
            <div className="flex flex-wrap gap-1.5">
              <TapeDeckButton
                size="sm"
                disabled={busy}
                onClick={() => setPending({ id: f.id, name: info.name, deleteFiles: false })}
                icon={<Trash2 className="h-3.5 w-3.5" />}
                aria-label={`Remove torrent ${info.name}`}
              >
                Remove torrent
              </TapeDeckButton>
              <TapeDeckButton
                size="sm"
                variant="amber"
                disabled={busy}
                onClick={() => setPending({ id: f.id, name: info.name, deleteFiles: true })}
                icon={<Trash2 className="h-3.5 w-3.5" />}
                aria-label={`Remove torrent and files for ${info.name}`}
              >
                Remove torrent + files
              </TapeDeckButton>
            </div>
          </MachinedCard>
        );
      })}
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
