import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import { Loader2, Send, Check } from 'lucide-react';
import { FormField, inputClass, ObsidianModal, TapeDeckButton } from '@/components/ui';
import { ApiError } from '@/services/apiClient';
import type { CreateIssuePayload, Issue, IssueItemType, IssueType } from '@/types/models';
import { ISSUE_TYPE_LABELS, ISSUE_MAX_DETAILS, ISSUE_MAX_TITLE } from '@/types/models';
import { IssueDetailModal } from './IssueDetailModal';

/** What the report is about, beyond its title: exactly one family of references applies per entry point. */
export interface IssueReference {
  /** Requests list: the request this report is about. */
  requestId?: string;
  /** Discover album / track popup. */
  discoveryId?: string;
  itemType?: IssueItemType;
  /** Admin library album / track. */
  albumId?: string;
  trackId?: string;
}

export interface ReportIssueModalProps {
  isOpen: boolean;
  onClose: () => void;
  mediaTitle: string;
  artist: string;
  reference?: IssueReference;
  /** Types offered for this context (a request that is not yet fulfilled offers "stuck", media offers the rest). */
  types: readonly IssueType[];
  isAdmin?: boolean;
  onSubmit: (payload: CreateIssuePayload) => Promise<Issue>;
}

/** The 409 duplicate body carries the id of the report already open. */
function existingIssueId(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const body: unknown = err.body;
  if (typeof body === 'object' && body !== null && 'existing_issue_id' in body) {
    const id = body.existing_issue_id;
    if (typeof id === 'string' && id) return id;
  }
  return null;
}

export function mapIssueError(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.status) {
      case 409:
        return existingIssueId(err) ? 'You already reported this.' : err.message;
      case 429:
        return "You've reached the limit of 10 reports in 24 hours. Please try again later.";
      case 422:
        return err.message.includes('request') ? err.message : 'Some details were not accepted. Check the type and description and try again.';
      case 404:
        return 'The related item could not be found.';
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
  reference,
  types,
  isAdmin = false,
  onSubmit,
}) => {
  const [issueType, setIssueType] = useState<IssueType>(types[0] ?? 'other');
  const [details, setDetails] = useState<string>('');
  const [isSending, setIsSending] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [duplicateId, setDuplicateId] = useState<string | null>(null);
  const [viewingId, setViewingId] = useState<string | null>(null);
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
    setDuplicateId(null);
    try {
      await onSubmit({
        media_title: mediaTitle,
        artist,
        issue_type: issueType,
        problem_details: details.trim(),
        ...(reference?.requestId ? { request_id: reference.requestId } : {}),
        ...(reference?.discoveryId ? { discovery_id: reference.discoveryId } : {}),
        ...(reference?.itemType ? { item_type: reference.itemType } : {}),
        ...(isAdmin && reference?.albumId ? { album_id: reference.albumId } : {}),
        ...(isAdmin && reference?.trackId ? { track_id: reference.trackId } : {}),
      });
      setDone(true);
    } catch (err: unknown) {
      setDuplicateId(existingIssueId(err));
      setError(mapIssueError(err));
    } finally {
      setIsSending(false);
    }
  };

  const openExisting = (): void => {
    if (!duplicateId) return;
    if (isAdmin) {
      // Admins manage it in the Requests queue.
      onClose();
      window.location.hash = `#/requests/issues/${encodeURIComponent(duplicateId)}`;
      return;
    }
    setViewingId(duplicateId);
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
          <p className="text-sm text-[var(--text-primary)]">Thanks - an admin will look at this.</p>
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

          <FormField label="Issue type" name="issue-type">
            <select
              value={issueType}
              onChange={(e) => {
                const next = types.find((t) => t === e.target.value);
                if (next) setIssueType(next);
              }}
              disabled={isSending}
              className={inputClass}
            >
              {types.map((t) => (
                <option key={t} value={t}>
                  {ISSUE_TYPE_LABELS[t]}
                </option>
              ))}
            </select>
          </FormField>

          <div>
            <FormField label="What is wrong?" name="issue-details">
              <textarea
                value={details}
                onChange={(e) => setDetails(e.target.value)}
                disabled={isSending}
                rows={6}
                className={`${inputClass} resize-y`}
                placeholder="Describe the problem, e.g. which tracks or what you hear."
              />
            </FormField>
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
              className="p-3 border border-[var(--status-error)] rounded-[4px] bg-[var(--bg-surface-elevated)] text-xs text-[var(--status-error)] font-mono flex flex-wrap items-center justify-between gap-2"
            >
              <span>{error}</span>
              {duplicateId && (
                <TapeDeckButton size="sm" onClick={openExisting}>
                  Open existing issue
                </TapeDeckButton>
              )}
            </div>
          )}
        </div>
      )}
    </ObsidianModal>
  );

  return (
    <>
      {isOpen ? createPortal(modal, document.body) : null}
      <IssueDetailModal issueId={viewingId} isAdmin={false} onClose={() => setViewingId(null)} />
    </>
  );
};
