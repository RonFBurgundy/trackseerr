import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, X, Trash2, Clock, CheckCircle2, AlertTriangle, RotateCw } from 'lucide-react';
import type { UseRequestsReturn, RequestFilter } from '@/hooks/useRequests';
import type { RequestItem } from '@/types/models';
import { MEDIA_ISSUE_TYPES, REQUEST_ISSUE_TYPES } from '@/types/models';
import {
  TabStrip,
  TapeDeckButton,
  MachinedCard,
  QuotaBadge,
  ToastBanner,
  type OverflowMenuItem,
} from '@/components/ui';
import { PageFrame } from '@/components/layout';
import { FlatList, type FlatListColumn } from '@/components/lists';
import { AdminIssuesPanel, IssueReportButton, MyIssuesList, untilTime } from '@/components/issues';
import { formatRequestSourceBadge, formatSourceLabel } from '@/components/requests';
import { useToast } from '@/hooks/useToast';
import type { UseIssuesReturn } from '@/hooks/useIssues';
import type { AccountInfo } from '@/types/account';
import { QuotaBars } from '@/components/account';
import type { RequestsSub, NavigateOptions } from '@/hooks/useAppRoute';

const noop = (): void => undefined;

export interface RequestsViewProps {
  /** Route sub-page: a request filter, or 'issues'. */
  sub: RequestsSub;
  onSubChange: (sub: RequestsSub, options?: NavigateOptions) => void;
  requestsHook: UseRequestsReturn;
  isAdmin?: boolean;
  canManageRequests?: boolean;
  issuesHook: UseIssuesReturn;
  /** Open + in-progress issue count, shown on the Issues key for admins. */
  issuesOpenCount?: number;
  /** Own issues with unseen admin activity; badge on the My issues key for non-admins. */
  issuesUnreadCount?: number;
  /** Issue open on top of the queue (`#/requests/issues/<id>`). */
  issueId?: string;
  onOpenIssue?: (id: string) => void;
  onCloseIssue?: () => void;
  currentUserId?: string;
  /** From GET /api/account; drives the per-type remaining-quota panel. */
  account?: AccountInfo | null;
}

export const RequestsView: React.FC<RequestsViewProps> = ({
  sub,
  onSubChange,
  requestsHook,
  isAdmin = false,
  canManageRequests = false,
  issuesHook,
  issuesOpenCount = 0,
  issuesUnreadCount = 0,
  issueId,
  onOpenIssue = noop,
  onCloseIssue = noop,
  currentUserId,
  account = null,
}) => {
  const canManage = isAdmin || canManageRequests;
  const {
    list,
    quota,
    source,
    sources,
    setSource,
    setFilter,
    approve,
    reject,
    remove,
    retry,
  } = requestsHook;
  const { toast, showToast } = useToast();

  const [processingId, setProcessingId] = useState<string | null>(null);
  const section: 'requests' | 'issues' = sub === 'issues' ? 'issues' : 'requests';

  // The request list loads through the hook's filter; keep it in step with the route.
  useEffect(() => {
    if (sub !== 'issues') setFilter(sub);
  }, [sub, setFilter]);

  const filters: Array<{ id: RequestFilter; label: string }> = [
    { id: 'all', label: 'All' },
    { id: 'pending', label: 'Pending' },
    { id: 'approved', label: 'Approved' },
    { id: 'fulfilled', label: 'Fulfilled' },
    { id: 'rejected', label: 'Rejected' },
  ];

  const handleApprove = async (id: string) => {
    setProcessingId(id);
    try {
      await approve(id);
    } finally {
      setProcessingId(null);
    }
  };

  const handleReject = async (id: string) => {
    setProcessingId(id);
    try {
      await reject(id);
    } finally {
      setProcessingId(null);
    }
  };

  const handleRetry = async (id: string) => {
    setProcessingId(id);
    try {
      const result = await retry(id);
      showToast(result.message, result.success ? 'ok' : 'error');
    } catch (err: unknown) {
      showToast(err instanceof Error ? err.message : 'Retry failed', 'error');
    } finally {
      setProcessingId(null);
    }
  };

  const handleRemove = async (id: string) => {
    setProcessingId(id);
    try {
      await remove(id);
    } finally {
      setProcessingId(null);
    }
  };

  const getStatusBadge = (status: RequestItem['status']) => {
    switch (status) {
      case 'approved':
      case 'processing':
        return (
          <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[2px] bg-info-950/60 border border-info-800 text-info-300 text-[11px] font-mono uppercase whitespace-nowrap">
            <CheckCircle2 className="h-3 w-3" /> Approved
          </span>
        );
      case 'fulfilled':
      case 'available':
        return (
          <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[2px] bg-green-950/60 border border-green-800 text-green-300 text-[11px] font-mono uppercase whitespace-nowrap">
            <Check className="h-3 w-3" /> Available
          </span>
        );
      case 'rejected':
        return (
          <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[2px] bg-red-950/60 border border-red-800 text-red-300 text-[11px] font-mono uppercase whitespace-nowrap">
            <X className="h-3 w-3" /> Rejected
          </span>
        );
      case 'pending':
      default:
        return (
          <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[2px] bg-amber-950/60 border border-[#e5a00d]/60 text-[#e5a00d] text-[11px] font-mono uppercase whitespace-nowrap">
            <Clock className="h-3 w-3" /> Pending
          </span>
        );
    }
  };

  const columns = useMemo<FlatListColumn<RequestItem>[]>(
    () => [
      {
        key: 'title',
        label: 'Title',
        mobile: 'title',
        render: (r) => (
          <div className="flex items-center gap-2.5 min-w-0">
            <img
              src={r.cover_url || '/placeholder.svg'}
              alt=""
              loading="lazy"
              decoding="async"
              className="h-8 w-8 rounded-[3px] object-cover border border-[#222222] shrink-0"
              onError={(e) => {
                (e.currentTarget as HTMLImageElement).src = '/placeholder.svg';
              }}
            />
            <span className="font-bold text-white truncate min-w-0" title={r.title}>
              {r.title}
            </span>
          </div>
        ),
      },
      {
        key: 'artist',
        label: 'Artist',
        mobile: 'sub',
        render: (r) => (
          <span className="truncate min-w-0" title={r.artist}>
            {r.artist}
          </span>
        ),
      },
      {
        key: 'album',
        label: 'Album',
        mobile: 'sub',
        render: (r) =>
          r.album ? (
            <span className="truncate min-w-0" title={r.album}>
              {r.album}
            </span>
          ) : null,
      },
      {
        key: 'source',
        label: 'Source',
        mobile: 'end',
        render: (r) => (
          <span className="inline-flex items-center px-1.5 py-0.5 rounded-[2px] bg-[#181818] border border-[#2a2a2a] text-neutral-300 text-[10px] font-mono uppercase tracking-wide whitespace-nowrap">
            {formatRequestSourceBadge(r)}
          </span>
        ),
      },
      {
        key: 'status',
        label: 'Status',
        mobile: 'end',
        render: (r) => getStatusBadge(r.status),
      },
      {
        key: 'created_at',
        label: 'Date',
        mobile: 'meta',
        render: (r) => (
          <span className="text-[10px] text-neutral-500 font-mono">
            {r.created_at ? new Date(r.created_at).toLocaleDateString() : ''}
          </span>
        ),
      },
      {
        key: 'user',
        label: 'User',
        mobile: 'meta',
        render: (r) =>
          r.username ? (
            <span className="text-[10px] text-neutral-500 font-mono">
              By: {r.username}
            </span>
          ) : null,
      },
      {
        key: 'message',
        label: 'Status Message',
        mobile: 'meta',
        render: (r) =>
          r.status_message ? (
            <span
              className="inline-flex items-center gap-1 text-[11px] text-amber-300 font-mono"
              role="status"
              data-reason={r.status_reason ?? undefined}
            >
              <AlertTriangle className="h-3 w-3 shrink-0" />
              <span>
                {r.status_message}
                {r.next_attempt_at ? ` - next retry ${untilTime(r.next_attempt_at)}` : ''}
              </span>
            </span>
          ) : null,
      },
    ],
    []
  );

  const rowMonitor = useCallback(
    (req: RequestItem) => {
      const isBusy = processingId === req.id;
      return (
        <div className="flex items-center gap-1.5 shrink-0">
          {req.status !== 'rejected' &&
            currentUserId !== undefined &&
            req.user_id === String(currentUserId) && (
              <IssueReportButton
                mediaTitle={req.title}
                artist={req.artist}
                reference={{ requestId: String(req.id) }}
                types={
                  req.status === 'fulfilled' || req.status === 'available'
                    ? MEDIA_ISSUE_TYPES
                    : REQUEST_ISSUE_TYPES
                }
                isAdmin={isAdmin}
                issuesHook={issuesHook}
                iconOnly
              />
            )}
          {canManage && req.status === 'pending' && (
            <>
              <TapeDeckButton
                size="sm"
                variant="amber"
                disabled={isBusy}
                onClick={(e) => {
                  e.stopPropagation();
                  void handleApprove(req.id);
                }}
                collapseLabel
                aria-label="Approve request"
                icon={<Check className="h-3.5 w-3.5" />}
              >
                Approve
              </TapeDeckButton>
              <TapeDeckButton
                size="sm"
                variant="danger"
                disabled={isBusy}
                onClick={(e) => {
                  e.stopPropagation();
                  void handleReject(req.id);
                }}
                collapseLabel
                aria-label="Reject request"
                icon={<X className="h-3.5 w-3.5" />}
              >
                Reject
              </TapeDeckButton>
            </>
          )}
          {isAdmin && req.status_reason && req.status !== 'pending' && req.status !== 'rejected' && (
            <TapeDeckButton
              size="sm"
              disabled={isBusy}
              onClick={(e) => {
                e.stopPropagation();
                void handleRetry(req.id);
              }}
              collapseLabel
              aria-label="Retry now"
              icon={<RotateCw className="h-3.5 w-3.5" />}
            >
              Retry
            </TapeDeckButton>
          )}
        </div>
      );
    },
    [processingId, currentUserId, isAdmin, issuesHook, canManage]
  );

  const rowMenu = useCallback(
    (req: RequestItem): OverflowMenuItem[] => {
      const isBusy = processingId === req.id;
      const items: OverflowMenuItem[] = [];
      if (canManage && req.status === 'pending') {
        items.push({
          key: 'approve',
          label: 'Approve',
          icon: <Check className="h-3.5 w-3.5" />,
          disabled: isBusy,
          onSelect: () => void handleApprove(req.id),
        });
        items.push({
          key: 'reject',
          label: 'Reject',
          icon: <X className="h-3.5 w-3.5" />,
          disabled: isBusy,
          onSelect: () => void handleReject(req.id),
        });
      }
      if (isAdmin && req.status_reason && req.status !== 'pending' && req.status !== 'rejected') {
        items.push({
          key: 'retry',
          label: 'Retry now',
          icon: <RotateCw className="h-3.5 w-3.5" />,
          disabled: isBusy,
          onSelect: () => void handleRetry(req.id),
        });
      }
      items.push({
        key: 'delete',
        label: 'Delete',
        icon: <Trash2 className="h-3.5 w-3.5" />,
        disabled: isBusy,
        onSelect: () => void handleRemove(req.id),
      });
      return items;
    },
    [processingId, canManage, isAdmin]
  );

  const rowTone = useCallback((req: RequestItem): 'warning' | 'error' | null => {
    if (req.status === 'rejected') return 'error';
    if (req.status_reason) return 'warning';
    return null;
  }, []);

  const rowDetails = useCallback((r: RequestItem): React.ReactNode | null => {
    const details: { label: string; value: string }[] = [];
    if (r.album) details.push({ label: 'Album', value: r.album });
    if (r.item_type) details.push({ label: 'Type', value: r.item_type });
    if (r.username) details.push({ label: 'Requested by', value: r.username });
    if (r.created_at) details.push({ label: 'Requested at', value: new Date(r.created_at).toLocaleString() });
    if (r.updated_at) details.push({ label: 'Updated at', value: new Date(r.updated_at).toLocaleString() });

    return (
      <div className="flex flex-col gap-1">
        {details.map((d) => (
          <div key={d.label}>
            <span className="text-neutral-500">{d.label}:</span> {d.value}
          </div>
        ))}
        {r.status_message && (
          <p
            className="flex items-start gap-1.5 text-[11px] text-amber-300 font-mono mt-0.5"
            role="status"
            data-reason={r.status_reason ?? undefined}
          >
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
            <span>
              {r.status_message}
              {r.next_attempt_at ? ` - next retry ${untilTime(r.next_attempt_at)}` : ''}
            </span>
          </p>
        )}
      </div>
    );
  }, []);

  const getKey = useCallback((r: RequestItem) => r.id, []);
  const rowMenuLabel = useCallback((r: RequestItem) => `Actions for ${r.title || r.artist || 'request'}`, []);

  return (
    <PageFrame
      bodyClassName="space-y-4"
      inlineActions={section === 'requests'}
      nav={
        <TabStrip fill aria-label="Request status filters">
          {filters.map((f) => (
            <TapeDeckButton
              key={f.id}
              size="sm"
              active={sub === f.id}
              onClick={() => onSubChange(f.id)}
            >
              {f.label}
            </TapeDeckButton>
          ))}
          <TapeDeckButton
            size="sm"
            active={section === 'issues'}
            onClick={() => onSubChange('issues')}
          >
            {isAdmin ? 'Issues' : 'My issues'}
            {isAdmin
              ? issuesOpenCount > 0 && (
                  <span
                    className="ml-1.5 px-1 rounded-[3px] bg-[var(--accent-amber)] text-[10px] font-mono font-bold text-black"
                    aria-label={`${issuesOpenCount} open issues`}
                  >
                    {issuesOpenCount}
                  </span>
                )
              : issuesUnreadCount > 0 && (
                  <span
                    className="ml-1.5 px-1 rounded-[3px] bg-[var(--accent-amber)] text-[10px] font-mono font-bold text-black"
                    aria-label={`${issuesUnreadCount} with new activity`}
                  >
                    {issuesUnreadCount}
                  </span>
                )}
          </TapeDeckButton>
        </TabStrip>
      }
      actions={
        section === 'requests' ? (
          <TabStrip aria-label="Request source filters">
            <TapeDeckButton
              size="sm"
              active={source === 'all' || !source}
              onClick={() => setSource('all')}
            >
              All sources
            </TapeDeckButton>
            {sources.map((s) => (
              <TapeDeckButton
                key={s.source}
                size="sm"
                active={source === s.source}
                onClick={() => setSource(s.source)}
              >
                {formatSourceLabel(s.source)}{' '}
                <span className="text-[10px] font-mono opacity-70">({s.count})</span>
              </TapeDeckButton>
            ))}
          </TabStrip>
        ) : undefined
      }
    >
      {toast && <ToastBanner message={toast.message} tone={toast.tone} />}
      {!account && quota && <QuotaBadge quota={quota} />}

      {account && (
        <MachinedCard className="p-3 sm:p-4">
          <QuotaBars account={account} isAdmin={isAdmin} compact />
          <p className="mt-2 text-[11px] font-mono text-[var(--text-muted)]">
            Remaining requests per type over a rolling {account.quotas.window_days} days.
          </p>
        </MachinedCard>
      )}

      {section === 'issues' &&
        (isAdmin ? (
          <AdminIssuesPanel
            issueId={issueId}
            onOpenIssue={onOpenIssue}
            onCloseIssue={onCloseIssue}
            onToast={showToast}
          />
        ) : (
          <MyIssuesList issuesHook={issuesHook} currentUserId={currentUserId} />
        ))}

      {section === 'requests' && (
        <div className="space-y-2">
          <div className="flex items-center justify-between text-xs font-mono text-neutral-400">
            <span>{list.total} {list.total === 1 ? 'request' : 'requests'}</span>
          </div>

          <FlatList<RequestItem>
            ariaLabel="Requests"
            columns={columns}
            list={list}
            getKey={getKey}
            sortKey="created_at"
            sortDir="desc"
            onSortChange={() => undefined}
            layout="card"
            hideMobileSortBar
            rowMenu={rowMenu}
            rowMenuLabel={rowMenuLabel}
            rowMonitor={rowMonitor}
            rowDetails={rowDetails}
            rowTone={rowTone}
            emptyMessage="No requests in this queue."
          />
        </div>
      )}
    </PageFrame>
  );
};
