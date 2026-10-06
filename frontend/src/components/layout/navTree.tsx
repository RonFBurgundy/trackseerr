import React from 'react';
import { Compass, Inbox, Library, ListMusic, Activity, ListTodo, Settings } from 'lucide-react';
import { defaultRoute, routeToHash, settingsRouteFor } from '@/hooks/useAppRoute';
import type { AppRoute } from '@/hooks/useAppRoute';
import { buildSettingsTree, hasChildRow, resolveSettingsRoute } from '@/components/settings/settingsTabs';

/** A node of the hub navigator: a leaf carries a route, a group carries children. */
export interface NavNode {
  key: string;
  label: string;
  icon?: React.ReactNode;
  description?: string;
  route?: AppRoute;
  children?: NavNode[];
  /** Count chip (admin-only items), hidden when 0. */
  badge?: number;
}

export interface RouteAccess {
  isAdmin: boolean;
  mfaEnrollmentRequired: boolean;
  /** Library-health findings awaiting review (admin only). */
  reviewCount?: number;
  /** Open + in-progress issues awaiting an admin. */
  issuesOpenCount?: number;
  /** The user's own issues with unseen admin activity. */
  issuesUnreadCount?: number;
}

/** Nav entries match a page, not the issue open on top of it. */
const withoutIssue = (r: AppRoute): AppRoute => (r.tab === 'activity' && r.issueId !== undefined ? { tab: 'activity', sub: r.sub } : r);

export const routesEqual = (a: AppRoute, b: AppRoute): boolean => routeToHash(withoutIssue(a)) === routeToHash(withoutIssue(b));

/** Clamp a location to what this user may open (MFA enrollment, admin-only tabs, hidden settings pages). */
export function gateRoute(route: AppRoute, access: RouteAccess): AppRoute {
  if (access.mfaEnrollmentRequired) return resolveSettingsRoute(settingsRouteFor('account'), false, true);
  if (!access.isAdmin && (route.tab === 'library' || route.tab === 'activity' || route.tab === 'wanted')) {
    return defaultRoute('discover');
  }
  if (route.tab === 'settings') return resolveSettingsRoute(route, access.isAdmin, false);
  return route;
}

const ico = 'h-4 w-4';

const leaf = (key: string, label: string, route: AppRoute): NavNode => ({ key, label, route });

/** Every main section the user may see, with its pages as children where it has any. */
export function buildNavTree({ isAdmin, mfaEnrollmentRequired, reviewCount = 0, issuesOpenCount = 0, issuesUnreadCount = 0 }: RouteAccess): NavNode[] {
  const settingsChildren: NavNode[] = buildSettingsTree(isAdmin, mfaEnrollmentRequired).map((section) => {
    if (!hasChildRow(section)) {
      return {
        key: `settings/${section.id}`,
        label: section.label,
        icon: section.icon,
        route: settingsRouteFor(section.id, section.leaves[0]?.id),
      };
    }
    return {
      key: `settings/${section.id}`,
      label: section.label,
      icon: section.icon,
      children: section.leaves.map((l) => ({
        key: `settings/${section.id}/${l.id}`,
        label: l.label,
        icon: l.icon,
        route: settingsRouteFor(section.id, l.id),
      })),
    };
  });
  const settings: NavNode = {
    key: 'settings',
    label: 'Settings',
    description: 'System & server configuration',
    icon: <Settings className={`${ico} text-[#e5a00d]`} />,
    children: settingsChildren,
  };
  if (mfaEnrollmentRequired) return [settings];

  const tree: NavNode[] = [
    {
      key: 'discover',
      label: 'Discover',
      description: 'Explore trending & search catalog',
      icon: <Compass className={ico} />,
      route: defaultRoute('discover'),
    },
    {
      key: 'requests',
      label: 'Requests',
      description: 'Manage & monitor your queue',
      icon: <Inbox className={ico} />,
      badge: issuesUnreadCount,
      children: [
        leaf('requests/all', 'All', { tab: 'requests', sub: 'all' }),
        leaf('requests/pending', 'Pending', { tab: 'requests', sub: 'pending' }),
        leaf('requests/approved', 'Approved', { tab: 'requests', sub: 'approved' }),
        leaf('requests/fulfilled', 'Fulfilled', { tab: 'requests', sub: 'fulfilled' }),
        leaf('requests/rejected', 'Rejected', { tab: 'requests', sub: 'rejected' }),
        { ...leaf('requests/issues', 'My issues', { tab: 'requests', sub: 'issues' }), badge: issuesUnreadCount },
      ],
    },
  ];
  if (isAdmin) {
    tree.push({
      key: 'library',
      label: 'Library',
      description: 'Synced audio collection',
      icon: <Library className={ico} />,
      children: [
        leaf('library/artists', 'Artists', { tab: 'library', sub: 'artists' }),
        leaf('library/albums', 'Albums', { tab: 'library', sub: 'albums' }),
        leaf('library/tracks', 'Tracks', { tab: 'library', sub: 'tracks' }),
        leaf('library/collections', 'Collections', { tab: 'library', sub: 'collections' }),
      ],
    });
  }
  tree.push({
    key: 'playlists',
    label: 'Playlists',
    description: 'Auto-syncing Spotify & Tidal lists',
    icon: <ListMusic className={ico} />,
    route: defaultRoute('playlists'),
  });
  if (isAdmin) {
    tree.push(
      {
        key: 'activity',
        label: 'Activity',
        description: 'Lidarr & download deck status',
        icon: <Activity className={ico} />,
        badge: reviewCount + issuesOpenCount,
        children: [
          leaf('activity/queue', 'Queue', { tab: 'activity', sub: 'queue' }),
          leaf('activity/history', 'History', { tab: 'activity', sub: 'history' }),
          leaf('activity/blocklist', 'Blocklist', { tab: 'activity', sub: 'blocklist' }),
          { ...leaf('activity/review', 'Needs review', { tab: 'activity', sub: 'review' }), badge: reviewCount },
          { ...leaf('activity/issues', 'Issues', { tab: 'activity', sub: 'issues' }), badge: issuesOpenCount },
        ],
      },
      {
        key: 'wanted',
        label: 'Wanted',
        description: 'Missing & cutoff-unmet music',
        icon: <ListTodo className={ico} />,
        children: [
          leaf('wanted/missing', 'Missing', { tab: 'wanted', sub: 'missing' }),
          leaf('wanted/cutoff', 'Cutoff Unmet', { tab: 'wanted', sub: 'cutoff' }),
        ],
      }
    );
  }
  tree.push(settings);
  return tree;
}

/** Keys of every group on the path to the active leaf (so the hub opens with the current page visible). */
export function activeAncestorKeys(nodes: NavNode[], route: AppRoute): string[] {
  const walk = (list: NavNode[], trail: string[]): string[] | null => {
    for (const node of list) {
      if (node.route && routesEqual(node.route, route.tab === 'library' ? { tab: 'library', sub: route.sub } : route.tab === 'discover' ? { tab: 'discover' } : route)) return trail;
      if (node.children) {
        const found = walk(node.children, [...trail, node.key]);
        if (found) return found;
      }
    }
    return null;
  };
  return walk(nodes, []) ?? [];
}

