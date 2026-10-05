import React from 'react';
import { ArrowUpCircle, SearchX } from 'lucide-react';
import { TabStrip, TapeDeckButton, ToastBanner } from '@/components/ui';
import { WantedPanel } from '@/components/wanted';
import { useToast } from '@/hooks/useToast';
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

  return (
    <div className="space-y-6">
      {toast && <ToastBanner message={toast.message} tone={toast.tone} />}
      <TabStrip aria-label="Wanted sections">
        {TABS.map((t) => (
          <TapeDeckButton key={t.id} size="sm" active={tab === t.id} onClick={() => onSubChange(t.id)} icon={t.icon}>
            {t.label}
          </TapeDeckButton>
        ))}
      </TabStrip>

      <WantedPanel key={tab} list={tab} onToast={showToast} />
    </div>
  );
};
