import React from 'react';
import { Sliders, Folder, Download, Search, Radio, Layers, UserRound, Users, Server, ListMusic, HardDrive } from 'lucide-react';

export type SettingsTab =
  | 'general'
  | 'media'
  | 'profiles'
  | 'clients'
  | 'indexers'
  | 'lidarr'
  | 'media-server'
  | 'import-lists'
  | 'users'
  | 'scrobbling'
  | 'system'
  | 'account';

export interface SettingsNavItem {
  id: SettingsTab;
  label: string;
  icon: React.ReactNode;
}

export interface SettingsNavGroup {
  id: string;
  label: string;
  items: SettingsNavItem[];
}

const ico = 'h-3.5 w-3.5';

const item = (id: SettingsTab, label: string, icon: React.ReactNode): SettingsNavItem => ({ id, label, icon });

/**
 * Grouped settings navigation. Visibility rules (unchanged from the flat tab bar):
 * MFA enrollment pending -> Account only; non-admins -> Scrobbling + Account; admins -> everything.
 */
export function buildSettingsGroups(isAdmin: boolean, mfaEnrollmentRequired: boolean): SettingsNavGroup[] {
  const account: SettingsNavGroup = {
    id: 'account',
    label: 'Account',
    items: [item('account', 'Account', <UserRound className={ico} />)],
  };
  if (mfaEnrollmentRequired) return [account];
  if (!isAdmin) {
    return [
      { id: 'requests', label: 'Requests', items: [item('scrobbling', 'Scrobbling', <Radio className={ico} />)] },
      account,
    ];
  }
  return [
    { id: 'general', label: 'General', items: [item('general', 'General', <Sliders className={ico} />)] },
    {
      id: 'media-management',
      label: 'Media Management',
      items: [
        item('media', 'Root Folders & Naming', <Folder className={ico} />),
        item('profiles', 'Profiles', <Layers className={ico} />),
        item('clients', 'Clients', <Download className={ico} />),
        item('indexers', 'Indexers', <Search className={ico} />),
      ],
    },
    { id: 'lidarr', label: 'Lidarr', items: [item('lidarr', 'Lidarr', <Radio className={ico} />)] },
    {
      id: 'media-server',
      label: 'Media Server',
      items: [item('media-server', 'Media Server', <HardDrive className={ico} />)],
    },
    {
      id: 'import-lists',
      label: 'Import Lists',
      items: [item('import-lists', 'Import Lists', <ListMusic className={ico} />)],
    },
    {
      id: 'requests',
      label: 'Requests',
      items: [
        item('users', 'Users', <Users className={ico} />),
        item('scrobbling', 'Scrobbling', <Radio className={ico} />),
      ],
    },
    { id: 'system', label: 'System', items: [item('system', 'System', <Server className={ico} />)] },
    account,
  ];
}

/** Tabs that only touch the signed-in user's own data and need no admin settings load. */
export const SELF_SERVICE_TABS: ReadonlySet<SettingsTab> = new Set<SettingsTab>([
  'scrobbling',
  'account',
  'users',
  'system',
  'import-lists',
  'media-server',
]);

export const MEDIA_MANAGEMENT_TABS: ReadonlySet<SettingsTab> = new Set<SettingsTab>([
  'media',
  'profiles',
  'clients',
  'indexers',
]);
