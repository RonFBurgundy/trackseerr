import React from 'react';
import type { LibraryManagerMode } from '@/types/models';
import type { SystemLeaf } from '@/hooks/useAppRoute';
import { SystemStatusPanel } from './SystemStatusPanel';
import { SystemTasksPanel } from './SystemTasksPanel';
import { SystemLogsPanel } from './SystemLogsPanel';
import { BackupPanel } from './BackupPanel';

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
export const SystemPage: React.FC<SystemPageProps> = ({ tab, isCore, libraryMode, onToast }) => {
  // Logs own their scroller (a nested PageFrame filling the settings body); the rest flow in the body.
  if (tab === 'logs') return <SystemLogsPanel />;
  return (
    <div className="space-y-6">
      {tab === 'status' && <SystemStatusPanel isCore={isCore} libraryMode={libraryMode} />}
      {tab === 'tasks' && <SystemTasksPanel onToast={onToast} />}
      {tab === 'backups' && <BackupPanel onToast={onToast} />}
    </div>
  );
};

/** True for the System leaves whose panel owns its own scroll region (the settings body must not scroll for them). */
export const systemTabOwnsScroll = (tab: SystemTab): boolean => tab === 'logs';
