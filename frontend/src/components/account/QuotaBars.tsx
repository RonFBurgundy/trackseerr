import React from 'react';
import { Check } from 'lucide-react';
import { QUOTA_KINDS, QUOTA_LABELS } from '@/types/account';
import type { AccountInfo } from '@/types/account';

export interface QuotaBarsProps {
  account: AccountInfo;
  /** Only hides the rolling-window note; unlimited is driven by null limits from the server. */
  isAdmin?: boolean;
  /** Compact variant for the Requests view. */
  compact?: boolean;
}

export const QuotaBars: React.FC<QuotaBarsProps> = ({ account, isAdmin = false, compact = false }) => {
  const { quotas, auto_approve: autoApprove } = account;

  return (
    <div className={compact ? 'grid grid-cols-1 sm:grid-cols-3 gap-3' : 'space-y-4'}>
      {QUOTA_KINDS.map((kind) => {
        const limit = quotas[kind];
        const used = quotas.used[kind];
        const unlimited = limit === null;
        const remaining = limit === null ? 0 : Math.max(0, limit - used);
        const exhausted = !unlimited && remaining === 0;
        const pct = unlimited ? 0 : limit === null || limit === 0 ? 100 : Math.min(100, (used / limit) * 100);

        return (
          <div key={kind} className="space-y-1.5">
            <div className="flex items-center justify-between gap-2 text-xs font-mono">
              <span className="uppercase tracking-wider text-[var(--text-secondary)]">
                {QUOTA_LABELS[kind]}
              </span>
              <span className="flex items-center gap-2">
                {autoApprove[kind] && (
                  <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[2px] border border-[var(--status-success)] text-[var(--status-success)] text-[10px]">
                    <Check className="h-3 w-3" /> Auto-approve
                  </span>
                )}
                <span className={exhausted ? 'text-[var(--status-error)]' : 'text-[var(--text-primary)]'}>
                  {unlimited ? 'Unlimited' : `${remaining} left (${used}/${limit})`}
                </span>
              </span>
            </div>
            <div
              className="h-2 bg-[var(--bg-canvas)] border border-[var(--border-default)] rounded-[2px] overflow-hidden"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={limit ?? undefined}
              aria-valuenow={unlimited ? undefined : used}
              aria-label={`${QUOTA_LABELS[kind]} quota used`}
            >
              <div
                className={`h-full ${exhausted ? 'bg-[var(--status-error)]' : 'bg-[var(--accent-amber)]'}`}
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        );
      })}
      {!compact && !isAdmin && (
        <p className="text-[11px] font-mono text-[var(--text-muted)]">
          Rolling window: {quotas.window_days} days
        </p>
      )}
    </div>
  );
};
