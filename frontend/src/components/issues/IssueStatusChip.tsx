import React from 'react';
import type { IssueStatus } from '@/types/models';
import { ISSUE_STATUS_LABELS } from '@/types/models';

const STATUS_CLASS: Record<IssueStatus, string> = {
  open: 'border-[var(--accent-amber)] text-[var(--accent-amber)]',
  in_progress: 'border-[var(--text-secondary)] text-[var(--text-primary)]',
  resolved: 'border-[var(--status-success)] text-[var(--status-success)]',
  wont_fix: 'border-[var(--border-default)] text-[var(--text-muted)]',
};

export interface IssueStatusChipProps {
  status: IssueStatus;
  prefix?: string;
}

export const IssueStatusChip: React.FC<IssueStatusChipProps> = ({ status, prefix }) => (
  <span
    className={`inline-flex items-center px-2 py-0.5 rounded-[2px] border bg-[var(--bg-surface-elevated)] text-[11px] font-mono uppercase ${STATUS_CLASS[status]}`}
  >
    {prefix ? `${prefix} (${ISSUE_STATUS_LABELS[status]})` : ISSUE_STATUS_LABELS[status]}
  </span>
);
