import React, { useState } from 'react';
import { ArrowUpCircle, Calendar, SearchX } from 'lucide-react';
import { PageFrame } from '@/components/layout';
import { TabStrip, TapeDeckButton, ToastBanner } from '@/components/ui';
import { WantedPanel } from '@/components/wanted';
import { useToast } from '@/hooks/useToast';
import { useRefreshHandler } from '@/hooks/useRefreshHandler';
import type { WantedSub, AppRoute, NavigateOptions } from '@/hooks/useAppRoute';
import { CalendarView } from './CalendarView';

const TABS: Array<{ id: WantedSub; label: string; icon: React.ReactNode }> = [
  { id: 'missing', label: 'Missing', icon: <SearchX className="h-3.5 w-3.5" /> },
  { id: 'cutoff', label: 'Cutoff Unmet', icon: <ArrowUpCircle className="h-3.5 w-3.5" /> },
  { id: 'calendar', label: 'Calendar', icon: <Calendar className="h-3.5 w-3.5" /> },
];

/** Admin-only. Switching sub-tab remounts the panel, which resets sort and selection. */
export interface WantedViewProps {
  sub: WantedSub;
  onSubChange: (sub: WantedSub, options?: NavigateOptions) => void;
  onNavigate: (route: AppRoute, options?: NavigateOptions) => void;
}

export const WantedView: React.FC<WantedViewProps> = ({ sub, onSubChange, onNavigate }) => {
  const { toast, showToast } = useToast();
  const [reloadKey, setReloadKey] = useState<number>(0);
  useRefreshHandler(() => setReloadKey((k) => k + 1));

  const strip = (
    <TabStrip aria-label="Wanted sections">
      {TABS.map((t) => (
        <TapeDeckButton key={t.id} size="sm" active={sub === t.id} onClick={() => onSubChange(t.id)} icon={t.icon}>
          {t.label}
        </TapeDeckButton>
      ))}
    </TabStrip>
  );

  if (sub === 'calendar') {
    return <CalendarView tabs={strip} onNavigate={onNavigate} />;
  }

  return (
    <PageFrame
      bodyClassName="space-y-6"
      nav={
        <>
          {toast && <ToastBanner message={toast.message} tone={toast.tone} />}
          {strip}
        </>
      }
    >
      <WantedPanel key={`${sub}:${reloadKey}`} list={sub} onToast={showToast} />
    </PageFrame>
  );
};
