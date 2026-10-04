import React, { useState } from 'react';
import { Activity, List, ListChecks, ScrollText, Terminal } from 'lucide-react';
import { TapeTransportBay, TapeDeckButton } from '@/components/ui';
import type { LibraryManagerMode } from '@/types/models';
import { SystemStatusPanel } from './SystemStatusPanel';
import { SystemQueuePanel } from './SystemQueuePanel';
import { SystemTasksPanel } from './SystemTasksPanel';
import { SystemEventsPanel } from './SystemEventsPanel';
import { SystemLogsPanel } from './SystemLogsPanel';

export type SystemTab = 'status' | 'queue' | 'tasks' | 'events' | 'logs';

const TABS: Array<{ id: SystemTab; label: string; icon: React.ReactNode }> = [
  { id: 'status', label: 'Status', icon: <Activity className="h-3.5 w-3.5" /> },
  { id: 'queue', label: 'Queue', icon: <List className="h-3.5 w-3.5" /> },
  { id: 'tasks', label: 'Tasks', icon: <ListChecks className="h-3.5 w-3.5" /> },
  { id: 'events', label: 'Events', icon: <ScrollText className="h-3.5 w-3.5" /> },
  { id: 'logs', label: 'Logs', icon: <Terminal className="h-3.5 w-3.5" /> },
];

export interface SystemPageProps {
  /** Core tier only: shows the Request portal card under Status. */
  isCore: boolean;
  libraryMode: LibraryManagerMode;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Settings > System. Only the active sub-tab is mounted, so polling and the log stream stop when it is left. */
export const SystemPage: React.FC<SystemPageProps> = ({ isCore, libraryMode, onToast }) => {
  const [tab, setTab] = useState<SystemTab>('status');

  return (
    <div className="space-y-6">
      <TapeTransportBay className="flex items-center gap-1.5 overflow-x-auto" aria-label="System sections">
        {TABS.map((t) => (
          <TapeDeckButton key={t.id} size="sm" active={tab === t.id} onClick={() => setTab(t.id)} icon={t.icon}>
            {t.label}
          </TapeDeckButton>
        ))}
      </TapeTransportBay>

      {tab === 'status' && <SystemStatusPanel isCore={isCore} libraryMode={libraryMode} />}
      {tab === 'queue' && <SystemQueuePanel />}
      {tab === 'tasks' && <SystemTasksPanel onToast={onToast} />}
      {tab === 'events' && <SystemEventsPanel />}
      {tab === 'logs' && <SystemLogsPanel />}
    </div>
  );
};
