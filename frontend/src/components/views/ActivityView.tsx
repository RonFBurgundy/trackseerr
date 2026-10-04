import React, { useState } from 'react';
import { Ban, DownloadCloud, History } from 'lucide-react';
import { TabStrip, TapeDeckButton, ToastBanner } from '@/components/ui';
import { ActivityQueuePanel, ActivityHistoryPanel, ActivityBlocklistPanel } from '@/components/activity';
import { useToast } from '@/hooks/useToast';

type ActivityTab = 'queue' | 'history' | 'blocklist';

const TABS: Array<{ id: ActivityTab; label: string; icon: React.ReactNode }> = [
  { id: 'queue', label: 'Queue', icon: <DownloadCloud className="h-3.5 w-3.5" /> },
  { id: 'history', label: 'History', icon: <History className="h-3.5 w-3.5" /> },
  { id: 'blocklist', label: 'Blocklist', icon: <Ban className="h-3.5 w-3.5" /> },
];

/** Admin-only. Only the active sub-tab is mounted, so queue polling stops when it is left. */
export const ActivityView: React.FC = () => {
  const [tab, setTab] = useState<ActivityTab>('queue');
  const { toast, showToast } = useToast();

  return (
    <div className="space-y-6">
      {toast && <ToastBanner message={toast.message} tone={toast.tone} />}
      <TabStrip aria-label="Activity sections">
        {TABS.map((t) => (
          <TapeDeckButton key={t.id} size="sm" active={tab === t.id} onClick={() => setTab(t.id)} icon={t.icon}>
            {t.label}
          </TapeDeckButton>
        ))}
      </TabStrip>

      {tab === 'queue' && <ActivityQueuePanel onToast={showToast} />}
      {tab === 'history' && <ActivityHistoryPanel onToast={showToast} />}
      {tab === 'blocklist' && <ActivityBlocklistPanel onToast={showToast} />}
    </div>
  );
};
