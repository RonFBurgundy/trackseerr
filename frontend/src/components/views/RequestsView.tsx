import React, { useState } from 'react';
import { Check, X, Trash2, Clock, CheckCircle2, AlertCircle, Loader2 } from 'lucide-react';
import type { UseRequestsReturn, RequestFilter } from '@/hooks/useRequests';
import type { RequestItem } from '@/types/models';
import {
  TapeTransportBay,
  TapeDeckButton,
  MachinedCard,
  QuotaBadge,
} from '@/components/ui';
import { IssueReportButton, MyIssuesList } from '@/components/issues';
import type { UseIssuesReturn } from '@/hooks/useIssues';
import type { AccountInfo } from '@/types/account';
import { QuotaBars } from '@/components/account';

export interface RequestsViewProps {
  requestsHook: UseRequestsReturn;
  isAdmin?: boolean;
  issuesHook: UseIssuesReturn;
  currentUserId?: string | number;
  /** From GET /api/account; drives the per-type remaining-quota panel. */
  account?: AccountInfo | null;
}

export const RequestsView: React.FC<RequestsViewProps> = ({
  requestsHook,
  isAdmin = false,
  issuesHook,
  currentUserId,
  account = null,
}) => {
  const {
    requests,
    quota,
    filter,
    setFilter,
    isLoading,
    error,
    approve,
    reject,
    remove,
  } = requestsHook;

  const [processingId, setProcessingId] = useState<number | null>(null);
  const [section, setSection] = useState<'requests' | 'issues'>('requests');

  const filters: Array<{ id: RequestFilter; label: string }> = [
    { id: 'all', label: 'All' },
    { id: 'pending', label: 'Pending' },
    { id: 'approved', label: 'Approved' },
    { id: 'fulfilled', label: 'Fulfilled' },
    { id: 'rejected', label: 'Rejected' },
  ];

  const handleApprove = async (id: number) => {
    setProcessingId(id);
    try {
      await approve(id);
    } finally {
      setProcessingId(null);
    }
  };

  const handleReject = async (id: number) => {
    setProcessingId(id);
    try {
      await reject(id);
    } finally {
      setProcessingId(null);
    }
  };

  const handleRemove = async (id: number) => {
    if (!confirm('Are you sure you want to remove this request?')) return;
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
          <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[2px] bg-blue-950/60 border border-blue-800 text-blue-300 text-[11px] font-mono uppercase">
            <CheckCircle2 className="h-3 w-3" /> Approved
          </span>
        );
      case 'fulfilled':
      case 'available':
        return (
          <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[2px] bg-green-950/60 border border-green-800 text-green-300 text-[11px] font-mono uppercase">
            <Check className="h-3 w-3" /> Available
          </span>
        );
      case 'rejected':
        return (
          <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[2px] bg-red-950/60 border border-red-800 text-red-300 text-[11px] font-mono uppercase">
            <X className="h-3 w-3" /> Rejected
          </span>
        );
      case 'pending':
      default:
        return (
          <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[2px] bg-amber-950/60 border border-[#e5a00d]/60 text-[#e5a00d] text-[11px] font-mono uppercase">
            <Clock className="h-3 w-3" /> Pending
          </span>
        );
    }
  };

  return (
    <div className="space-y-6">
      {/* Top Banner / Filters */}
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <TapeTransportBay className="flex items-center gap-1.5 overflow-x-auto w-full sm:w-auto">
          {filters.map((f) => (
            <TapeDeckButton
              key={f.id}
              size="sm"
              active={section === 'requests' && filter === f.id}
              onClick={() => {
                setSection('requests');
                setFilter(f.id);
              }}
            >
              {f.label}
            </TapeDeckButton>
          ))}
          <TapeDeckButton
            size="sm"
            active={section === 'issues'}
            onClick={() => setSection('issues')}
          >
            My issues
          </TapeDeckButton>
        </TapeTransportBay>

        {!account && quota && <QuotaBadge quota={quota} />}
      </div>

      {account && (
        <MachinedCard className="p-4">
          <QuotaBars account={account} isAdmin={isAdmin} compact />
          <p className="mt-2 text-[11px] font-mono text-[var(--text-muted)]">
            Remaining requests per type over a rolling {account.quotas.window_days} days.
          </p>
        </MachinedCard>
      )}

      {section === 'issues' && <MyIssuesList issuesHook={issuesHook} />}

      {/* Loading state */}
      {section === 'requests' && isLoading && (
        <div className="flex flex-col items-center justify-center py-20 gap-3">
          <Loader2 className="h-8 w-8 text-[#e5a00d] animate-spin" />
          <span className="text-xs uppercase tracking-widest text-neutral-400 font-mono">
            Loading Request Registry...
          </span>
        </div>
      )}

      {/* Error state */}
      {section === 'requests' && error && !isLoading && (
        <div className="p-4 bg-red-950/40 border border-red-800/50 rounded-[4px] text-xs text-red-300 font-mono flex items-center gap-2">
          <AlertCircle className="h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Requests List */}
      {section === 'requests' && !isLoading && requests.length === 0 && !error && (
        <div className="text-center py-16 text-neutral-500 font-mono text-sm">
          No requests in this queue.
        </div>
      )}

      {section === 'requests' && !isLoading && requests.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {requests.map((req) => {
            const isBusy = processingId === req.id;

            return (
              <MachinedCard key={req.id} className="p-4 flex flex-col justify-between gap-4">
                <div className="flex items-start gap-3.5">
                  <img
                    src={req.cover_url || '/placeholder.svg'}
                    alt=""
                    className="h-14 w-14 rounded-[3px] object-cover border border-[#222222] shrink-0"
                    onError={(e) => {
                      (e.currentTarget as HTMLImageElement).src = '/placeholder.svg';
                    }}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2 mb-1">
                      {getStatusBadge(req.status)}
                      <span className="text-[10px] text-neutral-500 font-mono">
                        {new Date(req.created_at).toLocaleDateString()}
                      </span>
                    </div>

                    <h4 className="font-bold text-sm text-white truncate" title={req.title}>
                      {req.title}
                    </h4>
                    <p className="text-xs text-neutral-400 truncate" title={req.artist}>
                      {req.artist}
                    </p>
                    {req.requested_by_username && (
                      <p className="text-[10px] text-neutral-500 font-mono mt-1">
                        By: {req.requested_by_username}
                      </p>
                    )}
                  </div>
                </div>

                {/* Actions */}
                <div className="flex items-center justify-end gap-2 pt-2 border-t border-[#1f1f1f]">
                  {(req.status === 'fulfilled' || req.status === 'available') &&
                    currentUserId !== undefined &&
                    (req.user_id ?? req.requested_by_id) !== undefined &&
                    String(req.user_id ?? req.requested_by_id) === String(currentUserId) && (
                      <IssueReportButton
                        mediaTitle={req.title}
                        artist={req.artist}
                        requestId={String(req.id)}
                        issuesHook={issuesHook}
                      />
                    )}
                  {isAdmin && req.status === 'pending' && (
                    <>
                      <TapeDeckButton
                        size="sm"
                        variant="amber"
                        disabled={isBusy}
                        onClick={() => handleApprove(req.id)}
                        icon={<Check className="h-3.5 w-3.5" />}
                      >
                        Approve
                      </TapeDeckButton>
                      <TapeDeckButton
                        size="sm"
                        variant="danger"
                        disabled={isBusy}
                        onClick={() => handleReject(req.id)}
                        icon={<X className="h-3.5 w-3.5" />}
                      >
                        Reject
                      </TapeDeckButton>
                    </>
                  )}

                  <TapeDeckButton
                    size="sm"
                    variant="danger"
                    disabled={isBusy}
                    onClick={() => handleRemove(req.id)}
                    icon={<Trash2 className="h-3.5 w-3.5" />}
                    aria-label="Delete request"
                  />
                </div>
              </MachinedCard>
            );
          })}
        </div>
      )}
    </div>
  );
};
