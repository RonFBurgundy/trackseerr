import React from 'react';
import type { CalendarItem } from '@/types/calendar';
import { CalendarAlbumChip } from './CalendarAlbumChip';
import { toDateKey } from '@/hooks/useCalendar';

export interface CalendarGridProps {
  currentDate: Date;
  itemsByDate: Map<string, CalendarItem[]>;
  onNavigateToAlbum?: (item: CalendarItem) => void;
}

const WEEK_DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

export const CalendarGrid: React.FC<CalendarGridProps> = ({
  currentDate,
  itemsByDate,
  onNavigateToAlbum,
}) => {
  const currentYear = currentDate.getFullYear();
  const currentMonth = currentDate.getMonth();

  const todayKey = toDateKey(new Date());

  // Generate 6 weeks (42 days) of grid cells starting from Sunday
  const firstOfMonth = new Date(currentYear, currentMonth, 1);
  const startDay = firstOfMonth.getDay(); // 0 (Sun) .. 6 (Sat)
  const gridStartDate = new Date(currentYear, currentMonth, 1 - startDay);

  const days = React.useMemo(() => {
    const list: Array<{ date: Date; dateKey: string; isCurrentMonth: boolean; isToday: boolean }> = [];
    for (let i = 0; i < 42; i++) {
      const d = new Date(gridStartDate.getFullYear(), gridStartDate.getMonth(), gridStartDate.getDate() + i);
      const dateKey = toDateKey(d);
      list.push({
        date: d,
        dateKey,
        isCurrentMonth: d.getMonth() === currentMonth,
        isToday: dateKey === todayKey,
      });
    }
    return list;
  }, [gridStartDate, currentMonth, todayKey]);

  return (
    <div className="w-full flex flex-col rounded-[4px] border border-[#222222] bg-[#0d0d0d] overflow-hidden shadow-xl">
      {/* Week day headers */}
      <div className="grid grid-cols-7 border-b border-[#222222] bg-[#141414]">
        {WEEK_DAYS.map((day) => (
          <div
            key={day}
            className="py-2.5 text-center text-xs font-mono font-semibold uppercase tracking-wider text-[var(--text-secondary)]"
          >
            {day}
          </div>
        ))}
      </div>

      {/* Days grid */}
      <div className="grid grid-cols-7 auto-rows-fr divide-x divide-y divide-[#1f1f1f] bg-[#0a0a0a]">
        {days.map(({ date, dateKey, isCurrentMonth, isToday }) => {
          const dayItems = itemsByDate.get(dateKey) || [];

          return (
            <div
              key={dateKey}
              className={`min-h-[110px] p-1.5 flex flex-col gap-1 transition-colors duration-150 ${
                isCurrentMonth ? 'bg-[#0f0f0f]' : 'bg-[#0a0a0a]/60 opacity-40'
              } ${isToday ? 'ring-1 ring-inset ring-[var(--accent-amber)]/60 bg-[#16130d]' : ''}`}
            >
              {/* Day header row */}
              <div className="flex items-center justify-between px-1">
                {isToday ? (
                  <span className="text-[10px] font-mono font-bold uppercase tracking-wider text-[var(--accent-amber)] bg-[var(--accent-amber)]/10 px-1 rounded-[2px]">
                    Today
                  </span>
                ) : (
                  <span />
                )}
                <span
                  className={`text-xs font-mono font-semibold ${
                    isToday
                      ? 'text-[var(--accent-amber)] font-bold'
                      : isCurrentMonth
                      ? 'text-[var(--text-primary)]'
                      : 'text-[var(--text-muted)]'
                  }`}
                >
                  {date.getDate()}
                </span>
              </div>

              {/* Day albums list */}
              <div className="flex-1 flex flex-col gap-1 overflow-y-auto max-h-[120px] pr-0.5">
                {dayItems.map((item) => (
                  <CalendarAlbumChip key={item.id} item={item} onClick={onNavigateToAlbum} />
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
