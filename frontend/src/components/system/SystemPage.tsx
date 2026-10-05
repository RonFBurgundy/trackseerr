import React from 'react';
import type { LibraryManagerMode } from '@/types/models';
import type { SystemLeaf } from '@/hooks/useAppRoute';
import { SystemStatusPanel } from './SystemStatusPanel';
import { SystemQueuePanel } from './SystemQueuePanel';
import { SystemTasksPanel } from './SystemTasksPanel';
import { SystemEventsPanel } from './SystemEventsPanel';
import { SystemLogsPanel } from './SystemLogsPanel';

export type SystemTab = SystemLeaf;

export interface SystemPageProps {
  /** Which panel to show; chosen by the settings navigation row (route leaf). */
  tab: SystemTab;
  /** Core tier only: shows the Request portal card under Status. */
  isCore: boolean;
  libraryMode: LibraryManagerMode;
  onToast: (msg: string, tone?: 'ok' | 'error') => void;
}

/** Settings > System. Only the active panel is mounted, so polling and the log stream stop when it is left. */
export const SystemPage: React.FC<SystemPageProps> = ({ tab, isCore, libraryMode, onToast }) => (
  <div className="space-y-6">
    {tab === 'status' && <SystemStatusPanel isCore={isCore} libraryMode={libraryMode} />}
    {tab === 'queue' && <SystemQueuePanel />}
    {tab === 'tasks' && <SystemTasksPanel onToast={onToast} />}
    {tab === 'events' && <SystemEventsPanel />}
    {tab === 'logs' && <SystemLogsPanel />}
  </div>
);
