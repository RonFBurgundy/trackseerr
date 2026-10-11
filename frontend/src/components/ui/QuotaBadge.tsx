import React from 'react';
import type { QuotaTypeStatus, UserQuota } from '@/types/models';

export interface QuotaBadgeProps {
  quota: UserQuota | null;
  className?: string;
}

const KIND_LABEL: Record<QuotaTypeStatus['kind'], string> = {
  tracks: 'Tracks',
  albums: 'Albums',
  discographies: 'Discographies',
};

/** The type with the least headroom left (lowest remaining, then lowest ratio). */
function tightest(types: QuotaTypeStatus[]): QuotaTypeStatus | null {
  let best: QuotaTypeStatus | null = null;
  for (const t of types) {
    if (!best || t.remaining < best.remaining) best = t;
  }
  return best;
}

export const QuotaBadge: React.FC<QuotaBadgeProps> = ({ quota, className = '' }) => {
  if (!quota) return null;

  const shell = `inline-flex items-center gap-2 px-2.5 py-0.5 sm:py-1 bg-[#121212] border border-[#222222] rounded-[3px] text-xs font-mono select-none ${className}`;

  if (quota.unlimited) {
    return (
      <div className={shell} title="Request quota: unlimited">
        <span className="h-2 w-2 rounded-full bg-[#e5a00d] shadow-[0_0_6px_rgba(229,160,13,0.8)]" />
        <span className="text-neutral-400">QUOTA:</span>
        <span className="font-bold text-neutral-200">&infin; UNLIMITED</span>
      </div>
    );
  }

  const worst = tightest(quota.types);
  if (!worst) return null;

  const isLow = worst.remaining <= 1;
  const tooltip = quota.types
    .map((t) => `${KIND_LABEL[t.kind]}: ${t.remaining} remaining of ${t.limit}`)
    .join('\n');

  return (
    <div className={shell} title={`Request quota every ${quota.period_days} days\n${tooltip}`}>
      <span
        className={`h-2 w-2 rounded-full ${
          isLow
            ? 'bg-red-500 shadow-[0_0_6px_rgba(239,68,68,0.8)]'
            : 'bg-[#e5a00d] shadow-[0_0_6px_rgba(229,160,13,0.8)]'
        }`}
      />
      <span className="text-neutral-400">{KIND_LABEL[worst.kind].toUpperCase()}:</span>
      <span className={`font-bold ${isLow ? 'text-red-400' : 'text-neutral-200'}`}>
        {worst.remaining} left
      </span>
      <span className="text-neutral-500 text-[10px]">of {worst.limit}</span>
      <span className="text-neutral-500 text-[10px]">({quota.period_days}d)</span>
    </div>
  );
};
