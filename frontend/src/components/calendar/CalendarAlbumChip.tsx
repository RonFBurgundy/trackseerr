import React from 'react';
import { Disc3 } from 'lucide-react';
import type { CalendarItem, CalendarStatus } from '@/types/calendar';
import { routeToHash } from '@/hooks/useAppRoute';

export interface CalendarAlbumChipProps {
  item: CalendarItem;
  onClick?: (item: CalendarItem) => void;
}

const STATUS_INDICATOR_CLASS: Record<CalendarStatus, string> = {
  downloaded: 'bg-[var(--status-success)] shadow-[0_0_4px_var(--status-success)]',
  partial: 'bg-[var(--accent-amber)] shadow-[0_0_4px_var(--accent-amber)]',
  missing: 'bg-[var(--status-error)] shadow-[0_0_4px_var(--status-error)]',
  upcoming: 'bg-[var(--text-secondary)]',
};

const STATUS_BORDER_CLASS: Record<CalendarStatus, string> = {
  downloaded: 'border-l-2 border-l-[var(--status-success)]',
  partial: 'border-l-2 border-l-[var(--accent-amber)]',
  missing: 'border-l-2 border-l-[var(--status-error)]',
  upcoming: 'border-l-2 border-l-[var(--border-default)]',
};

export const CalendarAlbumChip: React.FC<CalendarAlbumChipProps> = ({ item, onClick }) => {
  const href = routeToHash({
    tab: 'library',
    sub: 'artists',
    detail: { artistId: item.artist_id, albumId: item.id },
  });

  const handleClick = (e: React.MouseEvent<HTMLAnchorElement>) => {
    if (onClick && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
      onClick(item);
    }
  };

  const statusKey = (item.status as CalendarStatus) || 'upcoming';
  const indicator = STATUS_INDICATOR_CLASS[statusKey] ?? STATUS_INDICATOR_CLASS.upcoming;
  const borderHighlight = STATUS_BORDER_CLASS[statusKey] ?? STATUS_BORDER_CLASS.upcoming;

  return (
    <a
      href={href}
      onClick={handleClick}
      className={`group flex items-center gap-1.5 px-1.5 py-1 rounded-[3px] bg-[#121212] hover:bg-[#1a1a1a] border border-[#222222] hover:border-[var(--border-hover)] text-xs transition-colors duration-100 select-none overflow-hidden ${borderHighlight}`}
      title={`${item.artist_name} - ${item.title} (${item.status})`}
    >
      {/* Cover thumb */}
      <div className="relative h-5 w-5 shrink-0 rounded-[2px] overflow-hidden bg-[#181818] flex items-center justify-center">
        {item.cover_url ? (
          <img
            src={item.cover_url}
            alt=""
            loading="lazy"
            decoding="async"
            width={20}
            height={20}
            className="h-full w-full object-cover"
          />
        ) : (
          <Disc3 className="h-3 w-3 text-[var(--text-muted)]" />
        )}
      </div>

      {/* Info */}
      <div className="flex-1 min-w-0 flex items-center gap-1">
        <span className="font-semibold text-white truncate max-w-[120px]">{item.title}</span>
        <span className="text-[10px] text-[var(--text-muted)] truncate max-w-[80px]">— {item.artist_name}</span>
      </div>

      {/* Status indicator jewel */}
      <span
        aria-hidden="true"
        className={`inline-block h-1.5 w-1.5 rounded-full shrink-0 ${indicator}`}
        title={`Status: ${item.status}`}
      />
    </a>
  );
};
