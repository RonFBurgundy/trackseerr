import React from 'react';
import type { UserQuota } from '@/types/models';

export interface QuotaBadgeProps {
  quota: UserQuota | null;
  className?: string;
}

export const QuotaBadge: React.FC<QuotaBadgeProps> = ({ quota, className = '' }) => {
  if (!quota) return null;

  const isLow = quota.remaining <= 1;

  return (
    <div
      className={`inline-flex items-center gap-2 px-2.5 py-0.5 sm:py-1 bg-[#121212] border border-[#222222] rounded-[3px] text-xs font-mono select-none ${className}`}
      title={`Request Quota: ${quota.remaining} remaining out of ${quota.limit} every ${quota.period_days} days`}
    >
      <span
        className={`h-2 w-2 rounded-full ${
          isLow
            ? 'bg-red-500 shadow-[0_0_6px_rgba(239,68,68,0.8)]'
            : 'bg-[#e5a00d] shadow-[0_0_6px_rgba(229,160,13,0.8)]'
        }`}
      />
      <span className="text-neutral-400">QUOTA:</span>
      <span className={`font-bold ${isLow ? 'text-red-400' : 'text-neutral-200'}`}>
        {quota.remaining}/{quota.limit}
      </span>
      <span className="text-neutral-500 text-[10px]">({quota.period_days}d)</span>
    </div>
  );
};
