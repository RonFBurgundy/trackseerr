import React from 'react';
import {
  Sliders,
  Folder,
  Download,
  Search,
  Radio,
  Layers,
  UserRound,
  Users,
  Server,
  ListMusic,
  HardDrive,
  Activity,
  List,
  ListChecks,
  ScrollText,
  Terminal,
  Boxes,
  Inbox,
  Gauge,
  Tags,
  Bell,
} from 'lucide-react';
import { settingsRouteFor } from '@/hooks/useAppRoute';
import type { SettingsLeafId, SettingsRoute, SettingsSection } from '@/hooks/useAppRoute';

/** Panel ids: what SettingsView actually renders for a location. */
export type SettingsTab =
  | 'general'
  | 'media'
  | 'quality'
  | 'profiles'
  | 'custom-formats'
  | 'clients'
  | 'indexers'
  | 'lidarr'
  | 'media-server'
  | 'import-lists'
  | 'users'
  | 'scrobbling'
  | 'notifications'
  | 'system'
  | 'account';

export interface SettingsLeafNode {
  id: SettingsLeafId;
  label: string;
  icon: React.ReactNode;
}

export interface SettingsSectionNode {
  id: SettingsSection;
  label: string;
  icon: React.ReactNode;
  /** Visible child pages. Empty for single-page sections. */
  leaves: SettingsLeafNode[];
}

const ico = 'h-3.5 w-3.5';

const leaf = (id: SettingsLeafId, label: string, icon: React.ReactNode): SettingsLeafNode => ({ id, label, icon });

/** A section shows a child row only when it has more than one page to choose from. */
export const hasChildRow = (section: SettingsSectionNode): boolean => section.leaves.length > 1;

/**
 * Settings tree, in display order. Visibility rules: MFA enrollment pending -> Account only;
 * non-admins -> Requests (Scrobbling only) + Account; admins -> everything.
 */
export function buildSettingsTree(isAdmin: boolean, mfaEnrollmentRequired: boolean): SettingsSectionNode[] {
  const account: SettingsSectionNode = {
    id: 'account',
    label: 'Account',
    icon: <UserRound className={ico} />,
    leaves: [],
  };
  if (mfaEnrollmentRequired) return [account];
  if (!isAdmin) {
    return [
      {
        id: 'requests',
        label: 'Requests',
        icon: <Inbox className={ico} />,
        leaves: [leaf('scrobbling', 'Scrobbling', <Radio className={ico} />)],
      },
      account,
    ];
  }
  return [
    { id: 'general', label: 'General', icon: <Sliders className={ico} />, leaves: [] },
    {
      id: 'media-management',
      label: 'Media Management',
      icon: <Boxes className={ico} />,
      leaves: [
        leaf('media', 'Root Folders & Naming', <Folder className={ico} />),
        leaf('quality', 'Quality', <Gauge className={ico} />),
        leaf('profiles', 'Profiles', <Layers className={ico} />),
        leaf('custom-formats', 'Custom Formats', <Tags className={ico} />),
        leaf('clients', 'Clients', <Download className={ico} />),
        leaf('indexers', 'Indexers', <Search className={ico} />),
        leaf('import-lists', 'Import Lists', <ListMusic className={ico} />),
        leaf('media-server', 'Media Server', <HardDrive className={ico} />),
      ],
    },
    { id: 'lidarr', label: 'Lidarr', icon: <Radio className={ico} />, leaves: [] },
    {
      id: 'requests',
      label: 'Requests',
      icon: <Inbox className={ico} />,
      leaves: [
        leaf('users', 'Users', <Users className={ico} />),
        leaf('scrobbling', 'Scrobbling', <Radio className={ico} />),
        leaf('notifications', 'Notifications', <Bell className={ico} />),
      ],
    },
    {
      id: 'system',
      label: 'System',
      icon: <Server className={ico} />,
      leaves: [
        leaf('status', 'Status', <Activity className={ico} />),
        leaf('queue', 'Queue', <List className={ico} />),
        leaf('tasks', 'Tasks', <ListChecks className={ico} />),
        leaf('events', 'Events', <ScrollText className={ico} />),
        leaf('logs', 'Logs', <Terminal className={ico} />),
      ],
    },
    account,
  ];
}

/** Clamp a settings route to what this user may see (unknown or hidden section/leaf -> first visible). */
export function resolveSettingsRoute(
  route: SettingsRoute,
  isAdmin: boolean,
  mfaEnrollmentRequired: boolean
): SettingsRoute {
  const sections = buildSettingsTree(isAdmin, mfaEnrollmentRequired);
  const section = sections.find((s) => s.id === route.sub) ?? sections[0];
  const requestedLeaf = 'leaf' in route ? route.leaf : undefined;
  const visibleLeaf =
    section.leaves.find((l) => l.id === requestedLeaf)?.id ?? section.leaves[0]?.id;
  return settingsRouteFor(section.id, visibleLeaf);
}

/** The panel SettingsView renders for a (resolved) route. */
export function settingsPanel(route: SettingsRoute): SettingsTab {
  switch (route.sub) {
    case 'media-management':
    case 'requests':
      return route.leaf;
    case 'system':
      return 'system';
    default:
      return route.sub;
  }
}

/** Tabs that only touch the signed-in user's own data and need no admin settings load. */
export const SELF_SERVICE_TABS: ReadonlySet<SettingsTab> = new Set<SettingsTab>([
  'scrobbling',
  'notifications',
  'account',
  'users',
  'system',
  'import-lists',
  'media-server',
]);

export const MEDIA_MANAGEMENT_TABS: ReadonlySet<SettingsTab> = new Set<SettingsTab>([
  'media',
  'quality',
  'profiles',
  'custom-formats',
  'clients',
  'indexers',
]);
