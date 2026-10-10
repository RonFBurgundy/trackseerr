import React, { useState } from 'react';
import { Ban, ClipboardCheck, DownloadCloud, History } from 'lucide-react';
import { PageFrame } from '@/components/layout';
import { TabStrip, TapeDeckButton, ToastBanner } from '@/components/ui';
import { ActivityQueuePanel, ActivityHistoryPanel, ActivityBlocklistPanel, NeedsReviewPanel } from '@/components/activity';
import { useToast } from '@/hooks/useToast';
import { useRefreshHandler } from '@/hooks/useRefreshHandler';
import type { ActivitySub, NavigateOptions } from '@/hooks/useAppRoute';

const noop = (): void => undefined;

const TABS: Array<{ id: ActivitySub; label: string; icon: React.ReactNode }> = [
  { id: 'queue', label: 'Queue', icon: <DownloadCloud className="h-3.5 w-3.5" /> },
  { id: 'history', label: 'History', icon: <History className="h-3.5 w-3.5" /> },
  { id: 'blocklist', label: 'Blocklist', icon: <Ban className="h-3.5 w-3.5" /> },
  { id: 'review', label: 'Needs review', icon: <ClipboardCheck className="h-3.5 w-3.5" /> },
];

/** Admin-only. Only the active sub-tab is mounted, so queue polling stops when it is left. */
export interface ActivityViewProps {
  sub: ActivitySub;
  onSubChange: (sub: ActivitySub, options?: NavigateOptions) => void;
  /** Library-health finding count, shown on the Needs review key. */
  reviewCount?: number;
  /** Refreshes the nav badge after findings change. */
  onReviewChanged?: () => void;
}

export const ActivityView: React.FC<ActivityViewProps> = ({
  sub: tab,
  onSubChange,
  reviewCount = 0,
  onReviewChanged,
}) => {
  const { toast, showToast } = useToast();
  // Panels own their paged data; bumping the key remounts the active one, which reloads it from the server.
  const [reloadKey, setReloadKey] = useState<number>(0);
  useRefreshHandler(() => setReloadKey((k) => k + 1));

  return (
    <PageFrame
      bodyClassName="space-y-6"
      nav={
      <>
      {toast && <ToastBanner message={toast.message} tone={toast.tone} />}
      <TabStrip aria-label="Activity sections">
        {TABS.map((t) => (
          <TapeDeckButton key={t.id} size="sm" active={tab === t.id} onClick={() => onSubChange(t.id)} icon={t.icon}>
            {t.label}
            {t.id === 'review' && reviewCount > 0 && (
              <span className="ml-1.5 px-1 rounded-[3px] bg-[var(--accent-amber)] text-[10px] font-mono font-bold text-black">{reviewCount}</span>
            )}
          </TapeDeckButton>
        ))}
      </TabStrip>
      </>
      }
    >
      <React.Fragment key={reloadKey}>
      {tab === 'queue' && <ActivityQueuePanel onToast={showToast} />}
      {tab === 'history' && <ActivityHistoryPanel onToast={showToast} />}
      {tab === 'blocklist' && <ActivityBlocklistPanel onToast={showToast} />}
      {tab === 'review' && <NeedsReviewPanel onToast={showToast} onChanged={onReviewChanged ?? noop} />}
      </React.Fragment>
    </PageFrame>
  );
};
