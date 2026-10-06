import React, { useState } from 'react';
import { Loader2, RotateCcw, CheckCheck } from 'lucide-react';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import { useIssueActions } from '@/hooks/useIssueActions';
import { useIssueDetail } from '@/hooks/useIssueDetail';
import { notifyIssuesChanged, deleteIssue } from '@/services/issueService';
import { errorMessage } from '@/services/apiClient';
import type { Issue, IssueAction, IssueRematchResult } from '@/types/models';
import { ISSUE_ACTION_LABELS, ISSUE_TYPE_LABELS } from '@/types/models';
import { AdminIssueControls } from './AdminIssueControls';
import { IssueComposer } from './IssueComposer';
import { IssueStatusChip } from './IssueStatusChip';
import { IssueThread } from './IssueThread';
import { relativeTime } from './issueFormat';

export interface IssueDetailModalProps {
  /** Null keeps the modal closed. */
  issueId: string | null;
  /** Admin view: all comments incl. system, admin controls. Requesters get the reporter view. */
  isAdmin: boolean;
  onClose: () => void;
  /** Fired with the updated issue (or null when only the thread changed) so lists can refresh. */
  onChanged?: (issue: Issue | null) => void;
  /** Fired after an admin deleted the issue. */
  onDeleted?: (id: string) => void;
  /** Admin only: open the Manual Import modal for a `rematch` result. Without it the rematch action is not offered. */
  onRematch?: (issue: Issue, rematch: IssueRematchResult) => void;
  onToast?: (message: string, tone?: 'ok' | 'error') => void;
  /** Pass false when the open state is already carried by the URL (admin queue). */
  historyBacked?: boolean;
}

/** One issue: header, details, thread, composer, and per-role controls. Opening marks it seen for the reporter. */
export const IssueDetailModal: React.FC<IssueDetailModalProps> = ({
  issueId,
  isAdmin,
  onClose,
  onChanged,
  onDeleted,
  onRematch,
  onToast,
  historyBacked = true,
}) => {
  const detail = useIssueDetail(issueId, isAdmin, (changed) => {
    notifyIssuesChanged();
    onChanged?.(changed);
  });
  const actions = useIssueActions();
  const [actionError, setActionError] = useState<string | null>(null);
  const { issue, comments, isLoading, error, isBusy } = detail;
  const toast = (message: string, tone: 'ok' | 'error' = 'ok'): void => {
    if (onToast) onToast(message, tone);
    else if (tone === 'error') setActionError(message);
  };

  const changeStatus = async (status: Issue['status']): Promise<void> => {
    const failure = await detail.changeStatus(status);
    if (failure) toast(failure, 'error');
  };

  const runAction = async (action: IssueAction): Promise<void> => {
    if (!issue) return;
    const outcome = await actions.run(issue.id, action);
    if (outcome.kind === 'error') {
      toast(outcome.message, 'error');
      return;
    }
    await detail.adopt(outcome.issue);
    if (outcome.kind === 'rematch') {
      onRematch?.(outcome.issue, outcome.rematch);
      return;
    }
    toast(`${ISSUE_ACTION_LABELS[action]}: ${outcome.message}`);
  };

  const remove = async (): Promise<void> => {
    if (!issue) return;
    try {
      await deleteIssue(issue.id);
      notifyIssuesChanged();
      onDeleted?.(issue.id);
      onClose();
    } catch (err: unknown) {
      toast(errorMessage(err, 'Failed to delete issue'), 'error');
    }
  };

  const visibleIssue: Issue | null =
    issue && isAdmin && !onRematch
      ? { ...issue, available_actions: (issue.available_actions ?? []).filter((a) => a !== 'rematch') }
      : issue;
  const finished = issue?.status === 'resolved' || issue?.status === 'wont_fix';

  const footer = (
    <>
      {!isAdmin && issue && (
        <TapeDeckButton
          size="sm"
          disabled={isBusy}
          onClick={() => void changeStatus(finished ? 'open' : 'resolved')}
          icon={finished ? <RotateCcw className="h-3.5 w-3.5" /> : <CheckCheck className="h-3.5 w-3.5" />}
        >
          {finished ? 'Reopen' : 'Close issue'}
        </TapeDeckButton>
      )}
      <TapeDeckButton size="sm" onClick={onClose}>
        Close
      </TapeDeckButton>
    </>
  );

  return (
    <ObsidianModal
      isOpen={issueId !== null}
      onClose={onClose}
      historyBacked={historyBacked}
      title="Issue"
      subtitle={issue ? `${issue.media_title} - ${issue.artist}` : undefined}
      maxWidth="sm:max-w-2xl"
      footer={footer}
    >
      {isLoading && !issue && (
        <div className="flex justify-center py-10" role="status" aria-label="Loading issue">
          <Loader2 className="h-6 w-6 text-[var(--accent-amber)] animate-spin" />
        </div>
      )}
      {error && (
        <p role="alert" className="p-3 border border-[var(--status-error)] rounded-[4px] text-xs font-mono text-[var(--status-error)]">
          {error}
        </p>
      )}
      {actionError && (
        <p role="alert" className="mb-3 p-3 border border-[var(--status-error)] rounded-[4px] text-xs font-mono text-[var(--status-error)]">
          {actionError}
        </p>
      )}
      {issue && visibleIssue && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <IssueStatusChip status={issue.status} />
            <span className="text-[11px] font-mono uppercase text-[var(--text-secondary)]">{ISSUE_TYPE_LABELS[issue.issue_type]}</span>
            <span className="text-[11px] font-mono text-[var(--text-muted)]">
              {isAdmin && issue.username ? `${issue.username} - ` : ''}
              opened {relativeTime(issue.created_at)}
              {issue.resolved_at ? ` - closed ${relativeTime(issue.resolved_at)}` : ''}
            </span>
          </div>
          <p className="text-sm text-[var(--text-primary)] whitespace-pre-wrap break-words">{issue.problem_details}</p>

          {isAdmin && (
            <AdminIssueControls
              issue={visibleIssue}
              busy={isBusy}
              runningAction={actions.running}
              onStatus={(s) => void changeStatus(s)}
              onAction={(a) => void runAction(a)}
              onDelete={() => void remove()}
            />
          )}

          <IssueThread comments={comments} />
          <IssueComposer disabled={isBusy} onSend={detail.postComment} />
        </div>
      )}
    </ObsidianModal>
  );
};
