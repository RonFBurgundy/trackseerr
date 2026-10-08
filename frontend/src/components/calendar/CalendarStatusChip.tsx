import React from 'react';
import type { CalendarStatus } from '@/types/calendar';

export interface CalendarStatusChipProps {
  status: CalendarStatus;
  className?: string;
  size?: 'sm' | 'xs';
}

const STATUS_STYLES: Record<CalendarStatus, { label: string; dotClass: string; badgeClass: string }> = {
  downloaded: {
    label: 'Downloaded',
    dotClass: 'bg-[var(--status-success)] shadow-[0_0_5px_var(--status-success)]',
    badgeClass: 'border-[var(--status-success)]/40 bg-[var(--status-success)]/10 text-[var(--status-success)]',
  },
  partial: {
    label: 'Partial',
    dotClass: 'bg-[var(--accent-amber)] shadow-[0_0_5px_var(--accent-amber)]',
    badgeClass: 'border-[var(--accent-amber)]/40 bg-[var(--accent-amber)]/10 text-[var(--accent-amber)]',
  },
  missing: {
    label: 'Missing',
    dotClass: 'bg-[var(--status-error)] shadow-[0_0_5px_var(--status-error)]',
    badgeClass: 'border-[var(--status-error)]/40 bg-[var(--status-error)]/10 text-[var(--status-error)]',
  },
  upcoming: {
    label: 'Upcoming',
    dotClass: 'bg-[var(--text-secondary)]',
    badgeClass: 'border-[var(--border-default)] bg-[#181818] text-[var(--text-secondary)]',
  },
};

export const CalendarStatusChip: React.FC<CalendarStatusChipProps> = ({ status, className = '', size = 'xs' }) => {
  const meta = STATUS_STYLES[status] ?? STATUS_STYLES.upcoming;
  const padding = size === 'sm' ? 'px-2 py-0.5 text-[11px]' : 'px-1.5 py-0.2 text-[10px]';

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-[3px] border font-mono uppercase tracking-wider font-semibold ${padding} ${meta.badgeClass} ${className}`}
    >
      <span aria-hidden="true" className={`inline-block h-1.5 w-1.5 rounded-full shrink-0 ${meta.dotClass}`} />
      <span>{meta.label}</span>
    </span>
  );
};
