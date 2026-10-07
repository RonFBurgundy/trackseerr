import React, { useState } from 'react';
import { ArrowUpCircle, SearchX } from 'lucide-react';
import { PageFrame } from '@/components/layout';
import { TabStrip, TapeDeckButton, ToastBanner } from '@/components/ui';
import { WantedPanel } from '@/components/wanted';
import { useToast } from '@/hooks/useToast';
import { useRefreshHandler } from '@/hooks/useRefreshHandler';
import type { WantedSub, NavigateOptions } from '@/hooks/useAppRoute';

const TABS: Array<{ id: WantedSub; label: string; icon: React.ReactNode }> = [
  { id: 'missing', label: 'Missing', icon: <SearchX className="h-3.5 w-3.5" /> },
  { id: 'cutoff', label: 'Cutoff Unmet', icon: <ArrowUpCircle className="h-3.5 w-3.5" /> },
];

/** Admin-only. Switching sub-tab remounts the panel, which resets sort and selection. */
export interface WantedViewProps {
  sub: WantedSub;
  onSubChange: (sub: WantedSub, options?: NavigateOptions) => void;
}

export const WantedView: React.FC<WantedViewProps> = ({ sub: tab, onSubChange }) => {
  const { toast, showToast } = useToast();
  const [reloadKey, setReloadKey] = useState<number>(0);
  useRefreshHandler(() => setReloadKey((k) => k + 1));

  return (
    <PageFrame
      bodyClassName="space-y-6"
      nav={
      <>
      {toast && <ToastBanner message={toast.message} tone={toast.tone} />}
      <TabStrip aria-label="Wanted sections">
        {TABS.map((t) => (
          <TapeDeckButton key={t.id} size="sm" active={tab === t.id} onClick={() => onSubChange(t.id)} icon={t.icon}>
            {t.label}
          </TapeDeckButton>
        ))}
      </TabStrip>
      </>
      }
    >
      <WantedPanel key={`${tab}:${reloadKey}`} list={tab} onToast={showToast} />
    </PageFrame>
  );
};
