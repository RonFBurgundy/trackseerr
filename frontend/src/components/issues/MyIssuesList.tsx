import React, { useMemo, useState } from 'react';
import { Loader2, MessageSquare } from 'lucide-react';
import { MachinedCard } from '@/components/ui';
import type { UseIssuesReturn } from '@/hooks/useIssues';
import { ISSUE_TYPE_LABELS } from '@/types/models';
import { IssueDetailModal } from './IssueDetailModal';
import { IssueStatusChip } from './IssueStatusChip';
import { relativeTime } from './issueFormat';

export interface MyIssuesListProps {
  issuesHook: UseIssuesReturn;
  /** Admins receive everyone's issues from the API; this list shows only the viewer's own. */
  currentUserId?: string | number;
}

export const MyIssuesList: React.FC<MyIssuesListProps> = ({ issuesHook, currentUserId }) => {
  const { issues: all, isLoading, error, patch, refresh } = issuesHook;
  const [openId, setOpenId] = useState<string | null>(null);
  const issues = useMemo(
    () => (currentUserId === undefined ? all : all.filter((i) => String(i.user_id) === String(currentUserId))),
    [all, currentUserId]
  );

  if (isLoading && issues.length === 0) {
    return (
      <div className="flex justify-center py-12">
        <Loader2 className="h-6 w-6 text-[var(--accent-amber)] animate-spin" />
      </div>
    );
  }

  if (error && issues.length === 0) {
    return (
      <div className="p-4 border border-[var(--status-error)] rounded-[4px] text-xs text-[var(--status-error)] font-mono">
        {error}
      </div>
    );
  }

  return (
    <>
      {issues.length === 0 ? (
        <div className="text-center py-16 text-[var(--text-muted)] font-mono text-sm">You have not reported any issues.</div>
      ) : (
        <ul aria-label="My issues" className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {issues.map((issue) => (
            <li key={issue.id}>
              <MachinedCard className="p-0">
                <button
                  type="button"
                  onClick={() => setOpenId(issue.id)}
                  className="w-full text-left p-3 flex flex-col gap-2 rounded-[4px] hover:bg-[var(--bg-card-hover)] focus:outline-none focus-visible:border-[var(--accent-amber)]"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <h4 className="font-bold text-sm text-[var(--text-primary)] truncate flex items-center gap-1.5" title={issue.media_title}>
                        {issue.unread && (
                          <span
                            className="h-2 w-2 rounded-full bg-[var(--accent-amber)] shadow-[0_0_6px_var(--accent-amber-glow)] shrink-0"
                            role="img"
                            aria-label="New activity"
                          />
                        )}
                        <span className="truncate">{issue.media_title}</span>
                      </h4>
                      <p className="text-xs text-[var(--text-secondary)] truncate" title={issue.artist}>
                        {issue.artist}
                      </p>
                    </div>
                    <IssueStatusChip status={issue.status} />
                  </div>
                  <p className="text-[11px] font-mono uppercase text-[var(--text-muted)] flex items-center gap-2">
                    <span>{ISSUE_TYPE_LABELS[issue.issue_type]}</span>
                    <span>- {relativeTime(issue.last_activity_at ?? issue.updated_at ?? issue.created_at)}</span>
                    {(issue.comment_count ?? 0) > 0 && (
                      <span className="inline-flex items-center gap-1 ml-auto">
                        <MessageSquare className="h-3 w-3" aria-hidden="true" /> {issue.comment_count}
                      </span>
                    )}
                  </p>
                  <p className="text-xs text-[var(--text-secondary)] break-words line-clamp-2">{issue.problem_details}</p>
                </button>
              </MachinedCard>
            </li>
          ))}
        </ul>
      )}
      <IssueDetailModal
        issueId={openId}
        isAdmin={false}
        onClose={() => setOpenId(null)}
        onChanged={(changed) => {
          if (changed) patch(changed);
          else void refresh();
        }}
      />
    </>
  );
};
