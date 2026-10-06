import React from 'react';
import { Check } from 'lucide-react';
import type { DiscoveryStatus } from '@/types/models';
import { isOwnedStatus, isPendingStatus } from './profileItems';

export interface ProfileStatusBadgeProps {
  status: DiscoveryStatus;
  /** Tracks owned / total, shown for `partial`. */
  have?: number | null;
  total?: number | null;
  className?: string;
}

const BASE = 'inline-flex items-center gap-1 rounded-[2px] px-1.5 py-0.5 text-[10px] font-mono font-bold whitespace-nowrap';

/** Small status chip; renders nothing for statuses with nothing to say (`none`, `missing`). */
export const ProfileStatusBadge: React.FC<ProfileStatusBadgeProps> = React.memo(({ status, have, total, className = '' }) => {
  if (isOwnedStatus(status)) {
    return (
      <span className={`${BASE} bg-emerald-500/90 text-black ${className}`}>
        <Check className="h-3 w-3" /> In Library
      </span>
    );
  }
  if (status === 'partial') {
    const counts = have != null && total != null ? ` ${have}/${total}` : '';
    return <span className={`${BASE} bg-[#e5a00d]/15 text-[#e5a00d] border border-[#e5a00d]/40 ${className}`}>Partial{counts}</span>;
  }
  if (isPendingStatus(status)) {
    return <span className={`${BASE} bg-[#e5a00d]/90 text-black ${className}`}>Requested</span>;
  }
  if (status === 'processing') {
    return <span className={`${BASE} bg-blue-500/90 text-white ${className}`}>Processing</span>;
  }
  if (status === 'rejected') {
    return <span className={`${BASE} bg-red-950/60 text-red-300 border border-red-800/50 ${className}`}>Rejected</span>;
  }
  return null;
});
ProfileStatusBadge.displayName = 'ProfileStatusBadge';
