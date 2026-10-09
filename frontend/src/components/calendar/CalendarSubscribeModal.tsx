import React, { useState } from 'react';
import { Check, Copy, KeyRound } from 'lucide-react';
import { ObsidianModal, TapeDeckButton } from '@/components/ui';
import { getCalendarFeedUrl } from '@/services/calendarService';

export interface CalendarSubscribeModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export const CalendarSubscribeModal: React.FC<CalendarSubscribeModalProps> = ({ isOpen, onClose }) => {
  const [copied, setCopied] = useState(false);
  const feedUrl = getCalendarFeedUrl();

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(feedUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // fallback
    }
  };

  return (
    <ObsidianModal
      isOpen={isOpen}
      onClose={onClose}
      title="Subscribe to Release Calendar"
      subtitle="Sync upcoming and recent releases to Apple Calendar, Google Calendar, or Thunderbird"
      maxWidth="sm:max-w-xl"
    >
      <div className="space-y-4 py-2">
        <div className="p-3 rounded-[4px] bg-[#141414] border border-[#222222] space-y-2">
          <label
            htmlFor="calendar-feed-url"
            className="block text-xs font-mono font-semibold uppercase tracking-wider text-[var(--text-secondary)]"
          >
            iCal Feed URL
          </label>
          <div className="flex items-center gap-2">
            <input
              id="calendar-feed-url"
              name="calendar_feed_url"
              type="text"
              readOnly
              value={feedUrl}
              className="flex-1 px-2.5 py-1.5 text-xs font-mono bg-[#0d0d0d] text-white border border-[#262626] rounded-[3px] focus:outline-none focus:border-[var(--accent-amber)]"
              onFocus={(e) => e.currentTarget.select()}
            />
            <TapeDeckButton
              type="button"
              size="sm"
              variant="amber"
              onClick={handleCopy}
              icon={copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
            >
              {copied ? 'Copied' : 'Copy URL'}
            </TapeDeckButton>
          </div>
        </div>

        <div className="flex items-start gap-2.5 p-3 rounded-[4px] bg-[#121212] border border-[#262626] text-xs font-mono text-[var(--text-secondary)]">
          <KeyRound className="h-4 w-4 text-[var(--accent-amber)] shrink-0 mt-0.5" />
          <p className="leading-relaxed">
            Authentication required: calendar apps must supply your configured feed token (e.g.{' '}
            <code className="text-white bg-[#1a1a1a] px-1 py-0.5 rounded-[2px]">?token=&lt;feed_token&gt;</code>) or API
            key.
          </p>
        </div>
      </div>
    </ObsidianModal>
  );
};
