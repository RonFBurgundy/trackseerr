import React, { useCallback, useMemo, useState } from 'react';
import { Loader2, ScanSearch } from 'lucide-react';
import { ScrollFill, TactileSwitch, TapeDeckButton } from '@/components/ui';
import { ManualImportModal } from '@/components/manualImport';
import { formatDateTime } from '@/components/lists';
import { useLibraryHealth } from '@/hooks/useLibraryHealth';
import type { ManualImportScope } from '@/types/manualImport';
import type { LibraryHealthFinding, LibraryHealthRun } from '@/types/libraryHealth';
import type { ActivityPanelProps } from './ActivityQueuePanel';
import { ReviewMappingCard } from './ReviewMappingCard';
import { ReviewGroupList } from './ReviewGroupList';
import { ReviewWeakMatches } from './ReviewWeakMatches';
import { fileName, weakMatchInfo } from './reviewCauses';

export interface NeedsReviewPanelProps extends ActivityPanelProps {
  /** Called whenever findings may have changed so the nav badge can refresh. */
  onChanged: () => void;
}

const num = (n: number | null): string => (n === null ? '-' : n.toLocaleString());

function serverLabel(kind: string): string {
  return kind.charAt(0).toUpperCase() + kind.slice(1);
}

function statsLine(run: LibraryHealthRun, serverName: string): string {
  return `${num(run.disk_files)} files on disk · ${num(run.server_files)} in ${serverName} · ${num(run.unindexed)} not indexed · ${num(run.stale)} stale`;
}

export const NeedsReviewPanel: React.FC<NeedsReviewPanelProps> = ({ onToast, onChanged }) => {
  const hl = useLibraryHealth({ onChanged, onToast });
  const [importScope, setImportScope] = useState<ManualImportScope | null>(null);
  const { data, loading, error, running, busy } = hl;

  const rematch = useCallback((f: LibraryHealthFinding): void => {
    const info = weakMatchInfo(f);
    setImportScope({ kind: 'files', filePaths: [f.path], title: info.title ?? fileName(f.path) });
  }, []);

  const afterImport = useCallback((): void => {
    void hl.refresh();
    onChanged();
  }, [hl, onChanged]);

  const { regularGroups, weak } = useMemo(
    () => ({
      regularGroups: (data?.groups ?? []).filter((g) => g.kind !== 'weak_match'),
      weak: (data?.findings ?? []).filter((f) => f.kind === 'weak_match'),
    }),
    [data]
  );

  if (loading && !data) {
    return (
      <div className="flex items-center justify-center gap-2 py-10 text-xs font-mono uppercase tracking-widest text-[var(--text-secondary)]">
        <Loader2 className="h-5 w-5 animate-spin text-[var(--accent-amber)]" /> Loading
      </div>
    );
  }
  if (!data) {
    return (
      <p role="alert" className="py-6 text-xs font-mono text-[var(--status-error)]">
        {error ?? 'Library health is unavailable.'}
      </p>
    );
  }

  const { server, last_run: run, mapping } = data;
  const supported = server !== null && server.file_paths;
  const serverName = server ? serverLabel(server.kind) : 'server';
  const empty = regularGroups.length === 0 && weak.length === 0;

  return (
    <section className="flex flex-col gap-3 min-h-0">
      <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-3">
        <div className="min-w-0">
          <h4 className="text-sm font-bold uppercase font-mono text-white">
            Needs review
            <span className="ml-2 text-neutral-500 font-normal">{data.count}</span>
          </h4>
          {server && (
            <p className="text-xs text-neutral-400 font-mono mt-0.5">
              {serverName}
              {run ? ` · last check ${formatDateTime(run.finished_at ?? run.started_at)}` : ' · never checked'}
            </p>
          )}
          {run && supported && <p className="text-[11px] text-neutral-400 font-mono">{statsLine(run, serverName)}</p>}
          {run?.error && (
            <p role="alert" className="text-[11px] font-mono text-[var(--status-error)]">
              Last run failed: {run.error}
            </p>
          )}
        </div>
        {supported && (
          <div className="flex flex-wrap items-center gap-3">
            <TactileSwitch
              label="Check weekly"
              name="library-health-weekly"
              checked={data.weekly}
              disabled={busy}
              onChange={(v) => void hl.setWeekly(v)}
            />
            <TapeDeckButton
              variant="amber"
              size="sm"
              disabled={running}
              onClick={() => void hl.checkNow()}
              icon={running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ScanSearch className="h-3.5 w-3.5" />}
            >
              {running ? 'Checking' : 'Check now'}
            </TapeDeckButton>
          </div>
        )}
      </div>

      {!supported ? (
        <p className="py-6 text-xs font-mono text-[var(--text-muted)]">
          {server === null
            ? 'No media server is connected, so there is nothing to compare your library against.'
            : `${serverName} does not expose file paths, so library checks are not available.`}
        </p>
      ) : (
        <>
          <ReviewMappingCard mapping={mapping} busy={busy} onSave={hl.saveMapping} onRemove={hl.removeMapping} />
          <ScrollFill ariaLabel="Needs review findings" className="min-h-0 pr-1">
            {empty ? (
              <div className="py-10 text-center">
                <p className="text-sm font-mono font-bold text-white">Nothing needs review</p>
                <p className="mt-1 text-xs font-mono text-[var(--text-muted)]">
                  {run ? `Last checked ${formatDateTime(run.finished_at ?? run.started_at)}` : 'Run a check to compare your library.'}
                </p>
              </div>
            ) : (
              <div className="space-y-4">
                {regularGroups.length > 0 && (
                  <ReviewGroupList groups={regularGroups} findings={data.findings} busy={busy} onDismiss={hl.dismiss} />
                )}
                {weak.length > 0 && <ReviewWeakMatches findings={weak} busy={busy} onRematch={rematch} onDismiss={hl.dismiss} />}
              </div>
            )}
          </ScrollFill>
        </>
      )}

      <ManualImportModal scope={importScope} onClose={() => setImportScope(null)} onImported={afterImport} />
    </section>
  );
};
