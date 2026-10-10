import React, { useId, useState } from 'react';
import { MessageSquare } from 'lucide-react';
import { ConfirmDialog, MachinedCard, SearchBar, CassetteLoader } from '@/components/ui';
import { PageActionsPortal } from '@/components/layout';
import { ManualImportModal } from '@/components/manualImport';
import type { ManualImportScope } from '@/types/manualImport';
import { useIssueQueue } from '@/hooks/useIssueQueue';
import type { IssueStatusFilter, IssueTypeFilter } from '@/hooks/useIssueQueue';
import { notifyIssuesChanged, updateIssue } from '@/services/issueService';
import { errorMessage } from '@/services/apiClient';
import type { Issue, IssueRematchResult, IssueStatus, IssueType } from '@/types/models';
import { ISSUE_STATUS_LABELS, ISSUE_TYPE_LABELS } from '@/types/models';
import { IssueDetailModal } from './IssueDetailModal';
import { IssueStatusChip } from './IssueStatusChip';
import { relativeTime } from './issueFormat';

const selectClass =
  'min-h-[36px] px-2 rounded-[3px] bg-[var(--bg-surface-elevated)] border border-[var(--border-default)] text-[13px] text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-amber)] min-w-0';

const STATUS_FILTERS: readonly IssueStatus[] = ['open', 'in_progress', 'resolved', 'wont_fix'];
const TYPE_FILTERS: readonly IssueType[] = [
  'audio_quality',
  'corrupted_file',
  'wrong_release',
  'missing_tracks',
  'incorrect_tags',
  'request_stuck',
  'other',
];

export interface AdminIssuesPanelProps {
  /** Issue open on top of the queue, from the route (`#/requests/issues/<id>`). */
  issueId?: string;
  onOpenIssue: (id: string) => void;
  onCloseIssue: () => void;
  onToast: (message: string, tone?: 'ok' | 'error') => void;
}

/** Admin issue queue (Requests > Issues). The open issue lives in the URL so Back closes it. */
export const AdminIssuesPanel: React.FC<AdminIssuesPanelProps> = ({ issueId, onOpenIssue, onCloseIssue, onToast }) => {
  const queue = useIssueQueue(true);
  const statusId = useId();
  const typeId = useId();
  const [importScope, setImportScope] = useState<ManualImportScope | null>(null);
  const [rematching, setRematching] = useState<Issue | null>(null);
  const [offerResolve, setOfferResolve] = useState<Issue | null>(null);

  const parseStatusFilter = (value: string): IssueStatusFilter => {
    if (value === 'all' || value === 'active') return value;
    return STATUS_FILTERS.find((s) => s === value) ?? 'active';
  };
  const parseTypeFilter = (value: string): IssueTypeFilter => TYPE_FILTERS.find((t) => t === value) ?? 'all';

  const startRematch = (issue: Issue, rematch: IssueRematchResult): void => {
    setRematching(issue);
    setImportScope({
      kind: 'album',
      albumId: rematch.scope.album_id,
      title: rematch.album.title ?? issue.media_title,
      issueId: issue.id,
    });
  };

  const resolveAfterImport = async (): Promise<void> => {
    const issue = offerResolve;
    setOfferResolve(null);
    if (!issue) return;
    try {
      const updated = await updateIssue(issue.id, { status: 'resolved' });
      queue.patch(updated);
      notifyIssuesChanged();
      onToast('Issue resolved');
    } catch (err: unknown) {
      onToast(errorMessage(err, 'Failed to resolve issue'), 'error');
    }
  };

  return (
    <section className="space-y-3">
      <PageActionsPortal>
        <div className="flex items-center gap-2 min-w-0">
          <select
            id={statusId}
            name="issue-status-filter"
            aria-label="Filter by status"
            value={queue.statusFilter}
            onChange={(e) => queue.setStatusFilter(parseStatusFilter(e.target.value))}
            className={selectClass}
          >
            <option value="active">Open + in progress</option>
            <option value="all">All statuses</option>
            {STATUS_FILTERS.map((s) => (
              <option key={s} value={s}>
                {ISSUE_STATUS_LABELS[s]}
              </option>
            ))}
          </select>
          <select
            id={typeId}
            name="issue-type-filter"
            aria-label="Filter by type"
            value={queue.typeFilter}
            onChange={(e) => queue.setTypeFilter(parseTypeFilter(e.target.value))}
            className={`${selectClass} hidden sm:block`}
          >
            <option value="all">All types</option>
            {TYPE_FILTERS.map((t) => (
              <option key={t} value={t}>
                {ISSUE_TYPE_LABELS[t]}
              </option>
            ))}
          </select>
          <SearchBar
            value={queue.search}
            onChange={queue.setSearch}
            placeholder="Search issues"
            ariaLabel="Search issues"
            name="issue-search"
            className="flex-1 min-w-0"
          />
        </div>
      </PageActionsPortal>

      <h4 className="text-sm font-bold uppercase font-mono text-white">
        Issues
        <span className="ml-2 text-neutral-500 font-normal">
          {queue.issues.length}
          {queue.issues.length !== queue.total ? ` of ${queue.total}` : ''}
        </span>
      </h4>

      {queue.isLoading && queue.total === 0 && (
        <div className="flex justify-center py-12">
          <CassetteLoader size="sm" />
        </div>
      )}
      {queue.error && (
        <p role="alert" className="p-3 border border-[var(--status-error)] rounded-[4px] text-xs font-mono text-[var(--status-error)]">
          {queue.error}
        </p>
      )}
      {!queue.isLoading && !queue.error && queue.issues.length === 0 && (
        <p className="text-center py-12 font-mono text-sm text-[var(--text-muted)]">No issues match.</p>
      )}

      <ul aria-label="Issues" className="space-y-2">
        {queue.issues.map((issue) => (
          <li key={issue.id}>
            <MachinedCard className="p-0">
              <button
                type="button"
                onClick={() => onOpenIssue(issue.id)}
                className="w-full text-left p-3 rounded-[4px] hover:bg-[var(--bg-card-hover)] focus:outline-none focus-visible:border-[var(--accent-amber)] grid grid-cols-[1fr_auto] gap-x-3 gap-y-1"
              >
                <div className="min-w-0">
                  <p className="text-[10px] font-mono uppercase tracking-wider text-[var(--accent-amber)]">{ISSUE_TYPE_LABELS[issue.issue_type]}</p>
                  <h5 className="font-bold text-sm text-[var(--text-primary)] truncate" title={issue.media_title}>
                    {issue.media_title}
                  </h5>
                  <p className="text-xs text-[var(--text-secondary)] truncate">{issue.artist}</p>
                </div>
                <div className="flex flex-col items-end gap-1">
                  <IssueStatusChip status={issue.status} />
                  <span className="text-[11px] font-mono text-[var(--text-muted)] inline-flex items-center gap-1">
                    {(issue.comment_count ?? 0) > 0 && (
                      <>
                        <MessageSquare className="h-3 w-3" aria-hidden="true" /> {issue.comment_count}
                      </>
                    )}
                  </span>
                </div>
                <p className="col-span-2 text-[11px] font-mono text-[var(--text-muted)] truncate">
                  {issue.username || 'Unknown user'} - {relativeTime(issue.created_at)}
                </p>
              </button>
            </MachinedCard>
          </li>
        ))}
      </ul>

      <IssueDetailModal
        issueId={issueId ?? null}
        isAdmin
        historyBacked={false}
        onClose={onCloseIssue}
        onChanged={(changed) => {
          if (changed) queue.patch(changed);
          else void queue.refresh();
        }}
        onDeleted={(id) => {
          void queue.refresh();
          onToast(`Issue ${id} deleted`);
        }}
        onRematch={startRematch}
        onToast={onToast}
      />
      <ManualImportModal
        scope={importScope}
        onClose={() => {
          setImportScope(null);
          setRematching(null);
        }}
        onImported={() => {
          setImportScope(null);
          onToast('Files imported');
          if (rematching) setOfferResolve(rematching);
          setRematching(null);
        }}
      />
      <ConfirmDialog
        isOpen={offerResolve !== null}
        title="Resolve issue?"
        confirmLabel="Mark resolved"
        cancelLabel="Keep open"
        onCancel={() => setOfferResolve(null)}
        onConfirm={() => void resolveAfterImport()}
      >
        Files were imported for {offerResolve?.media_title}. Mark this issue resolved?
      </ConfirmDialog>
    </section>
  );
};
