import React from 'react';
import { Calendar as CalendarIcon, Disc3 } from 'lucide-react';
import type { CalendarItem, CalendarStatus } from '@/types/calendar';
import { CalendarStatusChip } from './CalendarStatusChip';
import { routeToHash } from '@/hooks/useAppRoute';
import { toDateKey } from '@/hooks/useCalendar';

export interface CalendarAgendaProps {
  items: CalendarItem[];
  onNavigateToAlbum?: (item: CalendarItem) => void;
}

function formatDayHeader(dateStr: string): string {
  try {
    const parts = dateStr.split('-');
    if (parts.length === 3) {
      const d = new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
      return d.toLocaleDateString('default', {
        weekday: 'short',
        month: 'short',
        day: 'numeric',
      });
    }
  } catch {
    // fallback
  }
  return dateStr;
}

export const CalendarAgenda: React.FC<CalendarAgendaProps> = ({ items, onNavigateToAlbum }) => {
  const todayKey = toDateKey(new Date());

  // Group items by release_date
  const grouped = React.useMemo(() => {
    const map = new Map<string, CalendarItem[]>();
    for (const item of items) {
      const dateKey = item.release_date;
      const list = map.get(dateKey);
      if (list) {
        list.push(item);
      } else {
        map.set(dateKey, [item]);
      }
    }
    // Sort ascending by date
    const sortedEntries = Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
    return sortedEntries;
  }, [items]);

  if (items.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center p-8 text-center rounded-[4px] border border-[#222222] bg-[#121212]">
        <CalendarIcon className="h-10 w-10 text-[var(--text-muted)] mb-3 opacity-60" />
        <h3 className="text-sm font-semibold text-[var(--text-primary)]">No Releases Found</h3>
        <p className="text-xs text-[var(--text-secondary)] font-mono mt-1">
          There are no releases recorded for monitored artists in this period.
        </p>
      </div>
    );
  }

  return (
    <div className="w-full space-y-4">
      {grouped.map(([dateKey, dayItems]) => {
        const isToday = dateKey === todayKey;
        const dayLabel = formatDayHeader(dateKey);

        return (
          <div key={dateKey} className="space-y-2">
            {/* Day Header */}
            <div className="sticky top-0 z-10 flex items-center justify-between py-1 px-2 rounded-[3px] bg-[#161616] border border-[#262626] backdrop-blur">
              <span className="text-xs font-mono font-bold uppercase tracking-wider text-[var(--text-primary)] flex items-center gap-2">
                {dayLabel}
                {isToday && (
                  <span className="text-[10px] font-bold text-[var(--accent-amber)] bg-[var(--accent-amber)]/10 px-1.5 py-0.2 rounded-[2px]">
                    Today
                  </span>
                )}
              </span>
              <span className="text-[11px] font-mono text-[var(--text-muted)]">
                {dayItems.length} {dayItems.length === 1 ? 'release' : 'releases'}
              </span>
            </div>

            {/* List of items on this day */}
            <div className="space-y-1.5">
              {dayItems.map((item) => {
                const href = routeToHash({
                  tab: 'library',
                  sub: 'artists',
                  detail: { artistId: item.artist_id, albumId: item.id },
                });

                return (
                  <a
                    key={item.id}
                    href={href}
                    onClick={(e) => {
                      if (onNavigateToAlbum && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
                        onNavigateToAlbum(item);
                      }
                    }}
                    className="flex items-center gap-3 p-2.5 rounded-[4px] bg-[#141414] hover:bg-[#1a1a1a] border border-[#222222] hover:border-[var(--border-hover)] transition-colors duration-100 group"
                  >
                    {/* Cover art */}
                    <div className="relative h-12 w-12 shrink-0 rounded-[3px] overflow-hidden bg-[#181818] border border-[#262626] flex items-center justify-center">
                      {item.cover_url ? (
                        <img
                          src={item.cover_url}
                          alt=""
                          loading="lazy"
                          decoding="async"
                          width={48}
                          height={48}
                          className="h-full w-full object-cover"
                        />
                      ) : (
                        <Disc3 className="h-6 w-6 text-[var(--text-muted)]" />
                      )}
                    </div>

                    {/* Details */}
                    <div className="flex-1 min-w-0 flex flex-col justify-center">
                      <div className="flex items-center gap-1.5 truncate">
                        <span className="font-semibold text-sm text-white group-hover:text-[var(--accent-amber)] truncate transition-colors">
                          {item.title}
                        </span>
                        {item.album_type && (
                          <span className="text-[10px] font-mono uppercase text-[var(--text-muted)] border border-[#262626] px-1 rounded-[2px] shrink-0">
                            {item.album_type}
                          </span>
                        )}
                      </div>
                      <span className="text-xs text-[var(--text-secondary)] truncate">{item.artist_name}</span>
                    </div>

                    {/* Status Chip */}
                    <div className="shrink-0 flex items-center gap-1.5">
                      <CalendarStatusChip status={(item.status as CalendarStatus) || 'upcoming'} size="xs" />
                    </div>
                  </a>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
};
