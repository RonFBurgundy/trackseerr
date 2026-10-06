import React, { useState } from 'react';
import { Flag } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';
import type { UseIssuesReturn } from '@/hooks/useIssues';
import type { IssueType } from '@/types/models';
import { IssueDetailModal } from './IssueDetailModal';
import { ReportIssueModal } from './ReportIssueModal';
import type { IssueReference } from './ReportIssueModal';
import { IssueStatusChip } from './IssueStatusChip';

export interface IssueReportButtonProps {
  mediaTitle: string;
  artist: string;
  /** Types offered for this context. */
  types: readonly IssueType[];
  reference?: IssueReference;
  issuesHook: UseIssuesReturn;
  isAdmin?: boolean;
  /** Visible label; "Report issue" by default. */
  label?: string;
  /** Icon-only key (the label becomes the accessible name). */
  iconOnly?: boolean;
  className?: string;
}

/** Shows an "Already reported" chip (opens the existing issue) when an active one exists, else a report key. */
export const IssueReportButton: React.FC<IssueReportButtonProps> = ({
  mediaTitle,
  artist,
  types,
  reference,
  issuesHook,
  isAdmin = false,
  label = 'Report issue',
  iconOnly = false,
  className = '',
}) => {
  const [open, setOpen] = useState<boolean>(false);
  const [viewing, setViewing] = useState<boolean>(false);
  const existing = issuesHook.findActive(mediaTitle, artist);

  if (existing) {
    return (
      <span className={className}>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            setViewing(true);
          }}
          aria-label={`Open your report for ${mediaTitle}`}
          className="focus:outline-none focus-visible:ring-1 focus-visible:ring-[var(--accent-amber)] rounded-[2px]"
        >
          <IssueStatusChip status={existing.status} prefix="Already reported" />
        </button>
        <IssueDetailModal
          issueId={viewing ? existing.id : null}
          isAdmin={false}
          onClose={() => setViewing(false)}
          onChanged={(changed) => {
            if (changed) issuesHook.patch(changed);
            else void issuesHook.refresh();
          }}
        />
      </span>
    );
  }

  return (
    <>
      <TapeDeckButton
        size="sm"
        className={className}
        onClick={(e) => {
          e.stopPropagation();
          setOpen(true);
        }}
        aria-label={iconOnly ? label : undefined}
        title={label}
        icon={<Flag className="h-3.5 w-3.5" />}
      >
        {iconOnly ? null : label}
      </TapeDeckButton>
      <ReportIssueModal
        isOpen={open}
        onClose={() => setOpen(false)}
        mediaTitle={mediaTitle}
        artist={artist}
        reference={reference}
        types={types}
        isAdmin={isAdmin}
        onSubmit={issuesHook.submit}
      />
    </>
  );
};
