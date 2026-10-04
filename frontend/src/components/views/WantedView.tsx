import React, { useState } from 'react';
import { ArrowUpCircle, SearchX } from 'lucide-react';
import { TapeTransportBay, TapeDeckButton, ToastBanner } from '@/components/ui';
import { WantedPanel } from '@/components/wanted';
import { useToast } from '@/hooks/useToast';
import type { WantedListName } from '@/types/activity';

const TABS: Array<{ id: WantedListName; label: string; icon: React.ReactNode }> = [
  { id: 'missing', label: 'Missing', icon: <SearchX className="h-3.5 w-3.5" /> },
  { id: 'cutoff', label: 'Cutoff Unmet', icon: <ArrowUpCircle className="h-3.5 w-3.5" /> },
];

/** Admin-only. Switching sub-tab remounts the panel, which resets sort and selection. */
export const WantedView: React.FC = () => {
  const [tab, setTab] = useState<WantedListName>('missing');
  const { toast, showToast } = useToast();

  return (
    <div className="space-y-6">
      {toast && <ToastBanner message={toast.message} tone={toast.tone} />}
      <TapeTransportBay className="flex items-center gap-1.5 overflow-x-auto" aria-label="Wanted sections">
        {TABS.map((t) => (
          <TapeDeckButton key={t.id} size="sm" active={tab === t.id} onClick={() => setTab(t.id)} icon={t.icon}>
            {t.label}
          </TapeDeckButton>
        ))}
      </TapeTransportBay>

      <WantedPanel key={tab} list={tab} onToast={showToast} />
    </div>
  );
};
