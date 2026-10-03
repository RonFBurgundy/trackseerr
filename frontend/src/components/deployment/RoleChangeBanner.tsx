import React from 'react';
import { X } from 'lucide-react';
import { TapeDeckButton } from '@/components/ui';
import { formatRelativeTime } from './RequestPortalCard';
import { useRoleChangeNotice } from '@/hooks/useRoleChangeNotice';

export interface RoleChangeBannerProps {
  enabled: boolean;
}

export const RoleChangeBanner: React.FC<RoleChangeBannerProps> = ({ enabled }) => {
  const { notice, isDismissing, error, dismiss } = useRoleChangeNotice(enabled);
  if (!notice) return null;

  return (
    <div
      role="status"
      className="bg-[var(--bg-surface)] border border-[var(--accent-amber)] rounded-[4px] p-4 space-y-3"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="text-xs font-mono text-[var(--accent-amber)] font-bold uppercase">
          Role changed
          {notice.from_role && notice.to_role
            ? `: ${notice.from_role} to ${notice.to_role}`
            : notice.to_role
              ? `: now ${notice.to_role}`
              : ''}
          {notice.changed_at && (
            <span className="ml-2 font-normal text-[var(--text-muted)]">
              {formatRelativeTime(notice.changed_at)}
            </span>
          )}
        </div>
        <TapeDeckButton
          size="sm"
          onClick={() => void dismiss()}
          disabled={isDismissing}
          aria-label="Dismiss role change notice"
          icon={<X className="h-4 w-4" />}
        >
          Dismiss
        </TapeDeckButton>
      </div>
      <ul className="list-disc pl-5 space-y-1 text-xs font-mono text-[var(--text-secondary)]">
        {notice.checklist.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      {error && <p className="text-xs font-mono text-[var(--status-error)]">{error}</p>}
    </div>
  );
};
