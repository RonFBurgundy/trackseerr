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

  const heading =
    notice.from_role && notice.to_role
      ? `Role changed: ${notice.from_role} to ${notice.to_role}`
      : notice.to_role
        ? `Role changed: now ${notice.to_role}`
        : 'Role changed';
  const checklist = notice.checklist.join(' | ');
  const fullText = [heading, checklist, error].filter(Boolean).join(' - ');

  return (
    <div
      role="status"
      title={fullText}
      className="bg-[var(--bg-surface)] border border-[var(--accent-amber)] rounded-[4px] pl-3 pr-1 py-1 flex items-center gap-2 min-w-0"
    >
      <div className="min-w-0 flex-1 truncate text-xs font-mono">
        <span className="text-[var(--accent-amber)] font-bold uppercase">{heading}</span>
        {notice.changed_at && (
          <span className="ml-2 text-[var(--text-muted)]">{formatRelativeTime(notice.changed_at)}</span>
        )}
        {checklist && <span className="ml-2 text-[var(--text-secondary)]">{checklist}</span>}
        {error && <span className="ml-2 text-[var(--status-error)]">{error}</span>}
      </div>
      <TapeDeckButton
        size="sm"
        className="shrink-0"
        onClick={() => void dismiss()}
        disabled={isDismissing}
        aria-label="Dismiss role change notice"
        title="Dismiss"
        icon={<X className="h-4 w-4" />}
        collapseLabel
      >
        Dismiss
      </TapeDeckButton>
    </div>
  );
};
