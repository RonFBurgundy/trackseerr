import React from 'react';
import { Check, Loader2, Plus } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';
import type { DiscoveryStatus } from '@/types/models';
import { isOwnedStatus, isPendingStatus } from './profileItems';

export interface ProfileRequestButtonProps {
  status: DiscoveryStatus;
  /** A request for this item is in flight. */
  busy: boolean;
  onRequest: () => void;
  /** Label of the actionable state. */
  label?: string;
  size?: 'sm' | 'md';
  className?: string;
}

/** Request key following the Discover card conventions: disabled "In Library / Requested / Processing", else amber Request. */
export const ProfileRequestButton: React.FC<ProfileRequestButtonProps> = React.memo(
  ({ status, busy, onRequest, label = 'Request', size = 'sm', className = '' }) => {
    const iconClass = size === 'sm' ? 'h-3.5 w-3.5' : 'h-4 w-4';
    if (isOwnedStatus(status)) {
      return (
        <TapeDeckButton size={size} disabled className={`text-emerald-400 border-emerald-500/30 bg-emerald-950/20 ${className}`} icon={<Check className={iconClass} />}>
          In Library
        </TapeDeckButton>
      );
    }
    if (isPendingStatus(status)) {
      return (
        <TapeDeckButton size={size} disabled className={`text-[#e5a00d] border-[#e5a00d]/30 ${className}`} icon={<Check className={iconClass} />}>
          Requested
        </TapeDeckButton>
      );
    }
    if (busy || status === 'processing') {
      return (
        <TapeDeckButton size={size} disabled className={`text-blue-400 border-blue-500/30 ${className}`} icon={<Loader2 className={`${iconClass} animate-spin`} />}>
          Processing
        </TapeDeckButton>
      );
    }
    return (
      <TapeDeckButton
        size={size}
        variant="amber"
        className={className}
        onClick={(e) => {
          e.stopPropagation();
          onRequest();
        }}
        icon={<Plus className={iconClass} />}
      >
        {label}
      </TapeDeckButton>
    );
  }
);
ProfileRequestButton.displayName = 'ProfileRequestButton';
