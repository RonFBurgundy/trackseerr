import React, { useState } from 'react';
import { ChevronLeft, ChevronRight, Loader2, Rss } from 'lucide-react';
import { PageFrame } from '@/components/layout';
import { TapeDeckButton, ToastBanner } from '@/components/ui';
import { CalendarAgenda, CalendarGrid, CalendarSubscribeModal } from '@/components/calendar';
import { useCalendar } from '@/hooks/useCalendar';
import { useRefreshHandler } from '@/hooks/useRefreshHandler';
import { useToast } from '@/hooks/useToast';
import type { AppRoute, NavigateOptions } from '@/hooks/useAppRoute';
import type { CalendarItem } from '@/types/calendar';

export interface CalendarViewProps {
  onNavigate?: (route: AppRoute, options?: NavigateOptions) => void;
  tabs?: React.ReactNode;
}

export const CalendarView: React.FC<CalendarViewProps> = ({ onNavigate, tabs }) => {
  const { toast } = useToast();
  const [isSubscribeOpen, setIsSubscribeOpen] = useState(false);

  const {
    currentDate,
    monthLabel,
    items,
    itemsByDate,
    isLoading,
    error,
    showUnmonitored,
    goToPrevMonth,
    goToNextMonth,
    goToToday,
    toggleUnmonitored,
    refetch,
  } = useCalendar();

  useRefreshHandler(() => void refetch());

  const handleOpenAlbum = (item: CalendarItem) => {
    if (onNavigate) {
      onNavigate({
        tab: 'library',
        sub: 'artists',
        detail: { artistId: item.artist_id, albumId: item.id },
      });
    }
  };

  return (
    <PageFrame
      bodyClassName="space-y-4"
      nav={
        <div className="space-y-2">
          {tabs}
          {toast && <ToastBanner message={toast.message} tone={toast.tone} />}
          {error && <ToastBanner message={error} tone="error" />}

          {/* Recessed transport bay toolbar */}
          <div className="tape-transport-bay flex flex-wrap items-center justify-between gap-2 p-1 bg-[#0d0d0d] border border-[#1f1f1f] rounded-[4px]">
            {/* Month navigation controls */}
            <div className="flex items-center gap-1.5">
              <TapeDeckButton
                type="button"
                size="sm"
                onClick={goToPrevMonth}
                aria-label="Previous month"
                icon={<ChevronLeft className="h-4 w-4" />}
              />
              <TapeDeckButton type="button" size="sm" onClick={goToToday}>
                Today
              </TapeDeckButton>
              <TapeDeckButton
                type="button"
                size="sm"
                onClick={goToNextMonth}
                aria-label="Next month"
                icon={<ChevronRight className="h-4 w-4" />}
              />
              <span className="ml-2 font-mono text-xs sm:text-sm font-bold uppercase tracking-wider text-white flex items-center gap-2">
                {monthLabel}
                {isLoading && <Loader2 className="h-3 w-3 animate-spin text-[var(--accent-amber)]" />}
              </span>
            </div>

            {/* Actions & Filters */}
            <div className="flex items-center gap-2">
              {/* Show unmonitored toggle */}
              <label
                htmlFor="calendar-show-unmonitored"
                className="flex items-center gap-1.5 px-2 py-1 rounded-[3px] bg-[#141414] border border-[#222222] hover:border-[var(--border-hover)] cursor-pointer text-xs font-mono select-none"
              >
                <input
                  id="calendar-show-unmonitored"
                  name="calendar_show_unmonitored"
                  type="checkbox"
                  checked={showUnmonitored}
                  onChange={toggleUnmonitored}
                  className="rounded-[2px] bg-[#0a0a0a] border-[#333333] text-[var(--accent-amber)] focus:ring-0 focus:ring-offset-0 cursor-pointer h-3.5 w-3.5"
                />
                <span className="text-[var(--text-secondary)] text-[11px] sm:text-xs">Show unmonitored</span>
              </label>

              {/* Subscribe button */}
              <TapeDeckButton
                type="button"
                size="sm"
                variant="amber"
                onClick={() => setIsSubscribeOpen(true)}
                icon={<Rss className="h-3.5 w-3.5" />}
              >
                Subscribe (iCal)
              </TapeDeckButton>
            </div>
          </div>
        </div>
      }
    >
      {/* Desktop View: Month Grid */}
      <div className="hidden md:block">
        <CalendarGrid
          currentDate={currentDate}
          itemsByDate={itemsByDate}
          onNavigateToAlbum={handleOpenAlbum}
        />
      </div>

      {/* Mobile View: Agenda List */}
      <div className="block md:hidden">
        <CalendarAgenda items={items} onNavigateToAlbum={handleOpenAlbum} />
      </div>

      {/* Subscribe Modal */}
      <CalendarSubscribeModal
        isOpen={isSubscribeOpen}
        onClose={() => setIsSubscribeOpen(false)}
      />
    </PageFrame>
  );
};
