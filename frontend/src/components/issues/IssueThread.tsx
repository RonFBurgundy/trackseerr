import React from 'react';
import { Cog } from 'lucide-react';
import type { IssueComment } from '@/types/models';
import { relativeTime } from './issueFormat';

export interface IssueThreadProps {
  comments: readonly IssueComment[];
}

/** Comment thread. System entries (admin only; the server omits them for requesters) are styled as log lines. */
export const IssueThread: React.FC<IssueThreadProps> = ({ comments }) => {
  if (comments.length === 0) {
    return <p className="text-xs font-mono text-[var(--text-muted)]">No comments yet.</p>;
  }
  return (
    <ol className="space-y-2" aria-label="Comments">
      {comments.map((c) =>
        c.is_system ? (
          <li
            key={c.id}
            className="flex items-start gap-2 px-2.5 py-1.5 rounded-[3px] border border-dashed border-[var(--border-default)] bg-[var(--bg-canvas)] text-[11px] font-mono text-[var(--text-muted)]"
          >
            <Cog className="h-3 w-3 mt-0.5 shrink-0" aria-hidden="true" />
            <span className="min-w-0 flex-1 break-words">{c.body}</span>
            <span className="shrink-0">{relativeTime(c.created_at)}</span>
          </li>
        ) : (
          <li
            key={c.id}
            className={`px-3 py-2 rounded-[4px] border bg-[var(--bg-card)] ${
              c.is_admin ? 'border-[var(--accent-amber)]/40' : 'border-[var(--border-default)]'
            }`}
          >
            <div className="flex items-center justify-between gap-2 text-[10px] font-mono uppercase tracking-wider text-[var(--text-muted)]">
              <span className="truncate">
                {c.mine ? 'You' : c.username || (c.is_admin ? 'Admin' : 'User')}
                {c.is_admin && !c.mine && <span className="ml-1.5 text-[var(--accent-amber)]">Admin</span>}
              </span>
              <span className="shrink-0">{relativeTime(c.created_at)}</span>
            </div>
            <p className="mt-1 text-sm text-[var(--text-primary)] whitespace-pre-wrap break-words">{c.body}</p>
          </li>
        )
      )}
    </ol>
  );
};
