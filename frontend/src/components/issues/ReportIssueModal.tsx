import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import { Loader2, Send, Check } from 'lucide-react';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import { ApiError } from '@/services/apiClient';
import type { CreateIssuePayload, Issue, IssueType } from '@/types/models';
import { ISSUE_TYPE_LABELS, ISSUE_MAX_DETAILS, ISSUE_MAX_TITLE } from '@/types/models';

export interface ReportIssueModalProps {
  isOpen: boolean;
  onClose: () => void;
  mediaTitle: string;
  artist: string;
  requestId?: string;
  onSubmit: (payload: CreateIssuePayload) => Promise<Issue>;
}

const ISSUE_TYPES = Object.keys(ISSUE_TYPE_LABELS) as IssueType[];

export function mapIssueError(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.status) {
      case 409:
        return "You've already reported this — it's still open.";
      case 429:
        return "You've reached the limit of 10 reports in 24 hours. Please try again later.";
      case 422:
        return 'Some details were not accepted. Check the type and description and try again.';
      case 404:
        return 'The related request could not be found.';
      default:
        return err.message;
    }
  }
  return err instanceof Error ? err.message : 'Failed to submit report.';
}

export function validateIssue(
  mediaTitle: string,
  artist: string,
  details: string
): string | null {
  if (!mediaTitle.trim() || mediaTitle.length > ISSUE_MAX_TITLE) {
    return `Title must be 1-${ISSUE_MAX_TITLE} characters.`;
  }
  if (!artist.trim() || artist.length > ISSUE_MAX_TITLE) {
    return `Artist must be 1-${ISSUE_MAX_TITLE} characters.`;
  }
  if (!details.trim()) return 'Please describe the problem.';
  if (details.length > ISSUE_MAX_DETAILS) {
    return `Description must be at most ${ISSUE_MAX_DETAILS} characters.`;
  }
  return null;
}

export const ReportIssueModal: React.FC<ReportIssueModalProps> = ({
  isOpen,
  onClose,
  mediaTitle,
  artist,
  requestId,
  onSubmit,
}) => {
  const [issueType, setIssueType] = useState<IssueType>('audio_quality');
  const [details, setDetails] = useState<string>('');
  const [isSending, setIsSending] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<boolean>(false);

  const handleSubmit = async () => {
    if (isSending) return;
    const invalid = validateIssue(mediaTitle, artist, details);
    if (invalid) {
      setError(invalid);
      return;
    }
    setIsSending(true);
    setError(null);
    try {
      await onSubmit({
        media_title: mediaTitle,
        artist,
        issue_type: issueType,
        problem_details: details.trim(),
        ...(requestId ? { request_id: requestId } : {}),
      });
      setDone(true);
    } catch (err: unknown) {
      setError(mapIssueError(err));
    } finally {
      setIsSending(false);
    }
  };

  const overLimit = details.length > ISSUE_MAX_DETAILS;

  const footer = done ? (
    <TapeDeckButton variant="amber" onClick={onClose}>
      Close
    </TapeDeckButton>
  ) : (
    <>
      <TapeDeckButton onClick={onClose} disabled={isSending}>
        Cancel
      </TapeDeckButton>
      <TapeDeckButton
        variant="amber"
        onClick={() => void handleSubmit()}
        disabled={isSending || overLimit || !details.trim()}
        icon={
          isSending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />
        }
      >
        {isSending ? 'Sending' : 'Submit'}
      </TapeDeckButton>
    </>
  );

  const modal = (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="Report an issue"
      subtitle={`${mediaTitle} - ${artist}`}
      maxWidth="sm:max-w-lg"
      footer={footer}
    >
      {done ? (
        <div className="flex items-center gap-3 py-6 text-[var(--status-success)]">
          <Check className="h-5 w-5 shrink-0" />
          <p className="text-sm text-[var(--text-primary)]">Thanks — an admin will look at this.</p>
        </div>
      ) : (
        <div className="space-y-4">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <span className="block text-[10px] uppercase tracking-widest font-mono text-[var(--text-muted)] mb-1">
                Title
              </span>
              <p className="text-sm text-[var(--text-primary)] break-words">{mediaTitle}</p>
            </div>
            <div>
              <span className="block text-[10px] uppercase tracking-widest font-mono text-[var(--text-muted)] mb-1">
                Artist
              </span>
              <p className="text-sm text-[var(--text-primary)] break-words">{artist}</p>
            </div>
          </div>

          <div>
            <label
              htmlFor="issue-type"
              className="block text-[10px] uppercase tracking-widest font-mono text-[var(--text-muted)] mb-1"
            >
              Issue type
            </label>
            <select
              id="issue-type"
              name="issue-type"
              value={issueType}
              onChange={(e) => setIssueType(e.target.value as IssueType)}
              disabled={isSending}
              className="w-full min-h-[36px] px-2.5 rounded-[3px] bg-[var(--bg-surface-elevated)] border border-[var(--border-default)] text-[13px] sm:text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-amber)]"
            >
              {ISSUE_TYPES.map((t) => (
                <option key={t} value={t}>
                  {ISSUE_TYPE_LABELS[t]}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label
              htmlFor="issue-details"
              className="block text-[10px] uppercase tracking-widest font-mono text-[var(--text-muted)] mb-1"
            >
              What is wrong?
            </label>
            <textarea
              id="issue-details"
              name="issue-details"
              value={details}
              onChange={(e) => setDetails(e.target.value)}
              disabled={isSending}
              rows={6}
              className="w-full px-3 py-2 rounded-[3px] bg-[var(--bg-surface-elevated)] border border-[var(--border-default)] text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-amber)] resize-y"
              placeholder="Describe the problem, e.g. which tracks or what you hear."
            />
            <div
              className={`mt-1 text-right text-[11px] font-mono ${
                overLimit ? 'text-[var(--status-error)]' : 'text-[var(--text-muted)]'
              }`}
              aria-live="polite"
            >
              {details.length} / {ISSUE_MAX_DETAILS}
            </div>
          </div>

          {error && (
            <div
              role="alert"
              className="p-3 border border-[var(--status-error)] rounded-[4px] bg-[var(--bg-surface-elevated)] text-xs text-[var(--status-error)] font-mono"
            >
              {error}
            </div>
          )}
        </div>
      )}
    </ObsidianModal>
  );

  return isOpen ? createPortal(modal, document.body) : null;
};
