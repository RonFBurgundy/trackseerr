import React, { useState } from 'react';
import { ExternalLink, Loader2, Trash2 } from 'lucide-react';
import { ConfirmDialog, FormField, inputClass, TapeDeckButton } from '@/components/ui';
import { routeToHash } from '@/hooks/useAppRoute';
import type { Issue, IssueAction, IssueStatus } from '@/types/models';
import { ISSUE_ACTION_LABELS, ISSUE_STATUS_LABELS } from '@/types/models';

const STATUSES: readonly IssueStatus[] = ['open', 'in_progress', 'resolved', 'wont_fix'];
/** Display order of the fix actions. */
const ACTION_ORDER: readonly IssueAction[] = ['retry_request', 'research', 'blocklist_and_research', 'rematch'];

export interface AdminIssueControlsProps {
  issue: Issue;
  busy: boolean;
  /** The action currently running, if any. */
  runningAction: string | null;
  onStatus: (status: IssueStatus) => void;
  onAction: (action: IssueAction) => void;
  onDelete: () => void;
}

/** Admin-only panel: status control, the fix actions the server offers for this issue, library/request links, delete. */
export const AdminIssueControls: React.FC<AdminIssueControlsProps> = ({ issue, busy, runningAction, onStatus, onAction, onDelete }) => {
  const [confirmAction, setConfirmAction] = useState<IssueAction | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<boolean>(false);
  const offered = issue.available_actions ?? [];
  const actions = ACTION_ORDER.filter((a) => offered.includes(a));
  const acting = runningAction !== null;

  const request = (action: IssueAction): void => {
    if (action === 'blocklist_and_research') setConfirmAction(action);
    else onAction(action);
  };

  return (
    <section aria-label="Admin controls" className="space-y-3 p-3 rounded-[4px] border border-[var(--border-default)] bg-[var(--bg-surface-elevated)]">
      <FormField label="Status" name="issue-status">
        <select
          value={issue.status}
          disabled={busy || acting}
          onChange={(e) => {
            const next = STATUSES.find((s) => s === e.target.value);
            if (next && next !== issue.status) onStatus(next);
          }}
          className={inputClass}
        >
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {ISSUE_STATUS_LABELS[s]}
            </option>
          ))}
        </select>
      </FormField>

      {actions.length > 0 && (
        <div className="flex flex-wrap gap-2" role="group" aria-label="Fix actions">
          {actions.map((a) => (
            <TapeDeckButton
              key={a}
              size="sm"
              variant={a === 'blocklist_and_research' ? 'danger' : 'default'}
              disabled={busy || acting}
              onClick={() => request(a)}
              icon={runningAction === a ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : undefined}
            >
              {ISSUE_ACTION_LABELS[a]}
            </TapeDeckButton>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap gap-3 text-xs font-mono">
          {issue.request_id && (
            <a href={routeToHash({ tab: 'requests', sub: 'all' })} className="inline-flex items-center gap-1 text-[var(--accent-amber)] hover:underline">
              <ExternalLink className="h-3 w-3" aria-hidden="true" /> Request #{issue.request_id}
            </a>
          )}
          {issue.album_id && (
            <a
              href={routeToHash({ tab: 'library', sub: 'albums', detail: { albumId: issue.album_id } })}
              className="inline-flex items-center gap-1 text-[var(--accent-amber)] hover:underline"
            >
              <ExternalLink className="h-3 w-3" aria-hidden="true" /> Library album
            </a>
          )}
        </div>
        <TapeDeckButton size="sm" variant="danger" disabled={busy || acting} onClick={() => setConfirmDelete(true)} icon={<Trash2 className="h-3.5 w-3.5" />}>
          Delete
        </TapeDeckButton>
      </div>

      <ConfirmDialog
        isOpen={confirmAction !== null}
        title="Blocklist and search again"
        confirmLabel="Blocklist & search"
        onCancel={() => setConfirmAction(null)}
        onConfirm={() => {
          const action = confirmAction;
          setConfirmAction(null);
          if (action) onAction(action);
        }}
      >
        This blocklists the release currently imported for this album so it is not grabbed again, then searches for a replacement. The existing files stay until a replacement is imported.
      </ConfirmDialog>
      <ConfirmDialog
        isOpen={confirmDelete}
        title="Delete issue"
        confirmLabel="Delete"
        onCancel={() => setConfirmDelete(false)}
        onConfirm={() => {
          setConfirmDelete(false);
          onDelete();
        }}
      >
        Permanently delete this issue and its comments? This cannot be undone.
      </ConfirmDialog>
    </section>
  );
};
